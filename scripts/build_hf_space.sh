#!/usr/bin/env bash
# Assemble a Hugging Face Space folder (Docker SDK) containing only what the API image needs.
# Usage: scripts/build_hf_space.sh [target-dir]   (default: ../InvoiceIQ-hf-space)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/../InvoiceIQ-hf-space}"
[ -e "$OUT" ] && { echo "Refusing to overwrite existing $OUT" >&2; exit 1; }
mkdir -p "$OUT/scripts/verification" "$OUT/docs/evaluations"
cp "$ROOT/deploy/huggingface/Dockerfile" "$ROOT/deploy/huggingface/README.md" "$ROOT/deploy/huggingface/start.sh" "$OUT/"
cp "$ROOT/.dockerignore" "$ROOT/.gitattributes" "$OUT/"
cp "$ROOT/scripts/verification/check_model_artifacts.py" "$OUT/scripts/verification/"
cp "$ROOT/docs/evaluations/model-risk-v1.json" "$OUT/docs/evaluations/"
# Runtime code only: no tests, local databases or private environment files.
rsync -a --exclude '.venv' --exclude '__pycache__' --exclude '.env*' --exclude 'tests' --exclude '.pytest_cache' \
  --exclude '.coverage*' --exclude 'app/ml/experiments' "$ROOT/backend/" "$OUT/backend/"
# Fail early if model files are still Git LFS pointers (run `git lfs pull` first).
if grep -rIl --include='*.joblib' 'git-lfs.github.com/spec' "$OUT/backend/app/ml/artifacts" 2>/dev/null | head -1 | grep -q .; then
  echo "Model artifacts are LFS pointers; run 'git lfs pull' in the repo first." >&2; exit 1
fi
cd "$OUT"
git init -q -b main
git lfs install --local >/dev/null
git add -A
git -c user.name="InvoiceIQ" -c user.email="noreply@example.invalid" commit -q -m "InvoiceIQ API Space"
echo "Space folder ready: $OUT"
du -sh "$OUT" | tail -1
