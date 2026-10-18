"""Safety failures must happen before any pytest subprocess or database writes."""
from copy import deepcopy
import json

import pytest
from scripts import test_database_safety as safety

URL = "postgresql+asyncpg://test:secret@127.0.0.1:55436/invoiceiq_test"


def container():
    return {"Id": "fixed-container-id", "Name": "/invoiceiq-ci-disposable-db",
            "State": {"Running": True},
            "Config": {"Image": "postgres:16-alpine", "Labels": {"com.invoiceiq.disposable": "true"}},
            "NetworkSettings": {"Ports": {"5432/tcp": [{"HostIp": "127.0.0.1", "HostPort": "55436"}]}},
            "HostConfig": {"Tmpfs": {"/var/lib/postgresql/data": "rw,noexec,nosuid,size=2147483648"}},
            "Mounts": [{"Type": "tmpfs"}]}


@pytest.mark.parametrize("raw", [URL.replace("55436", "5435"), URL.replace("127.0.0.1", "localhost"),
    URL.replace("invoiceiq_test", "recovery_db"), URL.replace("invoiceiq_test", "production_test"),
    URL + "?host=remote", URL.replace("postgresql+asyncpg", "sqlite")])
def test_unsafe_url_rejected(raw):
    with pytest.raises((RuntimeError, ValueError)):
        safety.validate_url(raw)


@pytest.mark.parametrize("field,value", [("Name", "/development"), ("State", {"Running": False}),
    ("Config", {"Image": "postgres:16", "Labels": {}}), ("Mounts", [{"Type": "bind"}]),
    ("Mounts", [{"Type": "volume"}]),
    ("HostConfig", {"Tmpfs": {}}),
    ("NetworkSettings", {"Ports": {"5432/tcp": [{"HostIp": "0.0.0.0", "HostPort": "55436"}]}})])
def test_unsafe_container_rejected(field, value):
    info = deepcopy(container())
    info[field] = value
    with pytest.raises(RuntimeError):
        safety.inspect_container(info)


@pytest.mark.parametrize("mismatch", [False, True])
def test_actual_connection_identity_must_match_container(monkeypatch, mismatch):
    identity = {"database": "invoiceiq_test", "username": "test", "port": 5432,
                "cluster": "123", "oid": "456", "started": "fixed"}
    monkeypatch.setattr(safety, "docker", lambda *args:
                        json.dumps([container()]) if args[0] == "inspect" else json.dumps(identity))

    class Transaction:
        async def __aenter__(self): pass
        async def __aexit__(self, *args): pass

    class Connection:
        def transaction(self, *, readonly):
            assert readonly
            return Transaction()
        async def fetchrow(self, sql):
            return dict(identity, cluster="other") if mismatch else identity
        async def close(self): pass

    async def connect(**kwargs):
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 55436
        assert kwargs["database"] == "invoiceiq_test"
        return Connection()

    monkeypatch.setattr(safety.asyncpg, "connect", connect)
    if mismatch:
        with pytest.raises(RuntimeError, match="identities do not match"):
            safety.verify(URL)
    else:
        assert safety.verify(URL) == identity


def test_docker_failure_prevents_postgres_connection(monkeypatch):
    def denied(*args):
        raise RuntimeError("Docker unavailable")
    monkeypatch.setattr(safety, "docker", denied)
    monkeypatch.setattr(safety.asyncpg, "connect", lambda **kw: pytest.fail("Must fail before TCP"))
    with pytest.raises(RuntimeError, match="Docker unavailable"):
        safety.verify(URL)


def test_runner_partitions_fresh_processes_and_clears_source_urls(monkeypatch):
    from scripts import run_backend_tests as runner
    from types import SimpleNamespace
    monkeypatch.setenv("TEST_DATABASE_URL", URL)
    monkeypatch.setenv("PHASE1_SOURCE_DATABASE_URL", "must-not-inherit")
    monkeypatch.setenv("PHASE1_TEST_DATABASE_URL", "must-not-inherit")
    monkeypatch.setattr(runner.sys, "argv", ["run_backend_tests.py"])
    verified, launched = [], []
    monkeypatch.setattr(runner, "verify", lambda raw: verified.append(raw))

    def run(command, *, cwd, env):
        launched.append((command, env))
        assert env["TEST_DATABASE_URL"] == verified[-1]
        assert env["DATABASE_URL"] == env["INVOICEIQ_PHASE7_DB_URL"] == verified[-1]
        assert "PHASE1_SOURCE_DATABASE_URL" not in env
        assert "PHASE1_TEST_DATABASE_URL" not in env
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", run)
    assert runner.main() == 0
    assert len(launched) == 2
    assert launched[0][0][-1] == runner.PHASE5
    assert launched[1][0][-1] == f"--ignore={runner.PHASE5}"
    assert verified[0].endswith("/invoiceiq_phase1_regression_test")
    assert verified[1].endswith("/invoiceiq_test")


def test_runner_never_launches_pytest_after_verification_failure(monkeypatch):
    from scripts import run_backend_tests as runner
    monkeypatch.setenv("TEST_DATABASE_URL", URL)
    monkeypatch.setattr(runner.sys, "argv", ["run_backend_tests.py"])
    def refuse(raw):
        raise RuntimeError("Not disposable")
    monkeypatch.setattr(runner, "verify", refuse)
    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **kw: pytest.fail("Must not start pytest"))
    assert runner.main() == 1
