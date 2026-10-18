#!/usr/bin/env bash
# Start a single-node Apache Kafka (KRaft mode, no ZooKeeper) beside the API inside one container.
# Storage is ephemeral: after a restart the broker starts empty, which is safe because every event is
# first written to the Postgres outbox and applied idempotently (see docs/EVENT_PIPELINE.md).
set -euo pipefail

VERSION="${KAFKA_VERSION:-3.9.0}"
BASE="${KAFKA_BASE_DIR:-/tmp/invoiceiq-kafka}"
PORT="${KAFKA_PORT:-9092}"
CONTROLLER_PORT="${KAFKA_CONTROLLER_PORT:-9093}"
ARCHIVE="kafka_2.13-${VERSION}.tgz"
URL="https://archive.apache.org/dist/kafka/${VERSION}/${ARCHIVE}"
HOME_DIR="$BASE/kafka_2.13-${VERSION}"
TOPICS=(risk.scored review.requested review.resolved risk.scored.dlq review.requested.dlq review.resolved.dlq)

mkdir -p "$BASE"
if [ ! -x "$HOME_DIR/bin/kafka-server-start.sh" ]; then
  curl -fsSL --retry 3 -o "$BASE/$ARCHIVE" "$URL"
  expected="$(curl -fsSL --retry 3 "$URL.sha512" | tr -d ' \n' | sed 's/^[^:]*://' | tr 'A-F' 'a-f')"
  actual="$(sha512sum "$BASE/$ARCHIVE" 2>/dev/null | cut -d' ' -f1 || shasum -a 512 "$BASE/$ARCHIVE" | cut -d' ' -f1)"
  [ "$expected" = "$actual" ] || { echo "Kafka archive checksum mismatch" >&2; exit 1; }
  tar -xzf "$BASE/$ARCHIVE" -C "$BASE"
fi

CONFIG="$BASE/server.properties"
DATA="$BASE/data"
cat > "$CONFIG" <<EOF
process.roles=broker,controller
node.id=1
controller.quorum.voters=1@127.0.0.1:${CONTROLLER_PORT}
listeners=PLAINTEXT://127.0.0.1:${PORT},CONTROLLER://127.0.0.1:${CONTROLLER_PORT}
advertised.listeners=PLAINTEXT://127.0.0.1:${PORT}
controller.listener.names=CONTROLLER
inter.broker.listener.name=PLAINTEXT
listener.security.protocol.map=CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT
log.dirs=${DATA}
num.partitions=1
offsets.topic.replication.factor=1
transaction.state.log.replication.factor=1
transaction.state.log.min.isr=1
auto.create.topics.enable=false
group.initial.rebalance.delay.ms=0
log.retention.hours=24
EOF

if [ ! -f "$DATA/meta.properties" ]; then
  "$HOME_DIR/bin/kafka-storage.sh" format -t "$("$HOME_DIR/bin/kafka-storage.sh" random-uuid)" -c "$CONFIG" >/dev/null
fi

export KAFKA_HEAP_OPTS="${KAFKA_HEAP_OPTS:--Xms128m -Xmx512m}"
nohup "$HOME_DIR/bin/kafka-server-start.sh" "$CONFIG" > "$BASE/kafka.log" 2>&1 &

for _ in $(seq 1 90); do
  if (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then break; fi
  sleep 1
done
(exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null || { echo "Kafka did not start; see $BASE/kafka.log" >&2; exit 1; }

for topic in "${TOPICS[@]}"; do
  "$HOME_DIR/bin/kafka-topics.sh" --bootstrap-server "127.0.0.1:${PORT}" --create --if-not-exists \
    --topic "$topic" --partitions 1 --replication-factor 1 >/dev/null
done
echo "Kafka ${VERSION} ready on 127.0.0.1:${PORT}"
