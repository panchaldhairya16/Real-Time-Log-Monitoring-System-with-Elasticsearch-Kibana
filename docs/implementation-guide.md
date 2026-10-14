# Implementation Guide – four-hour lab plan

| Time | Step | Outcome |
|---|---|---|
| 0:00–0:20 | **Concepts.** Read README §2 and `architecture.md`. Check Docker/Python prerequisites | You can draw the pipeline from memory |
| 0:20–0:45 | **Stack.** `cp .env.example .env`, `docker compose up -d`, wait for healthy, open Kibana | Both services healthy; `curl localhost:9200` works |
| 0:45–1:10 | **Index.** Read `config/index-mapping.json`; run `scripts/setup-index.py`; inspect `_mapping` | Explain each field type choice |
| 1:10–1:40 | **Generator.** Read `generate_logs.py`; run each `--profile`; inspect `logs/app.jsonl` | Understand scenarios and ECS field names |
| 1:40–2:10 | **Shipper.** Read `ship_logs.py` (validation, `send_bulk`, offset); run `--follow`; run `verify-elasticsearch.py` | Events flow end to end |
| 2:10–2:50 | **Kibana.** Data view, Discover + KQL, 7 panels, dashboard (README §10) | Dashboard `Real-Time Application Log Monitoring` live |
| 2:50–3:20 | **Alerting.** Create Rule 1 and Rule 2, Server log + Index connector; run `--spike 12`; inspect alert | Active alert + delivered Index document |
| 3:20–3:45 | **Resilience tests.** Scenarios 7, 7b, 7c from README §12; break something on purpose and diagnose with `troubleshooting.md` | Evidence of retry and no loss/duplicates |
| 3:45–4:00 | **Wrap-up.** Screenshots checklist, shutdown (`docker compose stop`), limitations discussion | Report-ready material |

Stretch goals if time remains: Mailpit email action, `python -m pytest -q`, extend mapping with a new field end to end.

## Final demonstration checklist (10–15 minutes)

1. [ ] `docker compose ps` – both services healthy (explain pinned versions, single node, security off = localhost only).
2. [ ] Show the mapping and explain keyword vs text vs ip vs date; show `dynamic: strict`.
3. [ ] Start shipper (`--follow`) and generator (`mixed`); point at `Indexed N event(s)` lines.
4. [ ] Run `verify-elasticsearch.py`; show a raw document with `_search`.
5. [ ] Show the dashboard updating live; explain each panel and one KQL filter.
6. [ ] Run `generate_logs.py --spike 12`; show Rule 1 turning **Active**.
7. [ ] Show the *delivery* evidence (Index connector document / Mailpit email) and explain alert vs notification.
8. [ ] `docker compose stop elasticsearch` for ~30 s, then start – show shipper retries and recovery with no loss.
9. [ ] Ctrl+C the shipper – show the clean shutdown message.
10. [ ] State honestly what is manual (dashboard, rules) and the limitations; mention future work (security, ILM, Filebeat).
