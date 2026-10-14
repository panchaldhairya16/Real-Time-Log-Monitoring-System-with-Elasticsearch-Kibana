#!/usr/bin/env python3
"""Realistic JSON Lines application log generator (ECS-style field names).

Examples
--------
  python app/generate_logs.py                       # mixed traffic, 1 event / 0.5 s
  python app/generate_logs.py --profile normal      # only INFO / DEBUG
  python app/generate_logs.py --profile warnings    # only WARNING
  python app/generate_logs.py --profile errors      # only ERROR
  python app/generate_logs.py --spike 12            # burst of 12 ERROR events, then exit
  python app/generate_logs.py --count 100 --interval 0.05
"""
from __future__ import annotations

import argparse
import json
import random
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SERVICES = ["checkout-service", "auth-service", "inventory-service", "payment-service", "search-service"]
HOSTS = ["web-01", "web-02", "web-03"]
PATHS = ["/api/products", "/api/cart", "/api/orders", "/api/search", "/api/users/me", "/health"]
USERS = ["alice", "bob", "carol", "dave", "erin", "mallory"]
MS = 1_000_000  # event.duration is in NANOSECONDS (ECS convention); 1 ms = 1,000,000 ns


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def random_ip(rng: random.Random) -> str:
    return f"{rng.choice([10, 172, 192, 203])}.{rng.randint(0, 255)}.{rng.randint(0, 255)}.{rng.randint(1, 254)}"


def build_event(rng, level, message, status, duration_ms, method=None, error_type=None,
                user=None, service=None, path=None) -> dict:
    event = {
        "@timestamp": now_iso(),
        "log": {"level": level},
        "message": message,
        "service": {"name": service or rng.choice(SERVICES)},
        "host": {"name": rng.choice(HOSTS)},
        "http": {
            "request": {"method": method or rng.choice(["GET", "GET", "GET", "POST", "PUT", "DELETE"])},
            "response": {"status_code": status},
        },
        "url": {"path": path or rng.choice(PATHS)},
        "event": {"id": str(uuid.uuid4()), "duration": int(duration_ms * MS)},
        "source": {"ip": random_ip(rng)},
    }
    if error_type:
        event["error"] = {"type": error_type}
    if user:
        event["user"] = {"name": user}
    return event


# ---- Scenarios: each returns one event dict ---------------------------------
def success_200(rng):
    status = rng.choice([200, 200, 200, 201, 204])
    return build_event(rng, "INFO", "Request completed successfully", status, rng.uniform(15, 350))


def debug_event(rng):
    return build_event(rng, "DEBUG", rng.choice(["Cache hit for session lookup", "Connection pool size=10 active=3",
                                                  "Feature flag evaluated"]), 200, rng.uniform(1, 20), method="GET")


def not_found_404(rng):
    return build_event(rng, "WARNING", "Resource not found", 404, rng.uniform(10, 120), method="GET")


def slow_request(rng):
    dur = rng.uniform(2000, 6000)
    return build_event(rng, "WARNING", f"Slow request detected ({dur:.0f} ms)", 200, dur)


def auth_failure(rng):
    user = rng.choice(USERS)
    return build_event(rng, "WARNING", f"Authentication failed for user '{user}'", rng.choice([401, 403]),
                       rng.uniform(30, 200), method="POST", error_type="AuthenticationError", user=user,
                       service="auth-service", path="/api/login")


def server_error_500(rng):
    return build_event(rng, "ERROR", "Internal server error while processing request", 500,
                       rng.uniform(100, 1500), error_type="InternalServerError")


def db_failure(rng):
    return build_event(rng, "ERROR", "Database connection failed: could not connect to db-primary:5432",
                       rng.choice([500, 503]), rng.uniform(1000, 5000), error_type="DatabaseConnectionError")


PROFILES = {
    "mixed": [(success_200, 55), (debug_event, 8), (not_found_404, 10), (slow_request, 6),
              (auth_failure, 7), (server_error_500, 8), (db_failure, 6)],
    "normal": [(success_200, 85), (debug_event, 15)],
    "warnings": [(not_found_404, 40), (slow_request, 30), (auth_failure, 30)],
    "errors": [(server_error_500, 60), (db_failure, 40)],
}


def make_event(rng: random.Random, profile: str = "mixed") -> dict:
    funcs, weights = zip(*PROFILES[profile])
    return rng.choices(funcs, weights=weights, k=1)[0](rng)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--output", default="logs/app.jsonl", help="JSONL file to append to (default: %(default)s)")
    p.add_argument("--interval", type=float, default=0.5, help="Seconds between events (default: %(default)s)")
    p.add_argument("--count", type=int, default=0, help="Stop after N events (0 = run until Ctrl+C)")
    p.add_argument("--profile", choices=sorted(PROFILES), default="mixed", help="Traffic mix (default: %(default)s)")
    p.add_argument("--spike", type=int, default=0, metavar="N",
                   help="Write a burst of N ERROR events (HTTP 500 / DB failures) quickly, then exit")
    p.add_argument("--spike-interval", type=float, default=0.05, help="Seconds between spike events")
    p.add_argument("--seed", type=int, default=None, help="Random seed for reproducible output")
    args = p.parse_args(argv)
    if args.interval < 0 or args.spike < 0 or args.count < 0:
        p.error("--interval, --count and --spike must not be negative")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    rng = random.Random(args.seed)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    written = 0
    with out.open("a", encoding="utf-8") as fh:
        def emit(event):
            nonlocal written
            fh.write(json.dumps(event, separators=(",", ":")) + "\n")
            fh.flush()  # make the line visible to the shipper immediately
            written += 1

        if args.spike:
            print(f"Writing error spike of {args.spike} events to {out} ...")
            for _ in range(args.spike):
                if stop["flag"]:
                    break
                emit(make_event(rng, "errors"))
                time.sleep(args.spike_interval)
        else:
            print(f"Generating '{args.profile}' logs to {out} every {args.interval}s (Ctrl+C to stop)")
            while not stop["flag"] and (args.count == 0 or written < args.count):
                emit(make_event(rng, args.profile))
                time.sleep(args.interval)
    print(f"Done. {written} events written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
