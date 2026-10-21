"""The assistant's figures equal the dashboard's, through the real routes on disposable PostgreSQL."""
import json
import uuid
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient
from langchain_core.messages import AIMessage, ToolMessage
from sqlalchemy import delete

from app.api import portal as portal_api
from app.core.db import async_session_factory, engine
from app.main import app
from app.models.portal import PortalUser, PortalWorkspace


class SummaryThenAnswer:
    """Calls one tool, then answers; keeps the tool result so the test can compare it."""
    max_output_tokens = 1024

    def __init__(self, tool, args):
        self.tool, self.args, self.result = tool, args, None

    def bind_tools(self, specs):
        return SimpleNamespace(ainvoke=self.ainvoke)

    async def ainvoke(self, messages):
        if isinstance(messages[-1], ToolMessage):
            self.result = json.loads(messages[-1].content)
            return AIMessage("Done.")
        return AIMessage(content="", tool_calls=[{"name": self.tool, "args": self.args, "id": "t1"}])


def invoice(number, account, posted_days_ago, term, total, paid=0, paid_days_ago=None):
    today = date.today()
    posted = today - timedelta(days=posted_days_ago)
    row = {"invoice_number": number, "counterparty": account, "issue_date": str(posted),
           "posting_date": str(posted), "due_date": str(posted + timedelta(days=term)),
           "amount_total": f"{total}.00", "amount_paid": f"{paid}.00"}
    if paid:
        row["payment_date"] = str(today - timedelta(days=paid_days_ago))
    return row


@pytest.mark.asyncio
async def test_assistant_figures_match_the_dashboard_api(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "false")
    monkeypatch.setattr("app.agents.llm_client.gemini_fallback_chats", lambda timeout=None: [])
    await engine.dispose()
    tag = uuid.uuid4().hex
    batch = {"invoices": [
        invoice("P-1", "North Mills", 400, 30, 90000, 40000, 300),   # posted over a year ago: outside the default view
        invoice("P-2", "North Mills", 120, 30, 250000),
        invoice("P-3", "South Foods", 75, 45, 180000, 80000, 10),
        invoice("P-4", "South Foods", 20, 30, 60000),
        invoice("P-5", "East Steel", 200, 30, 300000, 300000, 150),
        invoice("P-6", "East Steel", 45, 15, 125000),
    ]}
    workspace_id = user_id = None
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            reg = await client.post("/portal/register", json={
                "email": f"parity-{tag}@example.invalid", "full_name": "Parity Owner", "company": "Parity Co",
                "password": "A-strong-unique-long-password-37"})
            assert reg.status_code == 201, reg.text
            headers = {"Authorization": f"Bearer {reg.json()['token']}"}
            me = (await client.get("/portal/me", headers=headers)).json()
            workspace_id, user_id = uuid.UUID(me["workspace_id"]), uuid.UUID(me["user_id"])
            assert (await client.post("/portal/import", json=batch, headers=headers)).status_code == 201

            today = date.today()
            # The dashboard opens on invoices posted in the last 12 months (ReferenceFrame's 12M preset).
            default = {"start_date": str(today.replace(year=today.year - 1)), "end_date": str(today)}
            overdue_by_view = {}
            for period, query in (("last_12_months", default), ("all_time", {})):
                dash = (await client.get("/portal/dashboard", headers=headers, params=query)).json()
                overdue_by_view[period] = dash["metrics"]["overdue"]
                m = dash["metrics"]
                for tool, check in (
                    ("get_portfolio_summary", lambda r: (
                        r["invoiced"], r["collected_in_period"], r["open_balance"], r["overdue_balance"],
                        r["overdue_invoices"], r["invoice_count"])),
                    ("get_aging_breakdown", lambda r: [(b["bucket"], b["balance"], b["invoices"])
                                                       for b in r["buckets_days_past_due"]]),
                ):
                    llm = SummaryThenAnswer(tool, {"period": period})
                    monkeypatch.setattr("app.agents.llm_client._llm_chat", lambda llm=llm: (llm, "fake"))
                    portal_api._ASSISTANT_CALLS.clear()
                    reply = await client.post("/portal/assistant", headers=headers, json={"question": "Figures?"})
                    assert reply.status_code == 200, reply.text
                    expected = (
                        (m["invoiced"], m["collected"], m["open"], m["overdue"], m["overdue_count"],
                         m["invoice_count"]) if tool == "get_portfolio_summary"
                        else [(b["key"], b["amount"], b["count"]) for b in dash["aging"]])
                    as_money = lambda v: f"{float(v):.2f}" if isinstance(v, str) else v  # noqa: E731
                    got = check(llm.result)
                    normalise = (lambda t: tuple(as_money(x) for x in t)) if isinstance(got, tuple) else (
                        lambda rows: [(k, as_money(a), c) for k, a, c in rows])
                    assert normalise(got) == normalise(expected), (period, tool)
            # The year-old invoice (₹50,000 still open) is what separates the two views, on both sides.
            assert float(overdue_by_view["all_time"]) - float(overdue_by_view["last_12_months"]) == 50000
            assert llm.result["period"] == "all_time"
    finally:
        async with async_session_factory() as session:
            if workspace_id:
                await session.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            if user_id:
                await session.execute(delete(PortalUser).where(PortalUser.id == user_id))
            await session.commit()
        await engine.dispose()
