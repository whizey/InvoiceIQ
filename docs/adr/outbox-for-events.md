# Transactional outbox for events

**Context.** Decisions must announce themselves without a distributed transaction between PostgreSQL and Kafka.

**Decision.** Write the event row in the same transaction as the decision; publish afterwards; make consumers idempotent on event_id; add a reconciler for events the broker loses.

**Consequences.** Delivery is at-least-once and consumers must deduplicate; the reconciler makes effects independent of broker durability.
