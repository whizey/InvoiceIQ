#!/usr/bin/env bash
# One-command local dev runner: starts Postgres (Docker), sets up the
# backend venv, applies migrations, launches the API with auto-reload,
# and launches the Next.js dashboard with hot reload.
#
# Usage: ./run.sh

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

if [ -d "/opt/homebrew/bin" ]; then
    export PATH="/opt/homebrew/bin:$PATH"
fi

if ! docker info >/dev/null 2>&1; then
    echo "Docker doesn't seem to be running. Start Docker Desktop and try again."
    exit 1
fi

echo "==> Starting Postgres (Docker)..."
(cd infra && docker compose up -d db)

DB_CONTAINER=$(cd infra && docker compose ps -q db)
echo "==> Waiting for Postgres to be healthy..."
for i in $(seq 1 30); do
    STATUS=$(docker inspect --format='{{.State.Health.Status}}' "$DB_CONTAINER" 2>/dev/null || echo "")
    if [ "$STATUS" = "healthy" ]; then
        break
    fi
    sleep 1
done
if [ "$STATUS" != "healthy" ]; then
    echo "Postgres did not become healthy in time. Check 'docker compose -f infra/docker-compose.yml logs db'."
    exit 1
fi

cd "$REPO_ROOT/backend"

PYTHON_BIN=""
for candidate in /opt/homebrew/Cellar/python@3.13/*/bin/python3.13 /opt/homebrew/bin/python3.13 /opt/homebrew/bin/python3.12 /usr/local/bin/python3.13 /usr/local/bin/python3.12 python3.13 python3.12 python3; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        if "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' >/dev/null 2>&1; then
            PYTHON_BIN="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "Python 3.12+ is required to install the backend dependencies."
    exit 1
fi

if [ ! -d .venv ]; then
    echo "==> Creating virtualenv with $PYTHON_BIN..."
    "$PYTHON_BIN" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing backend dependencies..."
pip install -q -r requirements-dev.txt

if [ ! -f .env ]; then
    echo "==> Creating backend/.env from .env.example..."
    cp .env.example .env
fi

echo "==> Applying database migrations..."
alembic upgrade head

# npm/Next.js (Turbopack) spawn subprocesses that don't always die with
# their parent, so on exit we also free the ports directly rather than
# relying solely on PID-based kills.
kill_port() {
    local pids
    pids=$(lsof -ti "tcp:$1" -sTCP:LISTEN 2>/dev/null || true)
    if [ -n "$pids" ]; then
        kill $pids 2>/dev/null || true
    fi
}

cleanup() {
    echo
    echo "==> Stopping API and dashboard..."
    kill "$API_PID" "$WEB_PID" 2>/dev/null || true
    sleep 1
    kill_port 8000
    kill_port 3000
}
trap cleanup EXIT INT TERM

echo "==> Starting API with auto-reload at http://localhost:8000"
kill_port 8000
uvicorn app.main:app --reload --port 8000 &
API_PID=$!

cd "$REPO_ROOT/frontend"

if [ ! -d node_modules ]; then
    echo "==> Installing frontend dependencies (first run only, ~30s)..."
    npm install
fi

if [ ! -f .env.local ]; then
    echo "==> Creating frontend/.env.local from .env.local.example..."
    cp .env.local.example .env.local
fi

echo "==> Starting dashboard at http://localhost:3000 (Ctrl+C to stop everything)"
echo "    Postgres stays running in Docker — 'cd infra && docker compose down' to stop it."
kill_port 3000
rm -rf .next/dev 2>/dev/null || true
npm run dev &
WEB_PID=$!

wait
