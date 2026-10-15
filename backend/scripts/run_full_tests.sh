#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python"
fi

if [[ ! -f .env.test ]]; then
  echo 'ERROR: backend/.env.test is required; run the disposable DB provisioning script.' >&2
  exit 1
fi
set -a
source .env.test
set +a
unset PYTEST_ADDOPTS
"$PYTHON" -m scripts.run_full_tests "$@"
