import json
import random

from app import generate_logs as g
from app.ship_logs import validate_event


def test_all_profiles_produce_valid_events():
    rng = random.Random(1)
    for profile in g.PROFILES:
        for _ in range(200):
            validate_event(g.make_event(rng, profile))


def test_profile_levels():
    rng = random.Random(2)
    assert {g.make_event(rng, "normal")["log"]["level"] for _ in range(300)} <= {"INFO", "DEBUG"}
    assert {g.make_event(rng, "warnings")["log"]["level"] for _ in range(300)} == {"WARNING"}
    errs = [g.make_event(rng, "errors") for _ in range(300)]
    assert {e["log"]["level"] for e in errs} == {"ERROR"}
    assert {e["http"]["response"]["status_code"] for e in errs} <= {500, 503}


def test_mixed_covers_every_required_scenario():
    rng = random.Random(3)
    events = [g.make_event(rng, "mixed") for _ in range(3000)]
    assert {e["log"]["level"] for e in events} == {"INFO", "DEBUG", "WARNING", "ERROR"}
    statuses = {e["http"]["response"]["status_code"] for e in events}
    assert {200, 404, 500, 401}.issubset(statuses)
    assert any(e.get("error", {}).get("type") == "DatabaseConnectionError" for e in events)
    assert any(e.get("error", {}).get("type") == "AuthenticationError" for e in events)
    assert any(e["event"]["duration"] > 2_000_000_000 for e in events)  # slow request


def test_spike_cli_writes_n_errors(tmp_path):
    out = tmp_path / "a.jsonl"
    assert g.main(["--output", str(out), "--spike", "7", "--spike-interval", "0"]) == 0
    lines = [json.loads(x) for x in out.read_text().splitlines()]
    assert len(lines) == 7 and all(e["log"]["level"] == "ERROR" for e in lines)


def test_count_cli(tmp_path):
    out = tmp_path / "a.jsonl"
    g.main(["--output", str(out), "--count", "5", "--interval", "0", "--profile", "normal", "--seed", "1"])
    assert len(out.read_text().splitlines()) == 5
