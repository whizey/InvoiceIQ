#!/usr/bin/env bash
# Resume the live assistant eval on the dedicated EVAL_* keys and refresh the published results.
# Safe to run every day: each model stops at its daily quota and the next run picks up where it left off.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON=".venv/bin/python"
[[ -x "$PYTHON" ]] || PYTHON="python"
# gemini-3.6-flash is left out by default: its free tier allows 20 requests a day (about 6 questions).
MODELS=${EVAL_MODELS:-"gemini:gemini-3.5-flash-lite groq:openai/gpt-oss-120b"}
specs=()
for model in $MODELS; do specs+=(--live "$model"); done

"$PYTHON" -m app.agents.assistant_eval "${specs[@]}" --runs "${EVAL_RUNS:-3}" --out-dir eval-runs
"$PYTHON" -m app.agents.assistant_eval app/agents/eval_results.json --summary --out-dir eval-runs \
  --readme ../README.md
