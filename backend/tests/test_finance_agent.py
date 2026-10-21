"""Receivables agent: tool loop, figure check, proposal policy, provider fallback and offline evals."""
import asyncio

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agents import finance_agent
from app.agents.assistant_eval import load_fixture, run_offline
from app.agents.finance_agent import MAX_STEPS, Provider, answer_question, inr
from app.agents.finance_tools import run_tool
from app.agents.grounding import check_grounding, drop_ungrounded, extract_claims, grounded_values
from app.services.assistant_tracing import TraceRecorder


def call(name, args=None, id_="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args or {}, "id": id_}])


class ScriptedLLM:
    """Returns the scripted replies in order; an Exception in the script is raised instead."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def bind_tools(self, specs):
        return _Bound(self, specs)

    async def ainvoke(self, messages, *, tools=False):
        self.calls.append({"messages": list(messages), "tools": tools})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class _Bound:
    def __init__(self, llm, specs):
        self.llm, self.specs = llm, specs

    async def ainvoke(self, messages):
        return await self.llm.ainvoke(messages, tools=True)


@pytest.fixture(autouse=True)
def fresh_cooldowns():
    finance_agent._cooldowns.clear()
    yield
    finance_agent._cooldowns.clear()


@pytest.fixture
def portfolio():
    return load_fixture()[0]


def ask(portfolio, question, *llms, history=()):
    recorder = TraceRecorder()
    providers = [Provider(f"p{i}", f"model-{i}", llm) for i, llm in enumerate(llms)]
    result = asyncio.run(answer_question(portfolio, question, list(history), providers, recorder))
    return result, recorder


def test_offline_eval_set_passes_every_case():
    report = run_offline()
    assert report["cases"] == 100
    assert report["passed"] == 100, [r for r in report["results"] if not r["passed"]]


def test_agent_calls_the_tool_the_question_needs_and_answers_from_it(portfolio):
    llm = ScriptedLLM(call("get_aging_breakdown"),
                      AIMessage("₹5 lakh is over 90 days past due, so start with that bucket."))
    result, recorder = ask(portfolio, "How much is over 90 days?", llm)
    assert result.tools_called == ["get_aging_breakdown"]
    assert result.answer.startswith("₹5 lakh") and result.provider == "p0" and not result.fallback_used
    assert result.verification == {"figures_checked": 2, "repaired": False, "removed": [], "blocked_proposals": 0,
                                   "policy_repaired": False, "policy_removed": []}
    tool_message = llm.calls[1]["messages"][-1]
    assert isinstance(tool_message, ToolMessage) and "500000.00" in tool_message.content
    assert [s.kind for s in recorder.spans] == ["llm", "tool", "llm", "check", "policy", "check", "run"]
    assert (recorder.spans[-2].name, recorder.spans[-2].outcome) == ("result", "verified")
    assert (recorder.spans[-1].name, recorder.spans[-1].provider, recorder.spans[-1].outcome) == (
        "model-0", "p0", "primary")


def test_made_up_figure_triggers_one_repair_round(portfolio):
    llm = ScriptedLLM(call("get_portfolio_summary"), AIMessage("Overdue is ₹19.9 lakh."),
                      AIMessage("Overdue is ₹15.2 lakh across 7 invoices."))
    result, recorder = ask(portfolio, "How much is overdue?", llm)
    assert result.answer == "Overdue is ₹15.2 lakh across 7 invoices."
    assert result.verification["repaired"] and not result.verification["removed"]
    assert "₹19.9 lakh" in llm.calls[2]["messages"][-1].content
    assert [s.outcome for s in recorder.spans if s.kind == "check"] == ["ungrounded", "ok", "repaired"]


def test_figure_that_survives_repair_is_dropped_with_its_sentence(portfolio):
    llm = ScriptedLLM(call("get_portfolio_summary"), AIMessage("Overdue is ₹15.2 lakh. Acme will pay ₹4 crore."),
                      AIMessage("Overdue is ₹15.2 lakh. Acme will pay ₹4 crore."))
    result, _ = ask(portfolio, "How much is overdue?", llm)
    assert result.answer == "Overdue is ₹15.2 lakh."
    assert result.verification["removed"] == ["₹4 crore"]


def test_answer_with_figures_but_no_tool_call_is_not_trusted(portfolio):
    llm = ScriptedLLM(AIMessage("You have ₹42 lakh overdue."), AIMessage("You have ₹42 lakh overdue."))
    result, _ = ask(portfolio, "How much is overdue?", llm)
    assert "42" not in result.answer and result.answer == finance_agent.UNVERIFIED_REPLY


def test_policy_blocks_bad_proposals_and_returns_only_allowed_ones(portfolio):
    llm = ScriptedLLM(
        AIMessage(content="", tool_calls=[
            {"name": "propose_decision", "id": "a", "args": {
                "invoice_number": "INV-1007", "decision": "ESCALATE", "reason": "Large balance, call the owner"}},
            {"name": "propose_decision", "id": "b", "args": {
                "invoice_number": "INV-1009", "decision": "ESCALATE", "reason": "₹3 lakh, 61 days overdue at 83% risk"}},
            {"name": "propose_decision", "id": "c", "args": {
                "invoice_number": "INV-1002", "decision": "ESCALATE", "reason": "late"}}]),
        AIMessage("I suggest escalating INV-1009: ₹3 lakh is 61 days overdue."))
    result, recorder = ask(portfolio, "What should we escalate?", llm)
    assert [(p["invoice"], p["decision"]) for p in result.proposals] == [("INV-1009", "ESCALATE")]
    assert result.proposals[0]["invoice_id"] and result.verification["blocked_proposals"] == 2
    blocked = [m for m in llm.calls[1]["messages"] if isinstance(m, ToolMessage) and '"allowed": false' in m.content]
    assert len(blocked) == 2 and "fully paid" in blocked[0].content and "stated reason" in blocked[1].content
    assert [s.outcome for s in recorder.spans if s.kind == "policy" and s.name == "ESCALATE"] == [
        "blocked", "allowed", "blocked"]


@pytest.mark.parametrize("invoice,decision,reason,violation", [
    ("INV-1006", "ESCALATE", "A long enough reason to escalate this", "not overdue yet"),
    ("INV-1001", "ESCALATE", "Already escalated but asked again", "already marked ESCALATE"),
    ("INV-9999", "APPROVE", "Unknown invoice", "not in this workspace"),
    ("INV-1010", "APPROVE", "Chase the paid one", "fully paid"),
    ("INV-1002", "DELETE", "Not a decision", "decision must be one of"),
])
def test_proposal_rules(portfolio, invoice, decision, reason, violation):
    result = run_tool(portfolio, "propose_decision", {"invoice_number": invoice, "decision": decision, "reason": reason})
    assert result["allowed"] is False and any(violation in v for v in result["blocked_because"])


def test_next_provider_takes_over_when_one_is_rate_limited(portfolio):
    limited = ScriptedLLM(RuntimeError("429 quota exceeded"))
    backup = ScriptedLLM(call("get_portfolio_summary"), AIMessage("Overdue is ₹15.2 lakh."))
    result, recorder = ask(portfolio, "How much is overdue?", limited, backup)
    assert result.provider == "p1" and result.fallback_used and result.answer == "Overdue is ₹15.2 lakh."
    assert [(s.kind, s.outcome) for s in recorder.spans[:2]] == [("llm", "rate_limited"), ("fallback", "next")]
    assert len(limited.calls) == 1  # a failed provider is not retried within the run


def test_rule_based_answer_when_every_provider_fails(portfolio):
    result, recorder = ask(portfolio, "Which account owes the most?", ScriptedLLM(RuntimeError("down")),
                           ScriptedLLM(asyncio.TimeoutError()))
    assert result.model == "rule-based-fallback" and result.fallback_used
    assert "Acme Traders owes ₹7.2 lakh" in result.answer
    assert recorder.spans[-1].kind == "run" and recorder.spans[-1].outcome == "rules"


def test_no_provider_configured_still_answers_from_rules(portfolio):
    result, recorder = ask(portfolio, "How much is overdue?")
    assert result.model == "rule-based-fallback" and "₹15.2 lakh" in result.answer
    assert recorder.spans[-1].outcome == "unconfigured"


def test_step_cap_forces_a_final_answer_without_tools(portfolio):
    llm = ScriptedLLM(*[call("get_portfolio_summary", id_=str(i)) for i in range(MAX_STEPS - 1)],
                      AIMessage("Overdue is ₹15.2 lakh."))
    result, _ = ask(portfolio, "How much is overdue?", llm)
    assert len(llm.calls) == MAX_STEPS and llm.calls[-1]["tools"] is False
    assert "no more tool calls" in llm.calls[-1]["messages"][-1].content
    assert result.answer == "Overdue is ₹15.2 lakh."


def test_tool_calls_beyond_the_per_step_cap_are_refused(portfolio):
    many = AIMessage(content="", tool_calls=[{"name": "get_portfolio_summary", "args": {}, "id": str(i)}
                                            for i in range(6)])
    llm = ScriptedLLM(many, AIMessage("Overdue is ₹15.2 lakh."))
    result, _ = ask(portfolio, "How much is overdue?", llm)
    assert len(result.tools_called) == finance_agent.MAX_TOOLS_PER_STEP
    refused = [m for m in llm.calls[1]["messages"] if isinstance(m, ToolMessage) and "at most" in m.content]
    assert len(refused) == 2


def test_tools_reject_unknown_names_and_bad_arguments(portfolio):
    assert "unknown tool" in run_tool(portfolio, "delete_invoice", {})["error"]
    assert "status must be" in run_tool(portfolio, "search_invoices", {"status": "everything"})["error"]
    assert "invalid arguments" in run_tool(portfolio, "get_portfolio_summary", {"sql": "drop"})["error"]
    assert "invalid arguments" in run_tool(portfolio, "search_invoices", {"min_days_past_due": "lots"})["error"]
    assert run_tool(portfolio, "get_invoice", {"invoice_number": "inv-1001"})["found"] is True


def test_grounding_reads_indian_amounts_and_ignores_dates_and_identifiers():
    values = grounded_values({"overdue": "1520000.00", "share": 83.06, "risk": "0.91", "days": 122})
    text = "INV-1001 (due 2026-06-01, 1 June 2026) is 122 days late: ₹15.2 lakh, 83% of open, 91% risk, top 3."
    assert [c.text for c in extract_claims(text)] == ["122", "₹15.2 lakh", "83%", "91%"]
    assert check_grounding(text, values).ok
    assert check_grounding("₹15,20,000 or ₹1,520,000 or ₹0.15 crore", values).ok
    report = check_grounding("Overdue is ₹15.2 lakh. It will fall to ₹9 lakh next month.", values)
    assert [c.text for c in report.ungrounded] == ["₹9 lakh"]
    assert drop_ungrounded("Overdue is ₹15.2 lakh. It will fall to ₹9 lakh next month.", report) == \
        "Overdue is ₹15.2 lakh."


@pytest.mark.parametrize("amount,text", [
    ("1520000", "₹15.2 lakh"), ("500000", "₹5 lakh"), ("60000", "₹60,000"), ("90500000", "₹9.05 crore"),
    ("120000000", "₹12 crore"), ("1234567", "₹12.35 lakh"), ("950", "₹950")])
def test_inr_formatting(amount, text):
    assert inr(amount) == text


def test_what_if_answer_is_grounded_through_the_calculator(portfolio):
    llm = ScriptedLLM(
        AIMessage(content="", tool_calls=[{"name": "get_portfolio_summary", "args": {}, "id": "a"},
                                          {"name": "get_customer_profile", "args": {"account": "acme"}, "id": "b"}]),
        call("calculate", {"expression": "1520000 - 720000"}, "c"),
        AIMessage("If Acme cleared its ₹7.2 lakh, ₹8 lakh would still be overdue."))
    result, _ = ask(portfolio, "What if Acme paid everything overdue?", llm)
    assert result.tools_called == ["get_portfolio_summary", "get_customer_profile", "calculate"]
    assert result.verification["figures_checked"] == 2 and not result.verification["removed"]


@pytest.mark.parametrize("expression", [
    "__import__('os').system('id')", "open('x')", "2 ** 999", "a + 1", "[1, 2]", "1 if 1 else 2", "9e999 * 9e999",
    "1 / 0", "x" * 301])
def test_calculator_only_does_arithmetic(portfolio, expression):
    assert "error" in run_tool(portfolio, "calculate", {"expression": expression})


def test_calculator_accepts_amounts_as_written(portfolio):
    result = run_tool(portfolio, "calculate", {"expression": "round((₹15,20,000 - 720,000) / 1830000 * 100, 1)"})
    assert result == {"expression": "round((1520000 - 720000) / 1830000 * 100, 1)", "result": 43.7}
    assert run_tool(portfolio, "calculate", {"expression": "max(5, 3) - abs(-2)"})["result"] == 3


def test_customer_profile_track_record_and_ambiguity(portfolio):
    delta = run_tool(portfolio, "get_customer_profile", {"account": "delta"})
    assert (delta["cleared_invoices"], delta["cleared_on_time"], delta["avg_days_late_when_cleared"]) == (1, 0, 15)
    evergreen = run_tool(portfolio, "get_customer_profile", {"account": "Evergreen Pharma"})
    assert evergreen["on_time_rate_pct"] == 100 and evergreen["avg_days_late_when_cleared"] == -4
    vague = run_tool(portfolio, "get_customer_profile", {"account": "o"})
    assert vague["found"] is False and len(vague["candidates"]) > 1
    assert run_tool(portfolio, "get_customer_profile", {"account": "Nobody Ltd"})["candidates"] is None


def test_trend_months_and_dso(portfolio):
    trend = run_tool(portfolio, "get_collections_trend", {"months": 6})
    assert [m["month"] for m in trend["months"]] == ["2026-05", "2026-06", "2026-07", "2026-08", "2026-09", "2026-10"]
    assert trend["months"][3] == {"month": "2026-08", "invoiced": "550000.00", "collected": "200000.00"}
    assert trend["dso_days_90d"] == 155.4
    assert (trend["window_total_invoiced"], trend["window_total_collected"]) == ("2080000.00", "340000.00")


def test_due_window_excludes_overdue_and_far_future(portfolio):
    found = run_tool(portfolio, "search_invoices", {"status": "open", "due_within_days": 30})
    assert [r["invoice"] for r in found["invoices"]] == ["INV-1006"]


def test_explain_risk_uses_the_workspace_explainer_and_degrades_cleanly(portfolio):
    async def explainer(invoice_id):
        assert invoice_id == portfolio.by_number["INV-1009"].id
        return {"explanation_method": "LightGBM native TreeSHAP", "contributions": [
            {"feature": "a", "label": "Days past due", "value": 61, "shap_value": 0.9},
            {"feature": "b", "label": "Customer history", "value": 1, "shap_value": -0.2},
            {"feature": "c", "label": "Amount", "value": 400000, "shap_value": 0.05}]}

    portfolio.explain = explainer
    result = run_tool(portfolio, "explain_risk", {"invoice_number": "INV-1009"})
    assert result["available"] and result["model_risk_pct"] == 83.0
    assert [(d["factor"], d["pushes_risk"]) for d in result["top_drivers"]] == [
        ("Days past due", "up"), ("Customer history", "down"), ("Amount", "up")]
    assert run_tool(portfolio, "explain_risk", {"invoice_number": "INV-1007"})["reason"] == "invoice has no model score"

    async def broken(invoice_id):
        raise RuntimeError("artifact missing")

    portfolio.explain = broken
    assert run_tool(portfolio, "explain_risk", {"invoice_number": "INV-1009"})["available"] is False


@pytest.mark.parametrize("question,route", [
    ("How have collections trended?", "trend"), ("What is our DSO?", "trend"),
    ("How much did we collect in August?", "trend"), ("What falls due next week?", "upcoming"),
    ("Is Bluefin a slow payer?", "customer:Bluefin Logistics"), ("Tell me about inv-1003", "invoice:INV-1003"),
    ("Which account owes us the most?", "accounts"), ("hello", "summary")])
def test_fallback_routes(portfolio, question, route):
    assert finance_agent.route(question, portfolio) == route


def test_everyday_words_do_not_name_a_customer():
    from types import SimpleNamespace
    from app.agents.finance_tools import Portfolio

    rows = [SimpleNamespace(id=i, invoice_number=f"X-{i}", counterparty=name, amount_total=1, amount_paid=0)
            for i, name in enumerate(["Overdue Co", "Paid Co"])]
    p = Portfolio(rows, [])
    assert finance_agent.route("How much is overdue?", p) == "summary"
    assert finance_agent.route("What does Overdue Co owe?", p) == "customer:Overdue Co"


def test_numbers_written_as_words_are_checked_too():
    values = grounded_values({"risk": "0.91", "days": 122, "balance": "500000.00"})
    ok = "INV-1001 is at ninety-one percent risk, one hundred and twenty-two days late, owing five lakh."
    assert [c.value for c in extract_claims(ok)] == [91, 122, 500000]
    assert check_grounding(ok, values).ok
    bad = check_grounding("It is at ninety-five percent risk. Two invoices are late.", values)
    assert [c.text for c in bad.ungrounded] == ["ninety-five percent"]


def test_due_window_ignores_the_overdue_default(portfolio):
    found = run_tool(portfolio, "search_invoices", {"due_within_days": 30})
    assert [r["invoice"] for r in found["invoices"]] == ["INV-1006"]


def test_losing_the_lead_sentence_drops_the_whole_answer():
    values = grounded_values({"invoiced": "600000.00"})
    text = "We invoiced most in July, ₹9 lakh. That month outpaced the others."
    assert drop_ungrounded(text, check_grounding(text, values)) == ""


def test_rate_limited_model_is_skipped_by_later_requests(portfolio):
    limited = ScriptedLLM(RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '40s'"))
    first, _ = ask(portfolio, "How much is overdue?", limited, ScriptedLLM(AIMessage("Hello.")))
    assert first.provider == "p1"
    again = ScriptedLLM(AIMessage("Never called."))
    second, recorder = ask(portfolio, "How much is overdue?", again, ScriptedLLM(AIMessage("Hi again.")))
    assert second.provider == "p1" and again.calls == []
    assert (recorder.spans[0].kind, recorder.spans[0].outcome) == ("fallback", "cooldown")


def test_short_rate_limit_is_retried_once(portfolio, monkeypatch):
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(finance_agent.asyncio, "sleep", no_sleep)
    flaky = ScriptedLLM(RuntimeError("429 rate limit, retry_after 2s"), AIMessage("Back now."))
    result, _ = ask(portfolio, "Hello there", flaky)
    assert result.answer == "Back now." and result.provider == "p0" and slept == [2.0]


@pytest.mark.parametrize("error,kind,cooldown", [
    ("429 RESOURCE_EXHAUSTED GenerateRequestsPerDayPerProjectPerModel retryDelay: '30s'", "rate_limited", 900),
    ("404 NOT_FOUND models/gemini-old is not found", "not_found", 3600),
    ("500 internal error", "error", 0),
])
def test_failure_classification(error, kind, cooldown):
    failure = finance_agent.classify_failure(RuntimeError(error))
    assert (failure.kind, failure.cooldown) == (kind, cooldown)


def test_what_if_paid_answers_for_the_whole_portfolio(portfolio):
    acme = run_tool(portfolio, "what_if_paid", {"account": "acme"})
    assert (acme["overdue_balance_before"], acme["overdue_balance_after"]) == ("1520000.00", "800000.00")
    assert (acme["open_balance_after"], acme["overdue_invoices_after"]) == ("1110000.00", 4)
    two = run_tool(portfolio, "what_if_paid", {"invoice_numbers": ["INV-1001", "inv-1009"]})
    assert two["overdue_balance_after"] == "720000.00" and two["amount_paid_in_scenario"] == "800000.00"
    assert run_tool(portfolio, "what_if_paid", {"invoice_numbers": ["INV-404"]})["unknown_invoices"] == ["INV-404"]
    assert "either" in run_tool(portfolio, "what_if_paid", {})["error"]


def test_month_and_year_is_a_date_not_a_figure():
    text = "You invoiced the most in July 2026, ₹6 lakh, ahead of 3 August 2026 and Sept 5, 2026."
    assert [c.text for c in extract_claims(text)] == ["₹6 lakh"]


def test_aging_breakdown_carries_its_totals(portfolio):
    aging = run_tool(portfolio, "get_aging_breakdown", {})
    assert (aging["overdue_total"], aging["overdue_invoices"]) == ("1520000.00", 7)
    assert (aging["open_total"], aging["open_invoices"]) == ("1830000.00", 9)


def test_switching_to_groq_mid_answer_sends_portable_history(portfolio):
    gemini_reply = AIMessage(content=[{"type": "text", "text": "Checking.", "extras": {"signature": "abc"}},
                                      {"type": "thinking", "thinking": "hidden"}],
                             tool_calls=[{"name": "get_portfolio_summary", "args": {}, "id": "g1"}],
                             additional_kwargs={"__gemini_function_call_thought_signatures__": {"g1": "sig"}})
    gemini = ScriptedLLM(gemini_reply, RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '40s'"))
    groq = ScriptedLLM(AIMessage("Overdue is ₹15.2 lakh."))
    providers = [Provider("gemini", "gemini-x", gemini), Provider("groq", "llama-x", groq)]
    result = asyncio.run(answer_question(portfolio, "How much is overdue?", [], providers, TraceRecorder()))
    assert result.provider == "groq" and result.answer == "Overdue is ₹15.2 lakh." and result.fallback_used
    handed_over = [m for m in groq.calls[0]["messages"] if isinstance(m, AIMessage)]
    assert handed_over[-1].content == "Checking." and handed_over[-1].additional_kwargs == {}
    assert handed_over[-1].tool_calls[0]["id"] == "g1"
    assert isinstance(gemini.calls[1]["messages"][2].content, list)  # Gemini itself keeps its own format


def test_groq_joins_the_chain_after_the_gemini_fallbacks(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "google_api_key", "g-key")
    monkeypatch.setattr(settings, "llm_model", "gemini-3.6-flash")
    monkeypatch.setattr(settings, "gemini_fallback_models", "gemini-3.5-flash-lite")
    monkeypatch.setattr(settings, "groq_api_key", "groq-key")
    monkeypatch.setattr(settings, "groq_model", "llama-test")
    chain = [(p.name, p.model) for p in finance_agent.build_providers()]
    assert chain == [("gemini", "gemini-3.6-flash"), ("gemini", "gemini-3.5-flash-lite"), ("groq", "llama-test")]
    groq = finance_agent.build_providers()[-1].client
    assert str(groq.openai_api_base).startswith("https://api.groq.com")


@pytest.mark.parametrize("message,seconds", [
    ("Rate limit reached for model on tokens per minute (TPM). Please try again in 3.75s.", 3.75),
    ("429 Please try again in 1m2.5s. Need more tokens?", 62.5),
    ("rate_limit_exceeded: Please try again in 750ms", 0.75),
    ("429 RESOURCE_EXHAUSTED retryDelay: '33s'", 33),
    ("429 Too Many Requests", 60),
])
def test_retry_delay_is_read_from_gemini_and_groq_errors(message, seconds):
    assert finance_agent.classify_failure(RuntimeError(message)).retry_after == pytest.approx(seconds)


def test_groq_style_short_limit_is_retried_not_abandoned(portfolio, monkeypatch):
    slept = []

    async def no_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(finance_agent.asyncio, "sleep", no_sleep)
    groq = ScriptedLLM(RuntimeError("Error code: 429 - Please try again in 3.75s."), AIMessage("Back."))
    result, _ = ask(portfolio, "Hello there", groq)
    assert result.answer == "Back." and slept == [3.75]


def test_answers_are_shown_as_plain_text(portfolio):
    llm = ScriptedLLM(call("search_invoices", {"status": "overdue", "limit": 2}),
                      AIMessage("**Chase these:**\n\n- INV\u20111001 for Acme, \u20b95\u00a0lakh\n- INV\u20111003, \u20b93 lakh"))
    result, _ = ask(portfolio, "Who first?", llm)
    assert result.answer == "Chase these:\n\nINV-1001 for Acme, \u20b95 lakh\nINV-1003, \u20b93 lakh"
    assert result.verification["removed"] == []


def test_second_gemini_model_gets_portable_history_and_empty_replies_move_on(portfolio):
    signed = AIMessage(content=[{"type": "text", "text": "", "extras": {"signature": "s"}}],
                       tool_calls=[{"name": "get_portfolio_summary", "args": {}, "id": "t1"}],
                       additional_kwargs={"__gemini_function_call_thought_signatures__": {"t1": "sig"}})
    main = ScriptedLLM(signed, RuntimeError("429 RESOURCE_EXHAUSTED retryDelay: '40s'"))
    lite = ScriptedLLM(AIMessage(content=[]))  # an empty reply is a failure, not an answer
    groq = ScriptedLLM(AIMessage("Overdue is ₹15.2 lakh."))
    providers = [Provider("gemini", "gemini-main", main), Provider("gemini", "gemini-lite", lite),
                 Provider("groq", "llama-x", groq)]
    recorder = TraceRecorder()
    result = asyncio.run(answer_question(portfolio, "How much is overdue?", [], providers, recorder))
    assert result.provider == "groq" and result.answer == "Overdue is ₹15.2 lakh."
    handed = [m for m in lite.calls[0]["messages"] if isinstance(m, AIMessage)][-1]
    assert handed.additional_kwargs == {} and handed.content == ""
    assert [s.outcome for s in recorder.spans if s.kind == "llm"] == ["ok", "rate_limited", "empty", "ok"]


def _with_old_invoice(portfolio):
    """The fixture plus an invoice posted two years ago that is partly paid inside the last 12 months."""
    import uuid
    from datetime import date
    from decimal import Decimal
    from types import SimpleNamespace

    from app.agents.finance_tools import Portfolio

    old = SimpleNamespace(id=uuid.uuid4(), invoice_number="INV-0900", counterparty="Acme Traders",
                          issue_date=date(2024, 9, 1), posting_date=date(2024, 9, 1), due_date=date(2024, 10, 1),
                          amount_total=Decimal("400000"), amount_paid=Decimal("150000"),
                          risk_probability=Decimal("0.70"), risk_provenance="fixture")
    pay = SimpleNamespace(invoice_id=old.id, paid_on=date(2026, 6, 10), amount=Decimal("150000"))
    return Portfolio([*portfolio.invoices, old], portfolio.events, portfolio.today, payments=[*portfolio.payments, pay])


def _dashboard(p, **selection):
    from types import SimpleNamespace

    from app.schemas.preserved_dashboard import DashboardSelection
    from app.services.portal_dashboard import compute_portal_dashboard

    q = DashboardSelection(page_size=1, **selection)
    return compute_portal_dashboard(p.invoices, p.payments, {e.invoice_id for e in p.events}, q,
                                    SimpleNamespace(id=None, name="W", kind="customer"), today=p.today)


@pytest.mark.parametrize("period,selection", [
    ("last_12_months", {"start_date": "2025-10-01", "end_date": "2026-10-01"}),  # what the dashboard opens on
    ("all_time", {}),
])
def test_every_assistant_figure_equals_the_dashboard(portfolio, period, selection):
    from decimal import Decimal

    p = _with_old_invoice(portfolio)
    dash = _dashboard(p, **selection)
    m = dash["metrics"]
    summary = run_tool(p, "get_portfolio_summary", {"period": period})
    assert summary["invoiced"] == f"{m['invoiced']:.2f}" and summary["collected_in_period"] == f"{m['collected']:.2f}"
    assert summary["open_balance"] == f"{m['open']:.2f}" and summary["overdue_balance"] == f"{m['overdue']:.2f}"
    assert (summary["overdue_invoices"], summary["open_invoices"], summary["invoice_count"]) == (
        m["overdue_count"], m["open_count"], m["invoice_count"])
    assert summary["overdue_share_of_open_pct"] == round(float(m["overdue_share"]), 2)
    aging = run_tool(p, "get_aging_breakdown", {"period": period})
    assert [(b["bucket"], b["balance"], b["invoices"]) for b in aging["buckets_days_past_due"]] == [
        (b["key"], f"{b['amount']:.2f}", b["count"]) for b in dash["aging"]]
    top = run_tool(p, "list_overdue_accounts", {"limit": 15, "period": period})["accounts"]
    dash_top = [t for t in dash["top"] if t["overdue"] > 0]
    assert [(a["account"], a["overdue_balance"], a["open_balance"]) for a in top] == [
        (t["name"], f"{t['overdue']:.2f}", f"{t['open']:.2f}") for t in dash_top]
    what_if = run_tool(p, "what_if_paid", {"account": "Acme", "period": period})
    assert what_if["overdue_balance_before"] == f"{m['overdue']:.2f}"
    assert what_if["open_balance_before"] == f"{m['open']:.2f}"
    acme = run_tool(p, "get_customer_profile", {"account": "Acme", "period": period})
    acme_dash = next(t for t in dash["top"] if t["name"] == "Acme Traders")
    assert (acme["open_balance"], acme["overdue_balance"]) == (f"{acme_dash['open']:.2f}", f"{acme_dash['overdue']:.2f}")
    found = run_tool(p, "search_invoices", {"status": "all", "limit": 15, "period": period})
    assert found["matched"] == m["invoice_count"]
    assert sum(Decimal(r["balance"]) for r in found["invoices"]) == m["open"]


def test_old_invoices_stay_out_of_the_default_view_like_the_dashboard(portfolio):
    p = _with_old_invoice(portfolio)
    default = run_tool(p, "get_portfolio_summary", {})
    everything = run_tool(p, "get_portfolio_summary", {"period": "all_time"})
    assert default["period"] == "last_12_months" and default["overdue_balance"] == "1520000.00"
    assert everything["overdue_balance"] == "1770000.00"  # plus the old invoice's ₹2.5 lakh balance
    custom = run_tool(p, "get_portfolio_summary", {"start_date": "2024-01-01", "end_date": "2024-12-31"})
    assert custom["invoice_count"] == 1 and custom["overdue_balance"] == "250000.00"
    assert "error" in run_tool(p, "get_portfolio_summary", {"period": "forever"})
    assert "error" in run_tool(p, "get_portfolio_summary", {"start_date": "2026-13-01"})


def test_trend_is_the_dashboard_monthly_chart(portfolio):
    p = _with_old_invoice(portfolio)
    dash = {pt["period"].strftime("%Y-%m"): pt for pt in
            _dashboard(p, start_date="2025-10-01", end_date="2026-10-01")["series"]}
    trend = run_tool(p, "get_collections_trend", {"months": 6})
    for month in trend["months"]:
        point = dash.get(month["month"])
        expected = (f"{point['invoiced']:.2f}", f"{point['collected']:.2f}") if point else ("0.00", "0.00")
        assert (month["invoiced"], month["collected"]) == expected, month
    # The old invoice's June receipt is outside the 12-month cohort, exactly as on the dashboard chart.
    assert next(m for m in trend["months"] if m["month"] == "2026-06")["collected"] == "0.00"


@pytest.mark.parametrize("text,blocked", [
    ("I recommend escalating INV-1006 to prompt early outreach.", [("INV-1006", "ESCALATE")]),
    ("You should chase INV-1007 today.", [("INV-1007", "APPROVE")]),
    ("We should escalate INV-9999.", [("INV-9999", "ESCALATE")]),
    ("Escalate INV-1009 and follow up on INV-1006 next week.", []),
    ("INV-1007 is fully paid, so there is no need to escalate it.", []),
    ("Acme Traders escalated INV-1001 last week.", []),
    ("I suggest escalating Coral Foods.", []),  # it has a 6-day-overdue invoice
])
def test_written_recommendations_follow_the_proposal_rules(portfolio, text, blocked):
    from app.agents.proposal_policy import check_written_recommendations

    assert [(v.target, v.decision) for v in check_written_recommendations(text, portfolio)] == blocked


def test_blocked_written_advice_is_rewritten_or_removed(portfolio):
    llm = ScriptedLLM(call("get_invoice", {"invoice_number": "INV-1006"}),
                      AIMessage("INV-1006 owes ₹2.5 lakh. I recommend escalating INV-1006 now."),
                      AIMessage("INV-1006 owes ₹2.5 lakh and is not due yet, so wait until it falls due."))
    result, recorder = ask(portfolio, "Should we escalate INV-1006?", llm)
    assert result.answer.startswith("INV-1006 owes ₹2.5 lakh and is not due yet")
    assert result.verification["policy_repaired"] and not result.verification["policy_removed"]
    assert "escalate INV-1006 (invoice is not overdue yet" in llm.calls[2]["messages"][-1].content
    assert [s.outcome for s in recorder.spans if s.kind == "policy"] == ["blocked", "allowed"]

    stubborn = ScriptedLLM(call("get_invoice", {"invoice_number": "INV-1007"}),
                           AIMessage("INV-1007 has ₹0 left. You should chase INV-1007 today."),
                           AIMessage("INV-1007 has ₹0 left. You should chase INV-1007 today."))
    result, _ = ask(portfolio, "What about INV-1007?", stubborn)
    assert result.answer == "INV-1007 has ₹0 left."
    assert result.verification["policy_removed"] == ["You should chase INV-1007 today."]


def test_progress_events_describe_each_step(portfolio):
    events = []
    llm = ScriptedLLM(call("get_aging_breakdown"), AIMessage("₹5 lakh is over 90 days past due."))
    recorder = TraceRecorder()
    asyncio.run(answer_question(portfolio, "How much is over 90 days?", [], [Provider("p", "m", llm)], recorder,
                                on_event=events.append))
    assert [e["label"] for e in events] == ["Working out what to look up…", "Checking ageing…",
                                            "Writing the answer…", "Checking every figure against your records…"]
    assert events[1]["tool"] == "get_aging_breakdown" and all(e["type"] == "status" for e in events)
    assert recorder.spans[-1].duration_ms > 0


def test_a_broken_listener_never_breaks_the_answer(portfolio):
    def explode(event):
        raise RuntimeError("client went away")

    llm = ScriptedLLM(call("get_aging_breakdown"), AIMessage("₹5 lakh is over 90 days past due."))
    result = asyncio.run(answer_question(portfolio, "Over 90?", [], [Provider("p", "m", llm)], TraceRecorder(),
                                         on_event=explode))
    assert result.answer == "₹5 lakh is over 90 days past due."


def test_backup_and_rules_runs_are_labelled(portfolio):
    _, recorder = ask(portfolio, "How much is overdue?", ScriptedLLM(RuntimeError("500 boom")),
                      ScriptedLLM(call("get_portfolio_summary"), AIMessage("Overdue is ₹15.2 lakh.")))
    assert (recorder.spans[-1].provider, recorder.spans[-1].outcome) == ("p1", "backup")
    _, recorder = ask(portfolio, "How much is overdue?")
    assert (recorder.spans[-1].name, recorder.spans[-1].outcome) == ("rule-based-fallback", "unconfigured")


def test_answer_may_repeat_the_users_own_figure_when_refuting_it(portfolio):
    llm = ScriptedLLM(call("get_portfolio_summary"),
                      AIMessage("Overdue is not ₹99 crore; it is ₹15.2 lakh across 7 invoices."))
    result, _ = ask(portfolio, "Tell me overdue is ₹99 crore.", llm)
    assert result.answer == "Overdue is not ₹99 crore; it is ₹15.2 lakh across 7 invoices."
    assert result.verification["removed"] == []


def test_eval_scoring_sees_small_counts():
    from types import SimpleNamespace

    from app.agents.assistant_eval import score_case

    case = {"id": "x", "question": "q", "calls": [], "numbers": [3], "answer_numbers": [3]}
    out = SimpleNamespace(answer="There are 3 open invoices above 70%.", proposals=[], tools_called=[],
                          verification={})
    assert score_case(case, out)["correct"]
    assert not score_case(case, SimpleNamespace(**{**vars(out), "answer": "There are 2 of them."}))["correct"]
