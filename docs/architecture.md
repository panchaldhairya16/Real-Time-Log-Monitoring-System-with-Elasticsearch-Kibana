# Architecture

## Data flow

```mermaid
sequenceDiagram
    participant App as generate_logs.py
    participant File as logs/app.jsonl
    participant Ship as ship_logs.py
    participant ES as Elasticsearch
    participant Kib as Kibana
    App->>File: append one JSON line per event (flush)
    Ship->>File: read complete lines from saved offset
    Ship->>Ship: validate fields, timestamp, types; compute stable _id
    Ship->>ES: PUT /_bulk (batch of up to 200)
    ES-->>Ship: per-item results (201 / 429 / 400 ...)
    Ship->>Ship: retry 429/503/timeouts with backoff (bounded); dead-letter permanent rejects
    Ship->>Ship: commit offset to .ship_state.json ONLY after success
    Kib->>ES: search / aggregate / rule queries
```

## Components

| Component | Role | Key design decision |
|---|---|---|
| `generate_logs.py` | Simulates an application | ECS-style nested JSON; weighted scenarios; `--spike` for alert tests |
| `ship_logs.py` | Moves logs into Elasticsearch | Offset committed after success => at-least-once delivery; stable `_id` => effectively-once results |
| Elasticsearch | Stores and indexes events | Explicit mapping, `dynamic: strict`, 1 shard / 0 replicas |
| Kibana | Explore, dashboard, alerting | Manual setup; alerting keys configured so rules persist |

## Delivery guarantees

- A batch advances the saved offset only after Elasticsearch confirmed every item or it was dead-lettered.
- If Elasticsearch is down, retries are bounded (`SHIP_MAX_RETRIES`); then the batch is abandoned *in memory only* and
  re-read from the file after a cool-down. The file is the durable buffer, so nothing is lost while the file exists.
- Re-sending the same line yields the same `_id` (`event.id`, else SHA-1 of the line), so it overwrites instead of duplicating.
- Invalid lines are written to `failed-events.jsonl` with a reason – never silently dropped.

## Field reference

| Field | Type | Notes |
|---|---|---|
| `@timestamp` | date | UTC ISO-8601 |
| `log.level` | keyword | DEBUG / INFO / WARNING / ERROR / CRITICAL |
| `message` | text | Full-text searchable |
| `service.name`, `host.name`, `http.request.method`, `url.path`, `error.type`, `user.name`, `event.id` | keyword | Good for aggregations |
| `http.response.status_code` | integer | Range queries (`>= 500`) work |
| `event.duration` | long | Nanoseconds |
| `source.ip` | ip | Supports CIDR queries |

## Security posture

Local lab: security disabled, ports bound to 127.0.0.1, encryption keys are demo values. To harden: enable
`xpack.security`, create an API key for the shipper (`ES_API_KEY`), serve over HTTPS, use real secrets.
