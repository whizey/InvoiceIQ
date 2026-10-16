FROM python:3.12-slim AS builder

WORKDIR /project/backend
COPY backend/requirements.txt .
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
# The public demo has CPU inference only. Preserve the XGBoost version and
# Python API while omitting the CUDA/NCCL wheel pulled by the general package.
RUN test "$(grep -c '^xgboost==3.4.1$' requirements.txt)" = 1 \
    && sed 's/^xgboost==3.4.1$/xgboost-cpu==3.4.1/' requirements.txt > requirements-container.txt \
    && python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements-container.txt

FROM python:3.12-slim

WORKDIR /project/backend
ENV OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 PYTHONDONTWRITEBYTECODE=1 HOME=/tmp \
    ENV=staging INVOICEIQ_PORTAL_ENABLED=true INVOICEIQ_PORTAL_SHOWCASE=true PUBLIC_READ_ONLY=false \
    RUN_MIGRATIONS=false SCHEDULER_ENABLED=false OUTBOX_DISPATCH_ENABLED=false METRICS_ENABLED=false \
    LLM_MODEL=gemini-3.6-flash PATH=/opt/venv/bin:$PATH

RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /opt/venv /opt/venv

COPY backend/app ./app
COPY scripts/verification/check_model_artifacts.py ./check_model_artifacts.py
COPY backend/docs ./docs
COPY backend/EVALUATION_PROTOCOL.md .
COPY backend/alembic ./alembic
COPY backend/alembic.ini .
COPY backend/scripts ./scripts
COPY backend/entrypoint.sh .
COPY start.sh .
COPY docs/evaluations/model-risk-v1.json /project/docs/evaluations/model-risk-v1.json
RUN python -c 'from pathlib import Path; from check_model_artifacts import verify_artifacts; verify_artifacts(root=Path("/project/backend/app/ml/artifacts"), verify_binaries=True)'
RUN chmod +x entrypoint.sh start.sh && chown -R 10001:10001 /project

USER 10001:10001

CMD ["./start.sh"]

HEALTHCHECK --interval=30s --timeout=15s --start-period=30s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=12)"
