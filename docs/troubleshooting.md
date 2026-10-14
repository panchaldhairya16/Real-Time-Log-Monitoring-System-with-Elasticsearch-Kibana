# Troubleshooting

Work from the left of the pipeline to the right: **file → shipper → Elasticsearch → Kibana → alert**.

## 1. Is the generator writing?
```bash
tail -n 3 logs/app.jsonl         # new lines should appear
wc -l logs/app.jsonl
```

## 2. Is Elasticsearch healthy?
```bash
docker compose ps
docker compose logs --tail=50 elasticsearch
curl -s localhost:9200/_cluster/health?pretty      # green or yellow is fine for one node
```
| Problem | Fix |
|---|---|
| Container exits / restarts | Check RAM (`free -h`). `sudo sysctl -w vm.max_map_count=262144`. |
| `memory locking requested ... not locked` | Delete `bootstrap.memory_lock=true` and the `ulimits` block from the compose file. |
| `permission denied` talking to Docker | `sudo usermod -aG docker $USER`, log out/in. |
| Port already in use | Change `ES_PORT` / `KIBANA_PORT` in `.env` (and `ES_URL`), then `docker compose up -d`. |
| Cluster `red` | `curl 'localhost:9200/_cluster/allocation/explain?pretty'`; low disk is common. |

## 3. Shipper errors
| Message | Meaning | Fix |
|---|---|---|
| `Elasticsearch is unreachable` | Cannot connect at startup | Start/wait for the stack; check `ES_URL` |
| `Index 'application-logs' does not exist` | Index not created | `python scripts/setup-index.py` |
| `Retry n/5 ... connection problem` | Elasticsearch down or restarting | Wait; shipper continues automatically |
| `Could not deliver batch ... Position NOT advanced` | All retries failed | Fix Elasticsearch; events are re-sent automatically (follow mode) or on next run |
| `Elasticsearch rejected event <id>: mapper_parsing_exception / strict_dynamic_mapping_exception` | Event does not fit the mapping | Inspect `logs/failed-events.jsonl`; fix the producer or extend the mapping |
| `Invalid event skipped` | Missing field, bad timestamp, bad type | See `reason` in `logs/failed-events.jsonl` |
| `HTTP 401/403` | Security enabled but no credentials | Set `ES_API_KEY` or `ES_USERNAME`/`ES_PASSWORD` |

Re-ship everything from scratch: stop the shipper, `rm logs/.ship_state.json`, run again. Stable IDs prevent duplicates.
Use `--log-level DEBUG` for more detail.

## 4. Documents counted but Kibana shows nothing
1. Time picker (top right) must cover the events: use *Last 15 minutes* or *Last 24 hours*.
2. Data view must use `@timestamp` as its time field and the pattern `application-logs`.
3. Remove stray filters/queries in the search bar; check you picked the right data view.
4. Clock/timezone confusion: events are UTC; Kibana displays in the browser's timezone.
5. Refresh the data view (Stack Management → Data Views → refresh icon) if you recreated the index.

## 5. Kibana does not start
```bash
docker compose logs --tail=80 kibana
curl -s localhost:5601/api/status | head -c 300
```
Kibana waits for Elasticsearch; give it 1–3 minutes after Elasticsearch is healthy.
Versions of Kibana and Elasticsearch must be identical (`STACK_VERSION`).

## 6. Alert does not fire
1. Rule → *Execution history*: is it running every minute? Any errors?
2. Does the rule query match? Copy the KQL into Discover with the same 5-minute window.
3. Are ≥ 5 ERROR events inside the last 5 minutes *by `@timestamp`*? Old events don't count.
4. Is the shipper still running and shipping? (`verify-elasticsearch.py`)
5. Threshold: "IS ABOVE 4" means 5 or more.
6. Action not delivered: check the connector (use *Test* on the connector page), `docker compose logs kibana`, and the
   destination (index `alert-notifications`, Mailpit at :8025). Email host must be `mailpit` (container name), not `localhost`.
7. After restarting Kibana rules failing to decrypt: `KIBANA_ENCRYPTION_KEY` changed - recreate the rules/connectors.

## 7. Useful one-liners
```bash
curl -s localhost:9200/_cat/indices?v
curl -s localhost:9200/application-logs/_count
curl -s 'localhost:9200/application-logs/_search?q=log.level:ERROR&size=3&pretty'
docker compose logs -f kibana | grep -i -E "alert|rule|action"
python -m pytest -q
```
