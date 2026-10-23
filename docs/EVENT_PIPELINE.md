# Event pipeline

Decisions are announced as events through a transactional outbox, published to Kafka, and applied by an idempotent consumer with retries and a dead-letter store. If no broker is reachable, the same handlers run in-process, so the product never waits on infrastructure it can't reach.

## Why an outbox

If the API saved a decision and then published to Kafka separately, a crash between the two steps would lose the event, or publishing first would announce something that never actually committed. The outbox writes the event row in the same transaction as the decision, so they commit or roll back together. A dispatcher publishes the pending rows afterwards.

Delivery is therefore at least once. A crash after Kafka acknowledges and before the database records "published" can deliver an event twice, so consumers must deduplicate on the stable `event_id`.

## Event contract

Each outbox row carries a JSON envelope:

```json
{
  "schema_version": 1,
  "event_id": "uuid",
  "correlation_id": "request id",
  "occurred_at": "ISO-8601 timestamp",
  "data": { "...": "topic-specific fields" }
}
```

Topics (`backend/app/events/topics.py`): `invoice.overdue`, `payment.received`, `recovery.case_created`, `recovery.action_completed`, `promise_to_pay.created`, `promise_to_pay.broken`, `recovery.case_closed`, `risk.scored`, `action.proposed`, `review.requested`, `review.resolved`. A topic that fails repeatedly is parked on `<topic>.dlq`.

## Producing events

- `TransactionalEventPublisher.publish(topic, key, payload)` validates the topic and key and adds the outbox row to the caller's session. It returns the envelope.
- The portal publishes `review.requested` when a decision is `ESCALATE`, and `review.resolved` for `APPROVE`, `REJECT` or `RESET`. The payload carries `case_id` (the invoice), `workspace_id` and a `visibility_scope` so alerts are tenant-scoped.
- The recovery workflow publishes `risk.scored`, `action.proposed`, `review.requested` (when policy requires human review), `recovery.action_completed` and `recovery.case_closed`.

## Delivering events

`dispatch_batch` claims pending rows with `SELECT ... FOR UPDATE SKIP LOCKED`, so several dispatchers can run without double-claiming. It publishes each row with a six-second timeout. On failure it increments `attempts` and delays the next try with exponential backoff capped at five minutes. On success it records `published_at` and `delivery_mode`.

| Mode | When | Behaviour |
|---|---|---|
| `kafka` | `KAFKA_BOOTSTRAP_SERVERS` is set and the dispatcher is enabled | Rows are published to the broker; the consumer applies them |
| `log` | No broker, legacy workflow | Events are logged locally |
| `inline` | No broker configured, or the broker did not answer | The same handlers run in-process right after the commit, and the row is marked published |

Hosted brokers need TLS and SASL. The app reads `KAFKA_SECURITY_PROTOCOL`, `KAFKA_SASL_MECHANISM`, `KAFKA_USERNAME` and `KAFKA_PASSWORD` and applies them to the producer and the consumer.

## Consuming events

`python -m app.events.consumer` subscribes to the handled topics (`risk.scored`, `review.requested`, `review.resolved`) in the consumer group `effects-v1`, with manual offset commits. An offset is committed only after an event is fully handled.

For each message, `handle_message`:

1. Parses the envelope. A malformed message is a poison event and goes straight to the dead letters with no retry.
2. In one transaction, inserts `(consumer, event_id)` into `processed_events` with `ON CONFLICT DO NOTHING`. If nothing was inserted, the event is a duplicate and nothing else happens.
3. Applies the effect in the same transaction. If it fails, the transaction rolls back, dedupe marker included, so a retry starts clean.
4. Retries up to 3 times with backoff. After that it writes a `dead_letter_events` row (unique per consumer and event, so replays do not duplicate it) and publishes the original message with the error to `<topic>.dlq`.

### Effects

| Event | Effect |
|---|---|
| `risk.scored` | Upserts the latest risk per case into `case_risk_latest`. An older event arriving late never overwrites a newer score. The "high-risk" view is the cases rated HIGH or CRITICAL. |
| `review.requested` | Creates an `approval_alerts` row with workspace, scope, subject and reason. Sends nothing outside the database. |
| `review.resolved` | Closes the open alerts for that case, workspace and scope, recording the decision and time. |

## Outbox reconciler

`app/events/reconcile.py` sweeps the outbox every 60 seconds, and at consumer start, for rows published to Kafka at least 45 seconds ago that have no `processed_events` entry and no dead letter. It runs them through the same idempotent handlers. Because applying an event twice is a no-op, the sweep is harmless next to a healthy consumer, and it makes the effects independent of the broker's durability. The consumer also reconnects automatically if the broker is restarting, and the sweep keeps running while it does.

## Monitoring

The existing private Prometheus endpoint (`/internal/metrics`, enabled with `METRICS_ENABLED=true` and protected by `METRICS_TOKEN` outside local environments) also reports pipeline health. The figures come from the database at scrape time, so they are correct whichever process handled an event and whichever broker, if any, carried it.

| Metric | Meaning | Alert when |
|---|---|---|
| `invoiceiq_events_awaiting_consumer` | Events published to Kafka that no consumer has applied yet: the end-to-end consumer lag | stays above 0 |
| `invoiceiq_events_awaiting_consumer_oldest_seconds` | Age of the oldest of those | exceeds a few minutes |
| `invoiceiq_outbox_unpublished_events` and `_oldest_seconds` | Outbox rows the dispatcher has not published | grows, which means the broker is unreachable |
| `invoiceiq_dead_letters{topic}` | Parked events waiting for inspection or replay | any non-zero value |
| `invoiceiq_events_processed_total`, `invoiceiq_dead_letters_replayed_total` | Throughput and recoveries | n/a |

Topic labels are limited to the handled topics plus `other`, so cardinality stays low.

## Replaying dead letters

After the cause of a failure is fixed, parked events can be re-applied through the same idempotent handlers:

```sh
python -m app.events.replay --dry-run                          # list what would be replayed
python -m app.events.replay --topic review.requested --limit 50
python -m app.events.replay --event-id <uuid>
```

Successful replays are stamped with `replayed_at` (the row is kept for audit), so they stop counting in the dead-letter metric. Events that still fail stay parked and are reported. Unreadable messages cannot be replayed and are reported as such. Replaying an event that was already applied is a no-op.

## Tables

`outbox_events`, `processed_events`, `case_risk_latest`, `approval_alerts`, `dead_letter_events` (migrations `c4a91e6d2b75`, `d7e2c5f19a40` and `e4b1c9d83f26`).

## Reading alerts

`GET /portal/alerts` returns up to 50 alerts for the workspace (and, in the public workspace, the visitor's own scope), open ones first. The frontend `ApprovalAlerts` panel polls it every five seconds.

## Running it locally with a real broker

See `deploy/local-kafka/README.md`: Docker Compose starts Redpanda and its web console at `localhost:8080`; `python -m scripts.send_sample_events` publishes test messages (one duplicated, one unreadable); `python -m app.events.consumer` applies them. Expected result: 3 events processed, the duplicates ignored, 2 case-risk rows (one HIGH, one LOW), 1 alert and 1 dead letter. An end-to-end run through the portal API, outbox, Redpanda and consumer was verified: an escalation produced an alert, and approving the invoice resolved it.

## Verified behaviours (tests)

Replaying an event twice produces one alert and one processed marker; a stale risk event does not overwrite a newer one; an event that keeps failing is retried three times then dead-lettered, its dedupe marker rolled back so a later replay can still succeed, and a redelivery does not create a second dead letter; unreadable messages are dead-lettered immediately; the pipeline metrics count events stuck between the broker and the consumer and catch up when the consumer does; replay applies parked events only after the cause is fixed, stamps them, and reports unreadable ones; topics without a handler are ignored; an escalation raises an alert scoped to its workspace and an approval resolves it; with a broker configured the decision waits for the consumer.

## Hosted deployment: embedded single-node Kafka

A free Hugging Face Space runs one container and cannot host a managed broker, so `deploy/huggingface-gradio/start_kafka.sh` starts a single-node Kafka 3.9 in KRaft mode (no ZooKeeper) beside the API and the consumer:

- It unpacks a Kafka archive bundled with the deploy (stored with Git LFS, SHA-512 checksum pinned in the script; it falls back to the official archive server if the bundle is missing), formats storage, starts the broker with a 256 MB heap and a small thread footprint (about 150 MB resident), waits for the port and creates the six topics (three event topics and their `.dlq` twins). Java comes from `packages.txt`. Broker startup takes about 12 seconds, and it runs in parallel with the database migrations.
- `app.py` then points the app at `127.0.0.1:9092`, enables the outbox dispatcher and starts the consumer.
- Fallback: if Java, the download or the broker fails, the app prints a message and carries on with in-process delivery. Nothing breaks.
- Storage is ephemeral, and that is covered. After a restart the broker starts empty. Every event is written to the Postgres outbox first. Unpublished events are re-sent by the dispatcher, and events that were published but never applied are re-applied by the outbox reconciler (below). A test destroys the broker and its data after an event is published, and the alert still arrives.
- How a request flows: the decision commits with its outbox row, the API publishes the event to Kafka straight away (six-second timeout), and the consumer creates or closes the alert about a second later. If publishing fails, the request applies the event in-process instead.
- Verifying it: `GET /portal/alerts` reports `delivered_via` for each alert (`kafka` or `inline`). On the hosted site, alerts come back as `kafka`.
- Set `EMBEDDED_KAFKA=false` to turn it off, or set `KAFKA_BOOTSTRAP_SERVERS` and the SASL variables to use a managed broker instead.

A single node with ephemeral storage is fine for a project like this one. A production system would use a managed, replicated broker instead, and the configuration for that already exists; it's just not turned on.
