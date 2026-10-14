#!/usr/bin/env python3
"""Check connectivity, index existence, document counts and the latest events."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402


def main() -> int:
    client = config.make_client()
    try:
        info = client.info()
    except Exception as exc:  # noqa: BLE001
        print(f"FAIL  cannot reach Elasticsearch at {config.ES_URL}: {exc}")
        return 1
    print(f"OK    connected to Elasticsearch {info['version']['number']} (cluster '{info['cluster_name']}')")

    health = client.cluster.health()
    print(f"OK    cluster health: {health['status']}")

    if not client.indices.exists(index=config.ES_INDEX):
        print(f"FAIL  index '{config.ES_INDEX}' does not exist - run: python scripts/setup-index.py")
        return 1
    print(f"OK    index '{config.ES_INDEX}' exists")

    total = client.count(index=config.ES_INDEX)["count"]
    print(f"INFO  total documents: {total}")
    if total == 0:
        print("WARN  index is empty - generate and ship some logs (see README)")
        return 0

    agg = client.search(index=config.ES_INDEX, size=0, aggs={
        "by_level": {"terms": {"field": "log.level"}},
        "by_status": {"terms": {"field": "http.response.status_code", "size": 10}},
    })["aggregations"]
    print("INFO  by log.level:   ", {b["key"]: b["doc_count"] for b in agg["by_level"]["buckets"]})
    print("INFO  by status code: ", {b["key"]: b["doc_count"] for b in agg["by_status"]["buckets"]})

    latest = client.search(index=config.ES_INDEX, size=3, sort=[{"@timestamp": "desc"}])["hits"]["hits"]
    print("INFO  latest events:")
    for h in latest:
        s = h["_source"]
        print(f"        {s['@timestamp']}  {s['log']['level']:<7} {s['service']['name']:<18} {s['message']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
