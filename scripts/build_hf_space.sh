#!/usr/bin/env bash
# Assemble a Hugging Face Space folder (Docker SDK) containing only what the API image needs.
# Usage: scripts/build_hf_space.sh [target-dir] [gradio|docker]   (default: ../InvoiceIQ-hf-space, gradio)
# `gradio` suits free accounts (the Docker SDK is paid for new accounts); `docker` builds the container image.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$ROOT/../InvoiceIQ-hf-space}"
MODE="${2:-gradio}"
[ "$MODE" = gradio ] || [ "$MODE" = docker ] || { echo "Mode must be gradio or docker" >&2; exit 1; }
[ -e "$OUT" ] && { echo "Refusing to overwrite existing $OUT" >&2; exit 1; }
mkdir -p "$OUT/scripts/verification" "$OUT/docs/evaluations"
cp "$ROOT/.gitattributes" "$OUT/"
if [ "$MODE" = docker ]; then
  cp "$ROOT/deploy/huggingface/Dockerfile" "$ROOT/deploy/huggingface/README.md" "$ROOT/deploy/huggingface/start.sh" "$OUT/"
  cp "$ROOT/.dockerignore" "$OUT/"
  printf '!start.sh\n' >> "$OUT/.dockerignore"   # the repo whitelist predates the Space start script
else
  cp "$ROOT/deploy/huggingface-gradio/README.md" "$ROOT/deploy/huggingface-gradio/app.py" "$OUT/"
  # CPU-only XGBoost keeps the install small, matching the container image.
  sed 's/^xgboost==3.4.1$/xgboost-cpu==3.4.1/' "$ROOT/backend/requirements.txt" > "$OUT/requirements.txt"
fi
cp "$ROOT/scripts/verification/check_model_artifacts.py" "$OUT/scripts/verification/"
cp "$ROOT/docs/evaluations/model-risk-v1.json" "$OUT/docs/evaluations/"
# Runtime code only: no tests, local databases or private environment files.
rsync -a --exclude '.venv' --exclude '__pycache__' --exclude '.env*' --exclude 'tests' --exclude '.pytest_cache' \
  --exclude '.coverage*' --exclude 'app/ml/experiments' "$ROOT/backend/" "$OUT/backend/"
# Fail early if model files are still Git LFS pointers (run `git lfs pull` first).
if grep -rIl --include='*.joblib' 'git-lfs.github.com/spec' "$OUT/backend/app/ml/artifacts" 2>/dev/null | head -1 | grep -q .; then
  echo "Model artifacts are LFS pointers; run 'git lfs pull' in the repo first." >&2; exit 1
fi
# Hugging Face rejects binary files that are not stored through Git LFS/Xet.
for pattern in '*.png' '*.jpg' '*.jpeg' '*.gif' '*.ico' '*.pdf' '*.gz' '*.zip' '*.bin' '*.npy' '*.npz' '*.onnx' '*.pt' '*.h5' '*.woff' '*.woff2'; do
  grep -qxF "$pattern filter=lfs diff=lfs merge=lfs -text" "$OUT/.gitattributes" \
    || printf '%s filter=lfs diff=lfs merge=lfs -text\n' "$pattern" >> "$OUT/.gitattributes"
done
cd "$OUT"
git init -q -b main
git lfs install --local >/dev/null
git add -A
git -c user.name="InvoiceIQ" -c user.email="noreply@example.invalid" commit -q -m "InvoiceIQ API Space"
echo "Space folder ready: $OUT"
du -sh "$OUT" | tail -1
