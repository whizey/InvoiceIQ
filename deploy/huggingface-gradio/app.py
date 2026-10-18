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

# A free Space runs one container and cannot host a managed broker, so a single-node Kafka (KRaft) can
# start beside the API. It starts now, in parallel with the migrations and seeding below. If it cannot
# start, the app falls back to applying events in-process, so the product never depends on it. Set
# EMBEDDED_KAFKA=false to turn this off, or set KAFKA_BOOTSTRAP_SERVERS (and the SASL variables) to use a
# managed broker instead.
embedded_kafka = None
if os.environ.get("EMBEDDED_KAFKA", "true").lower() == "true" and not os.environ.get("KAFKA_BOOTSTRAP_SERVERS"):
    embedded_kafka = subprocess.Popen(["bash", str(ROOT / "start_kafka.sh")])

subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], check=True)
# Idempotent: seeds the illustrative showcase once; later starts add nothing.
subprocess.run([sys.executable, "-m", "scripts.provision_portal_showcase"], check=True)

if embedded_kafka is not None:
    try:
        if embedded_kafka.wait(timeout=240) != 0:
            raise RuntimeError("start_kafka.sh failed")
        os.environ["KAFKA_BOOTSTRAP_SERVERS"] = "127.0.0.1:9092"
        os.environ["OUTBOX_DISPATCH_ENABLED"] = "true"
    except Exception as exc:  # noqa: BLE001 - Java missing, download blocked, timeout...
        print(f"Embedded Kafka unavailable, events will be applied in-process: {exc}", flush=True)

# The consumer runs beside the API whenever a broker is configured.
if os.environ.get("KAFKA_BOOTSTRAP_SERVERS"):
    subprocess.Popen([sys.executable, "-m", "app.events.consumer"])

import uvicorn  # noqa: E402

uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "7860")))
