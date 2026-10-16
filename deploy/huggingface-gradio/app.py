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

# Free accounts can only use ZeroGPU hardware, which reports GPU functions to the platform when Gradio's
# launch() runs and refuses Spaces that report none. Start a tiny Gradio app (on an internal port) with one
# placeholder GPU function so the Space is accepted; the API itself is served by uvicorn below.
try:
    import gradio as gr
    import spaces

    @spaces.GPU
    def _placeholder_gpu_function():
        return "ok"

    with gr.Blocks(title="InvoiceIQ API") as _status_page:
        gr.Markdown("InvoiceIQ API is running. This page is only a status placeholder.")
        _out = gr.Textbox(label="Status", interactive=False)
        gr.Button("Check").click(_placeholder_gpu_function, None, _out)
    _status_page.launch(prevent_thread_lock=True, server_name="127.0.0.1", server_port=7862,
                        show_api=False, quiet=True)
except Exception as exc:  # not on a ZeroGPU Space (for example local runs): the API does not need it
    print(f"GPU placeholder skipped: {exc}", flush=True)

subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
# Idempotent: seeds the illustrative showcase once; later starts add nothing.
subprocess.run([sys.executable, "-m", "scripts.provision_portal_showcase"], check=True)

# A free Space runs one container, so the event consumer runs beside the API. It starts only when a
# Kafka broker is configured; without KAFKA_BOOTSTRAP_SERVERS the app just logs its events.
if os.environ.get("KAFKA_BOOTSTRAP_SERVERS"):
    subprocess.Popen([sys.executable, "-m", "app.events.consumer"])

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
