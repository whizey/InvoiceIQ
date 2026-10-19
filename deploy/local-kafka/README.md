# Local Kafka with Redpanda

Shows the event consumer working end to end on a laptop. Not used by the hosted app.

```bash
# 1. Start Redpanda and its web console (http://localhost:8080)
docker compose -f deploy/local-kafka/docker-compose.yml up -d

# 2. Point the backend at a migrated database you can write to, and at the broker
cd backend
export DATABASE_URL="postgresql+asyncpg://USER:PASSWORD@127.0.0.1:5432/DBNAME"
export KAFKA_BOOTSTRAP_SERVERS=127.0.0.1:9092

# 3. Send sample events (one is sent twice, one is unreadable), then run the consumer
python -m scripts.send_sample_events
python -m app.events.consumer          # Ctrl+C after a few seconds
```

Expected result: 3 rows in `processed_events`, 2 in `case_risk_latest` (one HIGH, one LOW),
1 in `approval_alerts` and 1 in `dead_letter_events`. The duplicates change nothing. In the
console, the topics `risk.scored`, `review.requested` and their `.dlq` topics show the messages.

For a short, repeatable deduplication check against the disposable test database, run:

```bash
cd backend
set -a; source .env.test; set +a
.venv/bin/python -m scripts.check_event_dedupe
```

It prints:

```text
first delivery: processed
second delivery: duplicate
approval alerts: 1
dedupe markers: 1
```

The script verifies the disposable PostgreSQL container before writes and removes the rows it created.
To inspect and replay failures from the Kafka run, use `python -m app.events.replay --dry-run`
then `python -m app.events.replay --topic risk.scored` after fixing the cause.

Stop it with `docker compose -f deploy/local-kafka/docker-compose.yml down`.

## Through the product

With the broker running and `KAFKA_BOOTSTRAP_SERVERS` set, an escalation recorded in the portal no longer
raises its alert immediately. The decision commits with an outbox event, the dispatcher publishes it to
`review.requested`, and the consumer creates the alert a moment later; approving the invoice later emits
`review.resolved`, which closes it. Run the dispatcher by setting `OUTBOX_DISPATCH_ENABLED=true` on the API.
