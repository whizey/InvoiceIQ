# Embedded single-node Kafka on the free hosting tier

**Context.** The free hosting tier runs one container, and no free managed Kafka is available.

**Decision.** Run Kafka (KRaft) beside the API with a bundled, checksum-pinned archive, and fall back to in-process delivery if it is down.

**Consequences.** Storage is ephemeral, which the outbox and reconciler cover; a managed replicated cluster is a configuration change away.
