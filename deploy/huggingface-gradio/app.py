"""Hugging Face Space entry point: prepare the database, then serve the InvoiceIQ FastAPI app on port 7860."""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = ROOT / "backend"
os.chdir(BACKEND)
sys.path.insert(0, str(BACKEND))

# Non-secret runtime flags. DATABASE_URL, GOOGLE_API_KEY and INVOICEIQ_SHOWCASE_DATABASE come from Space settings.
for key, value in {
    "ENV": "staging", "INVOICEIQ_PORTAL_ENABLED": "true", "INVOICEIQ_PORTAL_SHOWCASE": "true",
    "PUBLIC_READ_ONLY": "false", "RUN_MIGRATIONS": "false", "SCHEDULER_ENABLED": "false",
    "OUTBOX_DISPATCH_ENABLED": "false", "METRICS_ENABLED": "false", "LLM_MODEL": "gemini-3.6-flash",
}.items():
    os.environ.setdefault(key, value)

if not os.environ.get("DATABASE_URL"):
    raise SystemExit("DATABASE_URL secret is not set; add it in the Space settings.")

# ZeroGPU hardware refuses to start a Space that registers no GPU function; this one is never called.
try:
    import spaces

    @spaces.GPU
    def _unused_gpu_function():
        return None
except Exception:
    pass

subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
# Idempotent: seeds the illustrative showcase once; later starts add nothing.
subprocess.run([sys.executable, "-m", "scripts.provision_portal_showcase"], check=True)

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
