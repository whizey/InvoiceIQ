"""Invoice notes through real routes on disposable PostgreSQL: tenant isolation, validation and the assistant."""
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


class SearchThenAnswer:
    """Searches the notes once, keeps what the tool returned, then answers."""
    max_output_tokens = 1024

    def __init__(self):
        self.result = None

    def bind_tools(self, specs):
        return SimpleNamespace(ainvoke=self.ainvoke)

    async def ainvoke(self, messages):
        if isinstance(messages[-1], ToolMessage):
            self.result = json.loads(messages[-1].content)
            return AIMessage("Here is what the notes say.")
        return AIMessage(content="", tool_calls=[{"name": "search_notes", "args": {"query": "disputed delivery"},
                                                  "id": "s1"}])


@pytest.mark.asyncio
async def test_notes_are_workspace_scoped_validated_and_read_by_the_assistant(monkeypatch):
    monkeypatch.setenv("PUBLIC_READ_ONLY", "false")
    monkeypatch.setenv("INVOICEIQ_PORTAL_ENABLED", "true")
    monkeypatch.setenv("INVOICEIQ_PORTAL_SHOWCASE", "false")
    monkeypatch.setattr("app.agents.llm_client.gemini_fallback_chats", lambda timeout=None: [])
    await engine.dispose()
    tag, today = uuid.uuid4().hex, date.today()
    workspace_ids, user_ids, headers = [], [], []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            for name in ("Notes A", "Notes B"):
                reg = await client.post("/portal/register", json={
                    "email": f"{name[-1].lower()}-notes-{tag}@example.invalid", "full_name": "Notes Owner",
                    "company": name, "password": "A-strong-unique-long-password-37"})
                assert reg.status_code == 201, reg.text
                headers.append({"Authorization": f"Bearer {reg.json()['token']}"})
                me = (await client.get("/portal/me", headers=headers[-1])).json()
                workspace_ids.append(uuid.UUID(me["workspace_id"]))
                user_ids.append(uuid.UUID(me["user_id"]))
                posted = today - timedelta(days=50)
                assert (await client.post("/portal/import", headers=headers[-1], json={"invoices": [{
                    "invoice_number": f"N-{name[-1]}", "counterparty": f"{name} Customer",
                    "issue_date": str(posted), "posting_date": str(posted),
                    "due_date": str(posted + timedelta(days=30)), "amount_total": "5000.00",
                    "amount_paid": "0.00"}]})).status_code == 201
            ids = [(await client.get(f"/portal/invoices?q=N-{x}", headers=headers[i])).json()["invoices"][0]["id"]
                   for i, x in enumerate("AB")]

            # Saved notes are embedded for semantic search; if embedding fails the note still saves.
            import numpy as np
            from app.services import note_embeddings

            async def fake_embed(texts, task, **kwargs):
                return None if "offline" in texts[0] else [np.eye(note_embeddings.DIMENSIONS, dtype=np.float32)[0]]
            monkeypatch.setattr(note_embeddings, "available", lambda: True)
            monkeypatch.setattr(note_embeddings, "embed", fake_embed)
            added = await client.post("/portal/notes", headers=headers[0], json={
                "invoice_id": ids[0], "kind": "dispute", "body": "Customer disputes the delivery date."})
            assert added.status_code == 201, added.text
            assert added.json()["invoice"] == "N-A" and added.json()["sample"] is False
            unembedded = await client.post("/portal/notes", headers=headers[1], json={
                "invoice_id": ids[1], "kind": "internal", "body": "Embedding service offline today."})
            assert unembedded.status_code == 201
            async with async_session_factory() as session:
                from sqlalchemy import select
                from app.models.portal import PortalNote

                stored = {n.body: n for n in (await session.scalars(select(PortalNote).where(
                    PortalNote.workspace_id.in_(workspace_ids)))).all()}
            assert note_embeddings.unpack(stored["Customer disputes the delivery date."].embedding) is not None
            assert stored["Customer disputes the delivery date."].embedding_model == note_embeddings.model_name()
            assert stored["Embedding service offline today."].embedding is None
            # Workspace B can neither write to nor read A's invoice notes.
            assert (await client.post("/portal/notes", headers=headers[1], json={
                "invoice_id": ids[0], "kind": "internal", "body": "Sneaky note"})).status_code == 404
            assert (await client.get(f"/portal/notes?invoice_id={ids[0]}", headers=headers[1])).json()["notes"] == []
            assert [n["invoice"] for n in (await client.get("/portal/notes", headers=headers[1])).json()["notes"]] == [
                "N-B"]  # only its own note
            own = (await client.get(f"/portal/notes?invoice_id={ids[0]}", headers=headers[0])).json()["notes"]
            assert [n["body"] for n in own] == ["Customer disputes the delivery date."]
            # Validation and authentication.
            for bad in ({"kind": "gossip", "body": "Fine text"}, {"kind": "internal", "body": "  "},
                        {"kind": "internal", "body": "x" * 1001}):
                response = await client.post("/portal/notes", headers=headers[0], json={"invoice_id": ids[0], **bad})
                assert response.status_code == 422, bad
            assert (await client.get("/portal/notes")).status_code == 401
            assert (await client.post("/portal/notes", json={"invoice_id": ids[0], "kind": "internal",
                                                             "body": "No session"})).status_code == 401

            # The assistant reads only the asking workspace's notes.
            monkeypatch.setattr(note_embeddings, "available", lambda: False)  # keyword search for this part
            for index, expected in ((0, ["N-A"]), (1, [])):
                llm = SearchThenAnswer()
                monkeypatch.setattr("app.agents.llm_client._llm_chat", lambda llm=llm: (llm, "fake"))
                portal_api._ASSISTANT_CALLS.clear()
                reply = await client.post("/portal/assistant", headers=headers[index],
                                          json={"question": "Any disputed deliveries?"})
                assert reply.status_code == 200, reply.text
                assert [n["invoice"] for n in llm.result["notes"]] == expected
                assert reply.json()["tools_used"] == ["search_notes"]
    finally:
        async with async_session_factory() as session:
            for workspace_id in workspace_ids:
                await session.execute(delete(PortalWorkspace).where(PortalWorkspace.id == workspace_id))
            for user_id in user_ids:
                await session.execute(delete(PortalUser).where(PortalUser.id == user_id))
            await session.commit()
        await engine.dispose()
