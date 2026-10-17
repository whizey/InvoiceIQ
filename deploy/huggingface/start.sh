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
exec ./entrypoint.sh
