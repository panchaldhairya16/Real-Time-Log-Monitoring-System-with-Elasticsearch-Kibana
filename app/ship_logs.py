#!/usr/bin/env python3
"""Ship JSON Lines logs to Elasticsearch using the bulk API.

Features
  * reads a .jsonl file once, or follows it like `tail -f` (--follow)
  * validates every event (required fields, timestamp, numeric types, IP)
  * bulk indexing with BOUNDED retries and exponential backoff
  * stable document IDs (event.id, or SHA-1 of the line) => retries never create duplicates
  * remembers its position in a small state file, so restarts do not re-send or skip events
  * bad events go to a dead-letter file instead of being silently dropped
  * SIGINT / SIGTERM: flushes the pending batch, saves position, exits cleanly

If Elasticsearch stays unreachable, the shipper does NOT advance its saved position,
so nothing is lost: the events stay in the log file and are re-sent later.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import random
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

# Allow `python app/ship_logs.py` from the project root as well as `python -m app.ship_logs`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

log = logging.getLogger("shipper")

REQUIRED_FIELDS = ["@timestamp", "log.level", "message", "service.name", "host.name"]
VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
RETRYABLE_ITEM_STATUS = {429, 502, 503, 504}


class ShipFailure(Exception):
    """Raised when a batch could not be delivered after all bounded retries."""


# --------------------------------------------------------------------------- validation
def get_path(obj: dict, dotted: str):
    cur = obj
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def parse_timestamp(value) -> datetime:
    if not isinstance(value, str):
        raise ValueError("@timestamp must be an ISO-8601 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid @timestamp {value!r}") from exc


def validate_event(event) -> dict:
    """Return the (normalised) event or raise ValueError describing the problem."""
    if not isinstance(event, dict):
        raise ValueError("line is JSON but not an object")
    missing = [f for f in REQUIRED_FIELDS if get_path(event, f) in (None, "")]
    if missing:
        raise ValueError(f"missing required fields: {', '.join(missing)}")
    parse_timestamp(event["@timestamp"])

    level = str(event["log"]["level"]).upper()
    if level not in VALID_LEVELS:
        raise ValueError(f"unknown log.level {level!r}")
    event["log"]["level"] = level

    status = get_path(event, "http.response.status_code")
    if status is not None and (isinstance(status, bool) or not isinstance(status, int) or not 100 <= status <= 599):
        raise ValueError(f"http.response.status_code must be an integer 100-599, got {status!r}")
    duration = get_path(event, "event.duration")
    if duration is not None and (isinstance(duration, bool) or not isinstance(duration, int) or duration < 0):
        raise ValueError(f"event.duration must be a non-negative integer (nanoseconds), got {duration!r}")
    ip = get_path(event, "source.ip")
    if ip is not None:
        try:
            ipaddress.ip_address(ip)
        except ValueError as exc:
            raise ValueError(f"invalid source.ip {ip!r}") from exc
    return event


def make_doc_id(event: dict, raw_line: bytes) -> str:
    """Stable ID: the same line always produces the same ID, so re-sending overwrites, never duplicates."""
    event_id = get_path(event, "event.id")
    if event_id:
        return str(event_id)
    return hashlib.sha1(raw_line.strip()).hexdigest()


# --------------------------------------------------------------------------- bulk with retries
def send_bulk(client, index: str, docs: list[tuple[str, dict]], max_retries: int = 5,
              sleep=time.sleep, should_abort=lambda: False):
    """Index docs = [(doc_id, source), ...].

    Returns (indexed_count, permanent_failures) where permanent_failures is a list of
    (doc_id, source, reason) that Elasticsearch rejected for a non-retryable reason (e.g. mapping error).
    Raises ShipFailure if transient problems persist after `max_retries` retries.
    """
    from elastic_transport import ConnectionError as TransportConnectionError
    from elastic_transport import ConnectionTimeout, TransportError
    from elasticsearch import ApiError

    pending = list(docs)
    indexed = 0
    permanent: list[tuple[str, dict, str]] = []
    last_error = "unknown error"

    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = min(2 ** (attempt - 1), 30) + random.uniform(0, 0.5)
            log.warning("Retry %d/%d in %.1fs (%d events pending). Last error: %s",
                        attempt, max_retries, delay, len(pending), last_error)
            if should_abort():
                raise ShipFailure("interrupted by shutdown while retrying; events remain in the log file")
            sleep(delay)

        operations = []
        for doc_id, source in pending:
            operations.append({"index": {"_index": index, "_id": doc_id}})
            operations.append(source)

        try:
            resp = client.bulk(operations=operations, refresh=False)
        except (TransportConnectionError, ConnectionTimeout) as exc:
            last_error = f"connection problem: {exc}"
            continue
        except ApiError as exc:
            if exc.status_code in RETRYABLE_ITEM_STATUS:
                last_error = f"HTTP {exc.status_code} from Elasticsearch"
                continue
            raise ShipFailure(f"Elasticsearch rejected the bulk request: HTTP {exc.status_code}: {exc.message}") from exc
        except TransportError as exc:
            last_error = f"transport error: {exc}"
            continue

        still_pending = []
        for (doc_id, source), item in zip(pending, resp["items"]):
            result = next(iter(item.values()))
            status = result.get("status", 0)
            if status < 300:
                indexed += 1
            elif status in RETRYABLE_ITEM_STATUS:
                still_pending.append((doc_id, source))
                last_error = f"item status {status}"
            else:
                reason = json.dumps(result.get("error", result))
                permanent.append((doc_id, source, reason))
        pending = still_pending
        if not pending:
            return indexed, permanent

    raise ShipFailure(f"gave up after {max_retries} retries; last error: {last_error}")


# --------------------------------------------------------------------------- shipper
class Shipper:
    def __init__(self, client, index, path, state_path=None, dead_letter_path=None, batch_size=200,
                 flush_seconds=2.0, max_retries=5, follow=False, tail=False, poll_seconds=0.5,
                 failure_cooldown=5.0, sleep=time.sleep):
        self.client = client
        self.index = index
        self.path = Path(path)
        self.state_path = Path(state_path) if state_path else self.path.with_name(".ship_state.json")
        self.dead_letter_path = Path(dead_letter_path) if dead_letter_path else self.path.with_name("failed-events.jsonl")
        self.batch_size = batch_size
        self.flush_seconds = flush_seconds
        self.max_retries = max_retries
        self.follow = follow
        self.tail = tail
        self.poll_seconds = poll_seconds
        self.failure_cooldown = failure_cooldown
        self._sleep = sleep
        self.stop_requested = False
        self.stats = {"read": 0, "indexed": 0, "invalid": 0, "rejected": 0, "batches": 0, "failed_flushes": 0}

        self._docs: list[tuple[str, dict]] = []
        self._bad: list[tuple[bytes, str]] = []
        self._batch_end = 0          # file offset after the last line in the current batch
        self._committed = 0          # offset safely stored in the state file
        self._inode = None
        self._last_flush = time.monotonic()

    # ---- state ----------------------------------------------------------
    def _load_state(self, size: int, inode: int) -> int:
        try:
            state = json.loads(self.state_path.read_text())
            if state.get("inode") == inode and 0 <= state.get("offset", 0) <= size:
                return int(state["offset"])
            log.info("State file does not match the log file (rotated/truncated?) - starting from the beginning")
        except FileNotFoundError:
            pass
        except (ValueError, OSError) as exc:
            log.warning("Ignoring unreadable state file %s: %s", self.state_path, exc)
        return size if self.tail else 0

    def _save_state(self, offset: int):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"inode": self._inode, "offset": offset, "saved_at": time.time()}))
        os.replace(tmp, self.state_path)
        self._committed = offset

    # ---- batching -------------------------------------------------------
    def _dead_letter(self, entries):
        if not entries:
            return
        with self.dead_letter_path.open("a", encoding="utf-8") as fh:
            for raw, reason in entries:
                fh.write(json.dumps({"reason": reason, "raw": raw.decode("utf-8", "replace").strip()}) + "\n")

    def _handle_line(self, raw: bytes, end_offset: int):
        self.stats["read"] += 1
        self._batch_end = end_offset
        if not raw.strip():
            return
        try:
            event = validate_event(json.loads(raw))
        except (ValueError, UnicodeDecodeError) as exc:
            log.warning("Invalid event skipped (saved to %s): %s", self.dead_letter_path.name, exc)
            self._bad.append((raw, str(exc)))
            self.stats["invalid"] += 1
            return
        self._docs.append((make_doc_id(event, raw), event))

    def flush(self) -> bool:
        """Send the current batch. Returns True on success, False if delivery failed (nothing committed)."""
        if not self._docs and not self._bad and self._batch_end == self._committed:
            return True
        try:
            if self._docs:
                indexed, rejected = send_bulk(self.client, self.index, self._docs, self.max_retries,
                                              sleep=self._sleep, should_abort=lambda: self.stop_requested)
                self.stats["indexed"] += indexed
                self.stats["batches"] += 1
                if rejected:
                    self.stats["rejected"] += len(rejected)
                    for doc_id, _src, reason in rejected:
                        log.error("Elasticsearch rejected event %s: %s", doc_id, reason)
                    self._dead_letter([(json.dumps(src).encode(), f"rejected by Elasticsearch: {reason}")
                                       for _id, src, reason in rejected])
                log.info("Indexed %d event(s) into '%s'%s", indexed, self.index,
                         f" ({len(rejected)} rejected)" if rejected else "")
            self._dead_letter(self._bad)
        except ShipFailure as exc:
            self.stats["failed_flushes"] += 1
            log.error("Could not deliver batch of %d event(s): %s. Position NOT advanced - "
                      "events stay in %s and will be re-sent.", len(self._docs), exc, self.path)
            return False
        self._save_state(self._batch_end)
        self._docs, self._bad = [], []
        self._last_flush = time.monotonic()
        return True

    def _discard_batch_and_rewind(self, fh):
        self._docs, self._bad = [], []
        self._batch_end = self._committed
        fh.seek(self._committed)

    def _interruptible_sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while not self.stop_requested and time.monotonic() < end:
            time.sleep(min(0.1, seconds))

    # ---- main loop ------------------------------------------------------
    def run(self) -> int:
        """Returns a process exit code (0 = clean, 1 = events could not be delivered)."""
        while not self.path.exists():
            if not self.follow:
                log.error("Log file %s does not exist", self.path)
                return 1
            log.info("Waiting for %s to appear ...", self.path)
            self._interruptible_sleep(1)
            if self.stop_requested:
                return 0

        exit_code = 0
        fh = self.path.open("rb")
        try:
            st = os.fstat(fh.fileno())
            self._inode = st.st_ino
            self._committed = self._batch_end = self._load_state(st.st_size, st.st_ino)
            fh.seek(self._committed)
            log.info("Following %s from byte %d -> index '%s'", self.path, self._committed, self.index)

            while not self.stop_requested:
                line = fh.readline()
                if line.endswith(b"\n"):
                    self._handle_line(line, fh.tell())
                    full = len(self._docs) + len(self._bad) >= self.batch_size
                    if full and not self.flush():
                        if not self.follow:
                            return 1
                        self._discard_batch_and_rewind(fh)
                        self._interruptible_sleep(self.failure_cooldown)
                    continue

                if line:  # partial line still being written: wait for the rest
                    fh.seek(-len(line), os.SEEK_CUR)

                has_pending = bool(self._docs or self._bad or self._batch_end != self._committed)
                if has_pending and (not self.follow or time.monotonic() - self._last_flush >= self.flush_seconds):
                    if not self.flush():
                        if not self.follow:
                            return 1
                        self._discard_batch_and_rewind(fh)
                        self._interruptible_sleep(self.failure_cooldown)
                        continue
                if not self.follow:
                    break

                # detect log rotation / truncation
                try:
                    cur = os.stat(self.path)
                    if cur.st_ino != self._inode or cur.st_size < fh.tell():
                        log.info("Log file rotated or truncated - reopening")
                        if not self.flush():
                            self._discard_batch_and_rewind(fh)
                        fh.close()
                        fh = self.path.open("rb")
                        self._inode = os.fstat(fh.fileno()).st_ino
                        self._committed = self._batch_end = 0
                        self._save_state(0)
                        continue
                except FileNotFoundError:
                    pass
                self._interruptible_sleep(self.poll_seconds)

            # ---- clean shutdown: flush what we have ----
            if self.stop_requested:
                log.info("Shutdown requested - flushing pending events ...")
            if not self.flush():
                exit_code = 1
                log.error("Shutdown with undelivered events; they remain in %s and will be sent on next start.",
                          self.path)
        finally:
            fh.close()
        log.info("Finished. stats=%s", self.stats)
        return exit_code

    def request_stop(self, *_):
        self.stop_requested = True


# --------------------------------------------------------------------------- CLI
def wait_for_index(client, index: str, attempts: int = 10) -> bool:
    for i in range(1, attempts + 1):
        try:
            return bool(client.indices.exists(index=index))
        except Exception as exc:  # noqa: BLE001 - startup probe, report and retry
            log.warning("Elasticsearch not ready (%d/%d): %s", i, attempts, exc)
            time.sleep(min(2 * i, 10))
    raise SystemExit("Elasticsearch is unreachable. Check that the containers are healthy: docker compose ps")


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Ship JSONL logs to Elasticsearch")
    p.add_argument("--file", default=config.LOG_FILE, help="JSONL file to read (default: %(default)s)")
    p.add_argument("--es-url", default=config.ES_URL, help="Elasticsearch URL (default: %(default)s)")
    p.add_argument("--index", default=config.ES_INDEX, help="Target index (default: %(default)s)")
    p.add_argument("--follow", action="store_true", help="Keep running and ship new lines as they are written")
    p.add_argument("--tail", action="store_true", help="On first run, skip existing content and ship only new lines")
    p.add_argument("--batch-size", type=int, default=config.SHIP_BATCH_SIZE)
    p.add_argument("--flush-seconds", type=float, default=config.SHIP_FLUSH_SECONDS)
    p.add_argument("--max-retries", type=int, default=config.SHIP_MAX_RETRIES)
    p.add_argument("--state-file", default=None, help="Where to store the read position (default: next to log file)")
    p.add_argument("--dead-letter", default=None, help="Where to store rejected events (default: next to log file)")
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("elastic_transport").setLevel(logging.WARNING)

    client = config.make_client(args.es_url)
    if not wait_for_index(client, args.index):
        log.error("Index '%s' does not exist. Create it first: python scripts/setup-index.py", args.index)
        return 2

    shipper = Shipper(client, args.index, args.file, args.state_file, args.dead_letter, args.batch_size,
                      args.flush_seconds, args.max_retries, args.follow, args.tail)
    signal.signal(signal.SIGINT, shipper.request_stop)
    signal.signal(signal.SIGTERM, shipper.request_stop)
    return shipper.run()


if __name__ == "__main__":
    sys.exit(main())
