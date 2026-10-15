"""Fail-closed verification for the two disposable PostgreSQL test databases."""
import asyncio
import json
import subprocess

import asyncpg
from sqlalchemy.engine import make_url

DATABASES = {"invoiceiq_test", "invoiceiq_phase1_regression_test",
             "invoiceiq_phase1_corpus_test", "invoiceiq_phase1_replay_test",
             "invoiceiq_demo_test"}
CONTAINER = "invoiceiq-ci-disposable-db"
HOST = "127.0.0.1"
HOST_PORT = 55436
DATA_DIRECTORY = "/var/lib/postgresql/data"
IDENTITY_SQL = """SELECT current_database() AS database,
    current_user AS username, current_setting('port')::int AS port,
    (SELECT system_identifier::text FROM pg_control_system()) AS cluster,
    (SELECT oid::text FROM pg_database WHERE datname=current_database()) AS oid,
    pg_postmaster_start_time()::text AS started"""


def validate_url(raw):
    url = make_url(raw)
    if (url.drivername != "postgresql+asyncpg" or url.host != HOST
            or url.port != HOST_PORT or url.database not in DATABASES
            or not url.username or not url.password or url.query):
        raise RuntimeError("Unverified test destination: require the dedicated disposable endpoint and database")
    return url


def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=20)
    if result.returncode:
        raise RuntimeError("Docker verification failed; no test database writes permitted")
    return result.stdout


def inspect_container(data):
    tmpfs = data.get("HostConfig", {}).get("Tmpfs") or {}
    if (data["Name"] != f"/{CONTAINER}" or not data["State"]["Running"]
            or data["Config"]["Image"] not in {"postgres:16", "postgres:16-alpine"}
            or (data["Config"].get("Labels") or {}).get("com.invoiceiq.disposable") != "true"
            or data["NetworkSettings"]["Ports"].get("5432/tcp") != [
                {"HostIp": HOST, "HostPort": str(HOST_PORT)}]
            or DATA_DIRECTORY not in tmpfs
            or "size=2147483648" not in tmpfs[DATA_DIRECTORY]
            or any(m["Type"] != "tmpfs" for m in data["Mounts"])):
        raise RuntimeError("Container is not an explicitly disposable, tmpfs-only PostgreSQL test instance")
    return data["Id"]


def verify(raw):
    url = validate_url(raw)
    info = json.loads(docker("inspect", CONTAINER))[0]
    container_id = inspect_container(info)
    # Read-only identity through the container and the actual host TCP connection
    # must agree. Neither a URL suffix nor an environment flag grants approval.
    command = f"SELECT row_to_json(identity) FROM ({IDENTITY_SQL}) identity"
    inside = json.loads(docker("exec", container_id, "psql", "-XAt", "-v", "ON_ERROR_STOP=1",
                               "-U", url.username, "-d", url.database, "-c", command))

    async def connection_identity():
        conn = await asyncpg.connect(host=url.host, port=url.port, user=url.username,
                                     password=url.password, database=url.database, timeout=10)
        try:
            async with conn.transaction(readonly=True):
                return dict(await conn.fetchrow(IDENTITY_SQL))
        finally:
            await conn.close()

    outside = asyncio.run(connection_identity())
    if (inside != outside or outside["database"] != url.database
            or outside["username"] != url.username or outside["port"] != 5432):
        raise RuntimeError("PostgreSQL container and TCP database identities do not match")
    if inspect_container(json.loads(docker("inspect", CONTAINER))[0]) != container_id:
        raise RuntimeError("Test container changed during verification")
    return outside


def install_connection_guard(raw):
    """Cover additional SQLAlchemy engines before they open a connection."""
    from sqlalchemy import event
    from sqlalchemy.engine import Engine
    import os
    approved = {}
    for candidate in (raw, os.environ.get("PHASE1_TEST_DATABASE_URL"),
                      os.environ.get("PHASE1_REPLAY_DATABASE_URL"),
                      os.environ.get("INVOICEIQ_PHASE7_DB_URL")):
        if candidate:
            url = validate_url(candidate)
            approved[(url.host, url.port, url.database, url.username)] = candidate
    identities = {}
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock
    lock = Lock()

    @event.listens_for(Engine, "engine_connect")
    def check_connection(connection):
        # engine_connect runs before fixtures can execute statements. A separate
        # do_connect guard below also refuses the destination before network IO.
        if connection.dialect.name != "postgresql":
            return
        row = connection.exec_driver_sql(IDENTITY_SQL).mappings().one()
        key = (connection.engine.url.host, connection.engine.url.port,
               connection.engine.url.database, connection.engine.url.username)
        if dict(row) != identities[key]:
            raise RuntimeError("Connected PostgreSQL identity changed")
        connection.rollback()

    @event.listens_for(Engine, "do_connect")
    def check_destination(dialect, record, args, params):
        if dialect.name == "postgresql":
            key = (params.get("host"), params.get("port"),
                   params.get("database"), params.get("user"))
            if key not in approved:
                raise RuntimeError("Secondary test connection targets an unverified database")
            with lock:
                if key not in identities:
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        identities[key] = executor.submit(verify, approved[key]).result()
