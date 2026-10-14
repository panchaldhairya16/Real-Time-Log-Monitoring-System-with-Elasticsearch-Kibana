"""Shared configuration + Elasticsearch client factory.

All settings come from environment variables (optionally loaded from a .env file).
"""
from __future__ import annotations

import os
from pathlib import Path

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:  # pragma: no cover
    pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

ES_URL = os.getenv("ES_URL", "http://localhost:9200")
ES_INDEX = os.getenv("ES_INDEX", "application-logs")
ES_API_KEY = os.getenv("ES_API_KEY", "")
ES_USERNAME = os.getenv("ES_USERNAME", "")
ES_PASSWORD = os.getenv("ES_PASSWORD", "")
LOG_FILE = os.getenv("LOG_FILE", "logs/app.jsonl")
MAPPING_FILE = PROJECT_ROOT / "config" / "index-mapping.json"


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        raise SystemExit(f"Environment variable {name} must be an integer")


SHIP_BATCH_SIZE = _int_env("SHIP_BATCH_SIZE", 200)
SHIP_FLUSH_SECONDS = _int_env("SHIP_FLUSH_SECONDS", 2)
SHIP_MAX_RETRIES = _int_env("SHIP_MAX_RETRIES", 5)


def make_client(url: str | None = None, request_timeout: int = 15):
    """Build an Elasticsearch client from environment settings."""
    from elasticsearch import Elasticsearch

    # max_retries=0: the shipper implements its own bounded retry/backoff, so the client must not
    # silently add a second, hidden retry layer on top.
    kwargs = {"request_timeout": request_timeout, "max_retries": 0}
    if ES_API_KEY:
        kwargs["api_key"] = ES_API_KEY
    elif ES_USERNAME:
        kwargs["basic_auth"] = (ES_USERNAME, ES_PASSWORD)
    return Elasticsearch(url or ES_URL, **kwargs)
