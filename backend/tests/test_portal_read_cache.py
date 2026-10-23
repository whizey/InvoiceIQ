"""The portal read cache: shared showcase data is computed once, visitor data stays per visitor."""
import asyncio
import uuid
from types import SimpleNamespace

from app.api import portal


def test_shared_entries_serve_every_visitor_and_writes_invalidate(monkeypatch):
    monkeypatch.setattr(portal, "_cache", {})
    workspace = SimpleNamespace(id=uuid.uuid4(), kind="showcase")
    alice, bob = SimpleNamespace(token_digest="a"), SimpleNamespace(token_digest="b")
    builds = []

    def build(label):
        async def run():
            builds.append(label)
            return label
        return run

    async def scenario():
        assert await portal._cached(workspace, alice, "invoices", 1, build("list"), shared=True) == "list"
        assert await portal._cached(workspace, bob, "invoices", 1, build("list-again"), shared=True) == "list"
        assert await portal._cached(workspace, alice, "overview", None, build("alice")) == "alice"
        assert await portal._cached(workspace, bob, "overview", None, build("bob")) == "bob"  # never shared
        portal._bump(workspace.id)  # any write in the workspace
        assert await portal._cached(workspace, bob, "invoices", 1, build("fresh"), shared=True) == "fresh"

    asyncio.run(scenario())
    assert builds == ["list", "alice", "bob", "fresh"]


def test_ttl_expires_entries(monkeypatch):
    monkeypatch.setattr(portal, "_cache", {})
    workspace, visitor = SimpleNamespace(id=uuid.uuid4(), kind="customer"), SimpleNamespace(token_digest="x")
    clock = iter([100.0, 105.0, 131.0, 131.0])  # store, check after 5 s, check after 31 s, store again
    monkeypatch.setattr(portal, "time", SimpleNamespace(monotonic=lambda: next(clock)))  # the portal's clock only
    calls = []

    async def build():
        calls.append(1)
        return len(calls)

    async def scenario():
        assert await portal._cached(workspace, visitor, "reliability", 7, build, ttl=30) == 1
        assert await portal._cached(workspace, visitor, "reliability", 7, build, ttl=30) == 1  # 5 s later
        assert await portal._cached(workspace, visitor, "reliability", 7, build, ttl=30) == 2  # 31 s later

    asyncio.run(scenario())
