import json
import threading
import time

import pytest
from elasticsearch import Elasticsearch

from app import generate_logs as g
from app.ship_logs import Shipper, ShipFailure, make_doc_id, send_bulk, validate_event
from tests.fake_es import FakeES


@pytest.fixture
def es():
    f = FakeES()
    yield f
    f.close()


@pytest.fixture
def client(es):
    return Elasticsearch(es.url, request_timeout=3, max_retries=0)


def good(n=1, profile="mixed"):
    import random
    rng = random.Random(0)
    return [g.make_event(rng, profile) for _ in range(n)]


def write(path, events, extra=b""):
    with open(path, "ab") as fh:
        for e in events:
            fh.write(json.dumps(e).encode() + b"\n")
        fh.write(extra)


def mk(client, path, **kw):
    kw.setdefault("sleep", lambda s: None)
    kw.setdefault("failure_cooldown", 0.01)
    return Shipper(client, "application-logs", path, **kw)


# ---------------- validation ----------------
def test_validation_rejects_bad_events():
    ok = good()[0]
    validate_event(dict(ok))
    for mutate, msg in [
        (lambda e: e.pop("@timestamp"), "missing"),
        (lambda e: e.update({"@timestamp": "yesterday"}), "invalid @timestamp"),
        (lambda e: e["log"].update(level="LOUD"), "unknown log.level"),
        (lambda e: e["http"]["response"].update(status_code="500"), "status_code"),
        (lambda e: e["source"].update(ip="999.1.1.1"), "source.ip"),
        (lambda e: e["event"].update(duration=-5), "duration"),
    ]:
        e = json.loads(json.dumps(ok))
        mutate(e)
        with pytest.raises(ValueError, match=msg):
            validate_event(e)


def test_doc_id_is_stable():
    e = {"message": "x"}
    assert make_doc_id(e, b'{"message":"x"}\n') == make_doc_id(e, b'{"message":"x"}')
    assert make_doc_id({"event": {"id": "abc"}}, b"zzz") == "abc"


# ---------------- bulk / retry ----------------
def test_bulk_retries_transient_503_then_succeeds(es, client):
    es.fail_first_n_bulks = 2
    docs = [(f"id{i}", e) for i, e in enumerate(good(5))]
    indexed, rejected = send_bulk(client, "application-logs", docs, max_retries=5, sleep=lambda s: None)
    assert indexed == 5 and not rejected and len(es.docs) == 5 and es.bulk_calls == 3


def test_bulk_gives_up_after_bounded_retries(es, client):
    es.always_503 = True
    with pytest.raises(ShipFailure):
        send_bulk(client, "application-logs", [("a", good()[0])], max_retries=3, sleep=lambda s: None)
    assert es.bulk_calls == 4  # 1 try + 3 retries, no more


def test_bulk_retry_does_not_duplicate_and_item_429_is_retried(es, client):
    es.item_503_once = {"id1"}
    docs = [(f"id{i}", e) for i, e in enumerate(good(3))]
    indexed, _ = send_bulk(client, "application-logs", docs, sleep=lambda s: None)
    assert indexed == 3 and set(es.docs) == {"id0", "id1", "id2"}


def test_bulk_permanent_rejection_is_reported_not_retried(es, client):
    es.reject_ids = {"id1"}
    docs = [(f"id{i}", e) for i, e in enumerate(good(3))]
    indexed, rejected = send_bulk(client, "application-logs", docs, sleep=lambda s: None)
    assert indexed == 2 and [r[0] for r in rejected] == ["id1"] and es.bulk_calls == 1


def test_connection_refused_is_bounded(client):
    dead = Elasticsearch("http://127.0.0.1:9", request_timeout=1, max_retries=0)
    with pytest.raises(ShipFailure):
        send_bulk(dead, "application-logs", [("a", good()[0])], max_retries=2, sleep=lambda s: None)


# ---------------- one-shot shipping ----------------
def test_ships_file_and_dead_letters_bad_lines(es, client, tmp_path):
    p = tmp_path / "app.jsonl"
    write(p, good(10))
    with open(p, "ab") as fh:
        fh.write(b"this is not json\n")
        fh.write(b'{"message": "no timestamp"}\n')
    s = mk(client, p, batch_size=4)
    assert s.run() == 0
    assert len(es.docs) == 10 and s.stats["invalid"] == 2
    dl = [json.loads(x) for x in (tmp_path / "failed-events.jsonl").read_text().splitlines()]
    assert len(dl) == 2 and "missing required fields" in dl[1]["reason"]


def test_restart_does_not_resend(es, client, tmp_path):
    p = tmp_path / "app.jsonl"
    write(p, good(6))
    mk(client, p).run()
    calls = es.bulk_calls
    mk(client, p).run()
    assert es.bulk_calls == calls            # nothing new -> no bulk call
    write(p, good(3, "errors"))
    mk(client, p).run()
    assert len(es.docs) == 9


def test_undelivered_events_are_not_lost(es, client, tmp_path):
    p = tmp_path / "app.jsonl"
    write(p, good(5))
    es.always_503 = True
    s = mk(client, p, max_retries=1)
    assert s.run() == 1                       # reports failure
    assert not (tmp_path / ".ship_state.json").exists()   # position not advanced
    es.always_503 = False
    s2 = mk(client, p)
    assert s2.run() == 0 and len(es.docs) == 5            # nothing lost after ES recovers


def test_partial_last_line_is_not_parsed(es, client, tmp_path):
    p = tmp_path / "app.jsonl"
    write(p, good(2), extra=b'{"@timestamp": "2026-01-01T00:0')
    s = mk(client, p)
    s.run()
    assert len(es.docs) == 2 and s.stats["invalid"] == 0


# ---------------- follow mode + graceful shutdown ----------------
def test_follow_mode_ships_new_lines_and_stops_cleanly(es, client, tmp_path):
    p = tmp_path / "app.jsonl"
    write(p, good(3))
    s = mk(client, p, follow=True, flush_seconds=0.2, poll_seconds=0.05)
    result = {}
    t = threading.Thread(target=lambda: result.update(code=s.run()))
    t.start()
    deadline = time.time() + 5
    while len(es.docs) < 3 and time.time() < deadline:
        time.sleep(0.05)
    write(p, good(4, "errors"))
    while len(es.docs) < 7 and time.time() < deadline:
        time.sleep(0.05)
    write(p, good(2))                         # written right before shutdown
    s.request_stop()
    t.join(5)
    assert not t.is_alive() and result["code"] == 0
    assert len(es.docs) >= 7
    state = json.loads((tmp_path / ".ship_state.json").read_text())
    assert state["offset"] == p.stat().st_size or len(es.docs) == 7


def test_follow_mode_survives_outage(es, client, tmp_path):
    p = tmp_path / "app.jsonl"
    write(p, good(3))
    es.fail_first_n_bulks = 6                 # > max_retries -> first flush fails, later one succeeds
    s = mk(client, p, follow=True, flush_seconds=0.1, poll_seconds=0.05, max_retries=2)
    t = threading.Thread(target=s.run)
    t.start()
    deadline = time.time() + 8
    while len(es.docs) < 3 and time.time() < deadline:
        time.sleep(0.05)
    s.request_stop()
    t.join(5)
    assert len(es.docs) == 3 and s.stats["failed_flushes"] >= 1
