#!/bin/sh
# Create the platform's Kafka topics (docs/ARCHITECTURE.md §8, ADR-0002).
# Run once per `docker compose up` by the kafka-init service; idempotent.
# Existing topics with fewer partitions are grown (never shrunk: Kafka
# can't), so a volume from before Day 4, when topics were auto-created
# with 1 partition, is fixed in place.
set -eu

BOOTSTRAP="${BOOTSTRAP:-kafka:9092}"
PARTITIONS="${PARTITIONS:-12}"
TOPICS="alert.received alert.decided verdict.recorded alert.received.dlq"
KT=/opt/kafka/bin/kafka-topics.sh

for topic in $TOPICS; do
  "$KT" --bootstrap-server "$BOOTSTRAP" --create --if-not-exists \
    --topic "$topic" --partitions "$PARTITIONS" --replication-factor 1
  current=$("$KT" --bootstrap-server "$BOOTSTRAP" --describe --topic "$topic" \
    | sed -n 's/.*PartitionCount: *\([0-9]*\).*/\1/p' | head -n 1)
  if [ "$current" -lt "$PARTITIONS" ]; then
    echo "growing $topic from $current to $PARTITIONS partitions"
    "$KT" --bootstrap-server "$BOOTSTRAP" --alter --topic "$topic" --partitions "$PARTITIONS"
  fi
  echo "$topic: ok"
done
