# Local Kafka demo (Redpanda)

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

Stop it with `docker compose -f deploy/local-kafka/docker-compose.yml down`.
