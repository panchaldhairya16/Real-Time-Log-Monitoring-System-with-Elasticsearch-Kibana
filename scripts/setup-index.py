#!/usr/bin/env python3
"""Create the application-logs index with the explicit mapping in config/index-mapping.json.

  python scripts/setup-index.py              # create if missing
  python scripts/setup-index.py --recreate   # DELETE and recreate (destroys indexed data!)
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--recreate", action="store_true", help="delete the index first (data loss!)")
    p.add_argument("--wait", type=int, default=60, help="seconds to wait for Elasticsearch (default 60)")
    args = p.parse_args()

    client = config.make_client()
    deadline = time.time() + args.wait
    while True:
        try:
            client.info()
            break
        except Exception as exc:  # noqa: BLE001
            if time.time() > deadline:
                print(f"ERROR: cannot reach Elasticsearch at {config.ES_URL}: {exc}\n"
                      "Hint: run `docker compose ps` and wait until elasticsearch is 'healthy'.")
                return 1
            print("Waiting for Elasticsearch ...")
            time.sleep(3)

    body = json.loads(config.MAPPING_FILE.read_text())
    exists = client.indices.exists(index=config.ES_INDEX)
    if exists and args.recreate:
        client.indices.delete(index=config.ES_INDEX)
        print(f"Deleted index '{config.ES_INDEX}'")
        exists = False
    if exists:
        print(f"Index '{config.ES_INDEX}' already exists - nothing to do (use --recreate to rebuild it).")
        return 0
    client.indices.create(index=config.ES_INDEX, settings=body["settings"], mappings=body["mappings"])
    print(f"Created index '{config.ES_INDEX}' with mapping from {config.MAPPING_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
