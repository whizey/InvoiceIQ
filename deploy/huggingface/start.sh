#!/bin/sh
# Hugging Face Space start-up: prepare the database, then run the API.
set -e
if [ -z "${DATABASE_URL:-}" ]; then
  echo "DATABASE_URL secret is not set; add it in the Space settings." >&2
  exit 1
fi
alembic upgrade head
# Idempotent: seeds the illustrative showcase once, later runs add nothing.
python -m scripts.provision_portal_showcase
python -m scripts.provision_public_ledger || echo "Public 2015 ledger not loaded; continuing without it"
exec ./entrypoint.sh
