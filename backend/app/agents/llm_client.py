"""LLM provider abstraction with a deterministic fallback.

If GOOGLE_API_KEY or OPENAI_API_KEY is configured (backend/.env), diagnosis
and intervention use a real chat model via LangChain structured output.
Gemini is preferred when both keys are set. If neither key is set, a
deterministic rule-based fallback produces plausible results so the whole
graph is fully runnable and testable with zero external dependencies or
cost. Presence of a key is the switch — no separate feature flag.
"""

import asyncio
import logging
import os
import time
from typing import Any

from app.agents.prompts import (
    DIAGNOSIS_SYSTEM_PROMPT,
    INTERVENTION_SYSTEM_PROMPT,
    build_diagnosis_prompt,
    build_intervention_prompt,
    build_voice_system_prompt,
    build_voice_turn_prompt,
)
from app.agents.schemas import DiagnosisResult, InterventionRecommendation, VoiceTurnResult
from app.agents.guardrails import is_untrusted_instruction, validate_model_output, redact_text, grounded_claims, minimal_diagnosis
from app.core.config import settings
from app.core.metrics import record_llm_call, record_llm_fallback

MAX_VOICE_TURNS = 4

# A real provider call that hangs, rate-limits, or otherwise errors must
# never surface as a 500 to the caller — it falls back to the deterministic
# rule-based path instead, same as "no key configured" does. This bounds
# how long a caller ever waits on a real call before that fallback kicks in.
LLM_TIMEOUT_SECONDS = 15

FALLBACK_MODEL_NAME = "rule-based-fallback"
GEMINI_DEFAULT_MODEL = "gemini-2.5-flash"
OPENAI_DEFAULT_MODEL = "gpt-4o-mini"

logger = logging.getLogger("app.llm")


def _structured_client(llm, schema):
    try:
        return llm.with_structured_output(schema)
    except Exception:
        logger.warning("LLM structured client initialization failed; using deterministic fallback")
        return None


def configured_llm() -> tuple[str, str]:
    """Return (provider, model_name) without constructing a client."""
    if settings.google_api_key:
        return "gemini", settings.llm_model or GEMINI_DEFAULT_MODEL
    if settings.groq_api_key and settings.groq_model:
        return "groq", settings.groq_model
    if settings.openai_api_key:
        return "openai", settings.llm_model or OPENAI_DEFAULT_MODEL
    return "fallback", FALLBACK_MODEL_NAME


def _llm_chat() -> tuple[Any, str] | tuple[None, None]:
    try:
        return _build_llm_chat()
    except Exception:
        # SDK initialization errors can contain credentials or request details.
        logger.warning("LLM client initialization failed; using deterministic fallback")
        return None, None


def _build_llm_chat() -> tuple[Any, str] | tuple[None, None]:
    provider, model = configured_llm()
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        sampling = {} if model.removeprefix("models/") in {"gemini-3.5-flash-lite", "gemini-3.6-flash"} else {"temperature": 0}
        return (
            ChatGoogleGenerativeAI(
                model=model,
                api_key=settings.google_api_key,
                **sampling,
                max_output_tokens=1024,
                max_retries=0,
                timeout=LLM_TIMEOUT_SECONDS,
            ),
            model,
        )
    if provider in {"openai", "groq"}:
        from langchain_openai import ChatOpenAI

        options = {}
        if provider == "groq":
            options["base_url"] = "https://api.groq.com/openai/v1"
        return (
            ChatOpenAI(model=model, api_key=settings.groq_api_key if provider == "groq" else settings.openai_api_key,
                       temperature=0, max_tokens=1024, max_retries=0, timeout=LLM_TIMEOUT_SECONDS, **options),
            model,
        )
    return None, None


def _log_llm_call(stage: str, provider: str, model_name: str, *, llm_called: bool) -> None:
    if not llm_called:
        record_llm_fallback(stage, provider)
    logger.info(
        "llm %s provider=%s model=%s called=%s",
        stage,
        provider,
        model_name,
        llm_called,
        extra={
            "stage": stage,
            "provider": provider,
            "model": model_name,
            "llm_called": llm_called,
        },
    )


async def _invoke_structured_llm(
    structured_llm: Any, messages: list, *, stage: str, provider: str, model_name: str, schema: type, invoice_context: dict | None = None
) -> Any | None:
    """Returns the parsed structured result, or None on any failure (rate
    limit, timeout, network error, malformed response) — callers fall back
    to their rule-based path on None rather than letting the exception
    surface as a 500. Every other optional integration in this app (Kafka,
    Redis, Resend, Sarvam) fails the same way: logged, never raised."""
    try:
        if structured_llm is None:
            return None
        started = time.perf_counter()
        result = await asyncio.wait_for(structured_llm.ainvoke(messages), timeout=LLM_TIMEOUT_SECONDS)
    except Exception:
        record_llm_call(stage, provider, "error", time.perf_counter() - started)
        logger.warning(
            "llm %s call failed (provider=%s model=%s); falling back to rule-based",
            stage,
            provider,
            model_name,
        )
        return None
    validated = validate_model_output(result, schema)
    if validated is not None and not grounded_claims(validated, invoice_context or {}):
        validated = None
    if validated is None:
        record_llm_call(stage, provider, "invalid", time.perf_counter() - started)
        logger.warning("llm %s returned invalid structured output; using fallback", stage)
        return None
    record_llm_call(stage, provider, "success", time.perf_counter() - started)
    _log_llm_call(stage, provider, model_name, llm_called=True)
    if stage in {"diagnose", "recommend"}:
        from app.agents.recordings import PROMPT_VERSION, digest, prompt_hash, record_response, set_provenance
        provenance = {"source": "provider_call", "prompt_version": PROMPT_VERSION,
            "prompt_sha256": prompt_hash(stage, messages), "provider": provider,
            "model": model_name, "response_sha256": digest(validated.model_dump(mode="json"))}
        if os.getenv("LLM_RECORDING_ENABLED", "").lower() == "true":
            try:
                provenance["private_recording"] = record_response(stage, messages, provider, model_name, validated)
            except FileExistsError:
                provenance["recording_status"] = "existing_recording_preserved"
        set_provenance(provenance)
    return validated


async def _phase7_original_diagnose(invoice_context: dict, customer_context: dict) -> tuple[DiagnosisResult, str]:
    from app.agents.recordings import minimal_context, replay_response, set_provenance
    set_provenance({"source": "deterministic_fallback", "provider": "none"})
    safe_invoice, safe_customer = minimal_context(invoice_context, customer_context)
    from langchain_core.messages import HumanMessage, SystemMessage
    messages = [SystemMessage(content=DIAGNOSIS_SYSTEM_PROMPT),
                HumanMessage(content=build_diagnosis_prompt(safe_invoice, safe_customer))]
    if os.getenv("DEMO_SYNTHETIC", "").lower() == "true":
        return _rule_based_diagnosis(invoice_context, customer_context), FALLBACK_MODEL_NAME
    if os.getenv("PUBLIC_READ_ONLY", "").lower() == "true" or os.getenv("LLM_MODE") == "recorded":
        result, model = replay_response("diagnose", messages, DiagnosisResult)
        if not grounded_claims(result, invoice_context):
            raise ValueError("Recorded financial claims lack authorized evidence")
        return result, model
    provider, _configured_model = configured_llm()
    llm, model_name = _llm_chat()
    if llm is not None and not is_untrusted_instruction((invoice_context, customer_context)):
        from langchain_core.messages import HumanMessage, SystemMessage

        structured_llm = _structured_client(llm, DiagnosisResult)
        result = await _invoke_structured_llm(
            structured_llm, messages, stage="diagnose", provider=provider, model_name=model_name, schema=DiagnosisResult, invoice_context=invoice_context
        )
        if result is not None:
            return result, model_name

    _log_llm_call("diagnose", provider, FALLBACK_MODEL_NAME, llm_called=False)
    return _rule_based_diagnosis(invoice_context, customer_context), FALLBACK_MODEL_NAME


async def _phase7_original_recommend(
    invoice_context: dict, customer_context: dict, diagnosis: dict, reminder_count: int
) -> tuple[InterventionRecommendation, str]:
    from app.agents.recordings import minimal_context, replay_response, set_provenance
    set_provenance({"source": "deterministic_fallback", "provider": "none"})
    safe_invoice, safe_customer = minimal_context(invoice_context, customer_context)
    from langchain_core.messages import HumanMessage, SystemMessage
    messages = [SystemMessage(content=INTERVENTION_SYSTEM_PROMPT),
        HumanMessage(content=build_intervention_prompt(safe_invoice, safe_customer, minimal_diagnosis(diagnosis), reminder_count))]
    if os.getenv("DEMO_SYNTHETIC", "").lower() == "true":
        return _rule_based_intervention(invoice_context, reminder_count), FALLBACK_MODEL_NAME
    if os.getenv("PUBLIC_READ_ONLY", "").lower() == "true" or os.getenv("LLM_MODE") == "recorded":
        result, model = replay_response("recommend", messages, InterventionRecommendation)
        if not grounded_claims(result, invoice_context):
            raise ValueError("Recorded financial claims lack authorized evidence")
        return result, model
    provider, _configured_model = configured_llm()
    llm, model_name = _llm_chat()
    if llm is not None and not is_untrusted_instruction((invoice_context, customer_context, diagnosis)):
        from langchain_core.messages import HumanMessage, SystemMessage

        structured_llm = _structured_client(llm, InterventionRecommendation)
        result = await _invoke_structured_llm(
            structured_llm, messages, stage="recommend", provider=provider, model_name=model_name, schema=InterventionRecommendation, invoice_context=invoice_context
        )
        if result is not None:
            return result, model_name

    _log_llm_call("recommend", provider, FALLBACK_MODEL_NAME, llm_called=False)
    return _rule_based_intervention(invoice_context, reminder_count), FALLBACK_MODEL_NAME


def _rule_based_diagnosis(invoice_context: dict, customer_context: dict) -> DiagnosisResult:
    days_overdue = invoice_context["days_overdue"]
    num_late = customer_context["num_prior_late_payments"]
    num_on_time = customer_context["num_prior_on_time_payments"]
    promise = customer_context.get("promise_to_pay")

    if promise and promise["status"] == "BROKEN":
        return DiagnosisResult(
            diagnosis="Customer broke a promised payment date",
            reason=(
                f"Promised ₹{promise['promised_amount']:,.2f} by {promise['promised_date']}, "
                f"which passed without payment."
            ),
            recommended_priority="high",
        )

    if num_late >= 2 and num_late > num_on_time:
        return DiagnosisResult(
            diagnosis="Recurring late-payment pattern",
            reason=f"Customer paid late in {num_late} of the last {num_late + num_on_time} invoices.",
            recommended_priority="high" if days_overdue > 45 else "medium",
        )
    if days_overdue > 45:
        return DiagnosisResult(
            diagnosis="Significantly overdue with limited resolution",
            reason=f"Invoice is {days_overdue} days overdue.",
            recommended_priority="high",
        )
    return DiagnosisResult(
        diagnosis="Isolated late payment",
        reason="Available history does not establish the cause of this overdue payment.",
        recommended_priority="low",
    )


def _rule_based_intervention(invoice_context: dict, reminder_count: int) -> InterventionRecommendation:
    if invoice_context["days_overdue"] > 45 and invoice_context["amount_total"] >= 1_000_000:
        return InterventionRecommendation(
            action="ESCALATE", rationale="High-value invoice significantly overdue."
        )
    if reminder_count == 0:
        return InterventionRecommendation(
            action="SEND_EMAIL", rationale="First reminder for a newly overdue invoice."
        )
    if reminder_count == 1:
        return InterventionRecommendation(
            action="SEND_PAYMENT_LINK",
            rationale="One reminder recorded; a payment link may lower friction to pay.",
        )
    return InterventionRecommendation(
        action="ESCALATE", rationale="Multiple reminders sent without payment."
    )


async def voice_turn(
    invoice_context: dict, customer_context: dict, transcript_so_far: list[dict], turn_number: int
) -> tuple[VoiceTurnResult, str]:
    """One turn of the Hinglish voice-call demo. Only ever *proposes*
    proposed_action — app/api/voice.py runs it through evaluate_policy()
    before anything executes, exactly like recommend() above."""
    if os.getenv("DEMO_SYNTHETIC", "").lower() == "true":
        return _rule_based_voice_turn(transcript_so_far, turn_number), FALLBACK_MODEL_NAME
    from app.agents.recordings import minimal_context
    safe_invoice, safe_customer = minimal_context(invoice_context, customer_context)
    provider, _configured_model = configured_llm()
    llm, model_name = _llm_chat()
    if llm is not None and not is_untrusted_instruction((invoice_context, customer_context, transcript_so_far)):
        from langchain_core.messages import HumanMessage, SystemMessage

        structured_llm = _structured_client(llm, VoiceTurnResult)
        messages = [
            SystemMessage(content=build_voice_system_prompt(turn_number, MAX_VOICE_TURNS)),
            HumanMessage(
                content=build_voice_turn_prompt(safe_invoice, safe_customer, [{"speaker": t["speaker"] if t["speaker"] in {"agent", "customer"} else "customer", "text": redact_text(t["text"])} for t in transcript_so_far], turn_number)
            ),
        ]
        result = await _invoke_structured_llm(
            structured_llm, messages, stage="voice_turn", provider=provider, model_name=model_name, schema=VoiceTurnResult, invoice_context=invoice_context
        )
        if result is not None:
            return result, model_name

    _log_llm_call("voice_turn", provider, FALLBACK_MODEL_NAME, llm_called=False)
    return _rule_based_voice_turn(transcript_so_far, turn_number), FALLBACK_MODEL_NAME


def _rule_based_voice_turn(transcript_so_far: list[dict], turn_number: int) -> VoiceTurnResult:
    if not transcript_so_far:
        return VoiceTurnResult(
            agent_line_hi=(
                "Namaste! Main AI Revenue Recovery se baat kar raha hoon. Aapka invoice abhi tak "
                "pending hai — kya aap iske baare mein baat kar sakte hain?"
            ),
            concluded=False,
            proposed_action="NONE",
        )

    last_reply = transcript_so_far[-1]["text"].lower()

    promise_words = ("haan", "promise", "kar dunga", "de dunga", "kal", "pay kar")
    payment_link_words = ("link", "kaise pay", "payment link", "how to pay", "upi")
    refusal_words = ("nahi", "can't", "cannot", "busy", "not now", "no money")

    if any(w in last_reply for w in promise_words):
        return VoiceTurnResult(
            agent_line_hi="Bahut accha! Main aapka promise note kar raha hoon. Dhanyavaad, shukriya!",
            concluded=True,
            proposed_action="TRACK_PROMISE_TO_PAY",
        )
    if any(w in last_reply for w in payment_link_words):
        return VoiceTurnResult(
            agent_line_hi="Theek hai, main aapko abhi ek payment link bhej raha hoon.",
            concluded=True,
            proposed_action="SEND_PAYMENT_LINK",
        )
    if turn_number >= MAX_VOICE_TURNS or any(w in last_reply for w in refusal_words):
        return VoiceTurnResult(
            agent_line_hi="Samajh gaya. Main isse hamare finance team ko escalate kar raha hoon.",
            concluded=True,
            proposed_action="ESCALATE",
        )
    return VoiceTurnResult(
        agent_line_hi="Samajh sakta hoon. Kya aap kal tak confirm kar sakte hain ki payment kab hoga?",
        concluded=False,
        proposed_action="NONE",
    )


# INVOICEIQ_PHASE7_INTEGRATION_V1
# Backward-compatible, opt-in agent routing.

from app.agents.phase7_runtime import (  # noqa: E402
    choose_route,
    run_routed,
)


async def diagnose(
    invoice_context: dict,
    customer_context: dict,
) -> tuple[DiagnosisResult, str]:

    route = choose_route(
        "diagnose",
        invoice_context,
        customer_context,
        configured_llm()[0],
    )

    if route.provider == "legacy":
        return await _phase7_original_diagnose(
            invoice_context,
            customer_context,
        )

    return await run_routed(
        stage="diagnose",
        route=route,
        fallback_model=FALLBACK_MODEL_NAME,
        local=lambda: _rule_based_diagnosis(
            invoice_context,
            customer_context,
        ),
        original=lambda: _phase7_original_diagnose(
            invoice_context,
            customer_context,
        ),
    )


async def recommend(
    invoice_context: dict,
    customer_context: dict,
    diagnosis: dict,
    reminder_count: int,
) -> tuple[InterventionRecommendation, str]:

    route = choose_route(
        "recommend",
        invoice_context,
        customer_context,
        configured_llm()[0],
    )

    if route.provider == "legacy":
        return await _phase7_original_recommend(
            invoice_context,
            customer_context,
            diagnosis,
            reminder_count,
        )

    return await run_routed(
        stage="recommend",
        route=route,
        fallback_model=FALLBACK_MODEL_NAME,
        local=lambda: _rule_based_intervention(
            invoice_context,
            reminder_count,
        ),
        original=lambda: _phase7_original_recommend(
            invoice_context,
            customer_context,
            diagnosis,
            reminder_count,
        ),
    )
