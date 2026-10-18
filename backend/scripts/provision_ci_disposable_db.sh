#!/usr/bin/env bash
# Run in host Terminal from backend/. Creates only the new named test container.
set -euo pipefail
cd "$(dirname "$0")/.."

if docker inspect invoiceiq-ci-disposable-db >/dev/null 2>&1; then
  echo 'Refusing to replace an existing invoiceiq-ci-disposable-db container.' >&2
  exit 1
fi
python3 - <<'PY'
import re
import subprocess

page_output = subprocess.check_output(['vm_stat'], text=True)
page_size = int(re.search(r'page size of (\d+) bytes', page_output).group(1))
counts = {key: int(value) for key, value in re.findall(r'^Pages (free|inactive|speculative):\s+(\d+)\.', page_output, re.M)}
reclaimable = sum(counts.values()) * page_size
host_total = int(subprocess.check_output(['sysctl', '-n', 'hw.memsize'], text=True))
docker_total = int(subprocess.check_output(['docker', 'info', '--format', '{{.MemTotal}}'], text=True))
corpus = '../data/raw/b2b_primary/b2b_invoice_payment_45839.csv'
from pathlib import Path
corpus_size = Path(corpus).stat().st_size
import hashlib
if hashlib.sha256(Path(corpus).read_bytes()).hexdigest() != '2a7f1dfc4e52231d4a4cb60ee019d6cef3730708afffb1c12786b4d3061bb32d':
    raise SystemExit('Public test corpus fingerprint mismatch; no container started')
print(f'Host RAM: {host_total / 2**30:.2f} GiB; currently reclaimable: {reclaimable / 2**30:.2f} GiB; '
      f'Docker VM RAM: {docker_total / 2**30:.2f} GiB; CSV: {corpus_size / 2**20:.1f} MiB')
if host_total < 6 * 2**30 or reclaimable < 3 * 2**30 or docker_total < 4 * 2**30:
    raise SystemExit('Insufficient memory for the 2 GiB tmpfs plus test processes; no container started')
if corpus_size > 100 * 2**20:
    raise SystemExit('Corpus exceeds the validated test size; no container started')
PY

password="$(openssl rand -hex 24)"
credential_file="$(mktemp /private/tmp/invoiceiq-ci-db.XXXXXX)"
trap 'rm -f "$credential_file"' EXIT
chmod 600 "$credential_file"
printf "export TEST_DATABASE_URL='postgresql+asyncpg://invoiceiq_ci:%s@127.0.0.1:55436/invoiceiq_test'\n" "$password" > "$credential_file"

docker run -d --name invoiceiq-ci-disposable-db \
  --label com.invoiceiq.disposable=true \
  --publish 127.0.0.1:55436:5432 \
  --tmpfs /var/lib/postgresql/data:rw,noexec,nosuid,size=2147483648 \
  --env POSTGRES_USER=invoiceiq_ci \
  --env POSTGRES_PASSWORD="$password" \
  --env POSTGRES_DB=invoiceiq_test \
  postgres:16-alpine >/dev/null
unset password

until docker exec invoiceiq-ci-disposable-db pg_isready -U invoiceiq_ci -d invoiceiq_test >/dev/null 2>&1; do
  sleep 1
done
for database in invoiceiq_phase1_regression_test invoiceiq_phase1_corpus_test invoiceiq_phase1_replay_test; do
  docker exec invoiceiq-ci-disposable-db createdb -U invoiceiq_ci "$database"
done
# The verifier checks Docker identity, tmpfs, the TCP endpoint and PostgreSQL's
# actual cluster/database identifiers before any migration can write.
source "$credential_file"
.venv/bin/python - <<'PY'
import os
from sqlalchemy.engine import make_url
from scripts.test_database_safety import verify
base = make_url(os.environ['TEST_DATABASE_URL'])
for database in ('invoiceiq_test', 'invoiceiq_phase1_regression_test',
                 'invoiceiq_phase1_corpus_test', 'invoiceiq_phase1_replay_test'):
    verify(base.set(database=database).render_as_string(hide_password=False))
    print('Verified disposable', database)
PY
for database in invoiceiq_test invoiceiq_phase1_regression_test invoiceiq_phase1_corpus_test invoiceiq_phase1_replay_test; do
  export DATABASE_URL="$(.venv/bin/python - "$database" <<'PY'
import os, sys
from sqlalchemy.engine import make_url
print(make_url(os.environ['TEST_DATABASE_URL']).set(database=sys.argv[1]).render_as_string(hide_password=False))
PY
)"
  .venv/bin/alembic upgrade head
  .venv/bin/alembic current
  docker exec invoiceiq-ci-disposable-db df -h /var/lib/postgresql/data
done
unset DATABASE_URL
.venv/bin/python - <<'PY'
import os
from pathlib import Path
from sqlalchemy.engine import make_url

base = make_url(os.environ['TEST_DATABASE_URL'])
targets = {'TEST_DATABASE_URL': 'invoiceiq_test',
           'PHASE1_TEST_DATABASE_URL': 'invoiceiq_phase1_corpus_test',
           'PHASE1_REPLAY_DATABASE_URL': 'invoiceiq_phase1_replay_test',
           'INVOICEIQ_PHASE7_DB_URL': 'invoiceiq_test'}
path = Path('.env.test')
path.write_text(''.join(f"{key}='{base.set(database=name).render_as_string(hide_password=False)}'\n"
                        for key, name in targets.items()))
path.chmod(0o600)
PY
set -a
source .env.test
set +a
REAL_DATASET_DATABASE_URL="$PHASE1_TEST_DATABASE_URL" .venv/bin/python -m app.seed.load_real_dataset
.venv/bin/python -m scripts.prepare_phase1_replay
printf 'Provisioned and prepared four disposable databases. Private test environment: backend/.env.test\n'
