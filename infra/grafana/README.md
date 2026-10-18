# Grafana dashboard

`invoiceiq-pipeline.json` charts the event pipeline: consumer lag, age of the oldest unconsumed event, unpublished
outbox rows, dead letters by topic and throughput.

1. Enable metrics on the API: `METRICS_ENABLED=true` and a `METRICS_TOKEN`.
2. Scrape `/internal/metrics` with the bearer token from Prometheus.
3. In Grafana choose Dashboards, then Import, upload the JSON and pick your Prometheus data source.

Suggested alerts: lag above 0 for 5 minutes, any dead letter, unpublished outbox rows growing.
