"""Run the isolated SaaS preview without touching unrelated local processes.

Usage: backend/.venv/bin/python scripts/portal_preview.py
Requires the verified disposable Docker database and backend/.env.test.
"""
import argparse
import asyncio
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from dotenv import dotenv_values
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
FRONTEND = ROOT / "frontend"
sys.path.insert(0, str(BACKEND))
from scripts.test_database_safety import verify  # noqa: E402


def available(port):
    with socket.socket() as sock:
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_ready(port, process):
    for _ in range(150):
        if process.poll() is not None:
            raise RuntimeError(f"Preview process exited before port {port} became ready")
        if not available(port):
            return
        time.sleep(.2)
    raise RuntimeError(f"Preview process did not listen on port {port}")


async def showcase_exists(url):
    import asyncpg
    from app.api.portal import SHOWCASE_ID

    connection = await asyncpg.connect(host=url.host, port=url.port, user=url.username,
                                       password=url.password, database=url.database, timeout=10)
    try:
        return await connection.fetchval("SELECT EXISTS(SELECT 1 FROM portal_workspaces WHERE id=$1)", SHOWCASE_ID)
    finally:
        await connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--frontend-port", type=int, default=3048)
    parser.add_argument("--backend-port", type=int, default=8018)
    args = parser.parse_args()
    if args.frontend_port == args.backend_port or any(not 1024 <= port <= 65535
                                                       for port in (args.frontend_port, args.backend_port)):
        raise RuntimeError("Choose two different non-privileged ports")
    for port in (args.frontend_port, args.backend_port):
        if not available(port):
            raise RuntimeError(f"Port {port} is occupied; choose another port. No process was stopped.")
    private = dotenv_values(BACKEND / ".env.test")
    raw = private.get("TEST_DATABASE_URL")
    if not raw:
        raise RuntimeError("backend/.env.test with TEST_DATABASE_URL is required")
    url = make_url(raw).set(database="invoiceiq_demo_test")
    demo_url = url.render_as_string(hide_password=False)
    verify(demo_url)
    if not asyncio.run(showcase_exists(url)):
        raise RuntimeError("Isolated showcase is not provisioned in invoiceiq_demo_test")
    backend_env = dict(os.environ, DATABASE_URL=demo_url,
                       INVOICEIQ_PORTAL_ENABLED="true", INVOICEIQ_PORTAL_SHOWCASE="true",
                       PUBLIC_READ_ONLY="false")
    frontend_env = dict(os.environ, INVOICEIQ_PORTAL_ENABLED="true",
                        INVOICEIQ_API_URL=f"http://127.0.0.1:{args.backend_port}")
    subprocess.run(["npm", "run", "build"], cwd=FRONTEND, env=frontend_env, check=True)
    children = []
    try:
        api = subprocess.Popen([str(BACKEND / ".venv/bin/python"), "-m", "uvicorn", "app.main:app",
                                "--host", "127.0.0.1", "--port", str(args.backend_port)],
                               cwd=BACKEND, env=backend_env, start_new_session=True)
        children.append(api)
        wait_ready(args.backend_port, api)
        web = subprocess.Popen(["npm", "run", "start", "--", "-H", "127.0.0.1", "-p",
                                str(args.frontend_port)], cwd=FRONTEND, env=frontend_env,
                               start_new_session=True)
        children.append(web)
        wait_ready(args.frontend_port, web)
        print(f"Preview: http://127.0.0.1:{args.frontend_port}/home", flush=True)
        print(f"Backend health: http://127.0.0.1:{args.backend_port}/health", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(.5)
        raise RuntimeError("A preview process exited unexpectedly")
    except KeyboardInterrupt:
        return 0
    finally:
        for child in reversed(children):
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Preview unavailable: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
