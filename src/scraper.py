"""
Tiki product scraper (Project 02)

Fetches ~200,000 Tiki products through the API, keeps 6 fields, cleans the
"description", saves 1,000 products per JSON file, and records every failed
ID with its reason.

How to run (from the project root, with the venv active):
    python -u src/scraper.py          # full run
    python -u src/scraper.py 3000     # test run: only the first 3,000 IDs

The script runs these steps in order:
    [1] Read and check the ID file
    [2] Call the API once (pre-flight check)
    [3] Fetch all products in batches of 1,000, with a checkpoint
    [4] Check the result (reconciliation)
"""

import csv
import html
import json
import logging
import os
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm
from urllib3.util.retry import Retry

# ============================================================
# SETTINGS
# ============================================================
VERSION = "v4.1"

# The script lives in src/, so the project root is two levels up.
# Paths are built from the script's location, so they work from any folder.
BASE_DIR = Path(__file__).resolve().parent.parent
ID_FILE = BASE_DIR / "data" / "ids.csv"
OUTPUT_DIR = BASE_DIR / "output"
ERROR_DIR = BASE_DIR / "errors"
LOG_DIR = BASE_DIR / "logs"

API_URL = "https://api.tiki.vn/product-detail/api/v1/products/{}"
USER_AGENT = "Mozilla/5.0"

BATCH_SIZE = 1000      # products per file. NEVER change between runs: the checkpoint uses batch numbers
MAX_WORKERS = 1        # parallel requests. 2 workers got blocked (~1.4 req/s), so 1
TIMEOUT = 10           # max seconds to wait for one response
INTERVAL = 1.0         # seconds between the START of two requests (1.0 = 1 request/second)
                       # measured safe: 1.0 and 0.85. Blocked: ~1.4 req/s and faster

MAX_NETWORK_ERRORS = 20  # this many network errors in a row = "the connection is down"
CHECK_EVERY = 120        # while blocked or offline: try one request every 2 minutes
MAX_WAIT = 3600          # give up after waiting 1 hour

# Optional test limit from the command line: `python src/scraper.py 3000`.
# No number = full run, so nobody has to edit the code before running it.
LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else None

logger = logging.getLogger("tiki")


# ============================================================
# Logging: to the screen and to logs/run.log
# ============================================================
def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    # Full date in each line: a run lasts several days, so "17:03" alone is ambiguous
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    for handler in (logging.FileHandler(LOG_DIR / "run.log", encoding="utf-8"),
                    logging.StreamHandler()):
        handler.setFormatter(fmt)
        logger.addHandler(handler)


# ============================================================
# [1] Read and check the ID file
# ============================================================
def load_ids(path):
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Put the ID file in data/")

    raw, invalid = [], []
    # utf-8-sig: drops the invisible BOM character if the file was saved from Excel
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            value = line.strip().split(",")[0].strip()   # strip() also removes Windows \r
            if value.isdigit():
                raw.append(value)
            elif value:
                invalid.append(value)                    # e.g. the header line "id"

    ids = list(dict.fromkeys(raw))                       # remove duplicates, keep the order
    logger.info("[1] ID file: %d valid lines | %d duplicates | %d invalid %s",
                len(raw), len(raw) - len(ids), len(invalid), invalid[:3])
    logger.info("[1] => %d unique IDs", len(ids))
    return ids


# ============================================================
# Clean the data
# ============================================================
def clean_description(raw):
    """HTML -> text: remove tags, decode &nbsp; &amp; ..., collapse whitespace."""
    if not raw:
        return ""
    text = BeautifulSoup(raw, "html.parser").get_text(separator=" ")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()   # \s also matches \xa0 (&nbsp;) and line breaks


def extract(p):
    """Keep only the 6 fields the project needs."""
    return {
        "id": p.get("id"),
        "name": p.get("name"),
        "url_key": p.get("url_key"),
        "price": p.get("price"),
        "description": clean_description(p.get("description")),
        "images": [img.get("base_url") for img in (p.get("images") or [])],
    }


# ============================================================
# Call the API
# ============================================================
_local = threading.local()


def get_session():
    """One Session per thread: reuses the connection and retries temporary errors."""
    if not hasattr(_local, "session"):
        session = requests.Session()
        session.headers["User-Agent"] = USER_AGENT
        retry = Retry(
            total=3,
            backoff_factor=1,                         # wait longer between each retry
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False,                    # after the last retry, return the response so we record the code
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        _local.session = session
    return _local.session


# Pacing: one request START every INTERVAL seconds.
# v4 slept 1 s BEFORE each request, so the real gap was 1 s + Tiki's answer time (~1.4 s).
# Here we remember when the next request may start, and sleep only the time that is left.
_pace_lock = threading.Lock()
_next_start = 0.0


def wait_turn():
    global _next_start
    with _pace_lock:                                  # threads take turns, so the rate holds with any MAX_WORKERS
        now = time.monotonic()
        start = max(now, _next_start)
        _next_start = start + INTERVAL
    time.sleep(start - now)


# Shared stop flag for all threads: "stop sending requests in this batch".
# It is set for two reasons: Tiki blocks us, or the network is down.
STOP = threading.Event()
stop_reason = ""
_count_lock = threading.Lock()
network_errors_in_a_row = 0


def stop(reason):
    global stop_reason
    if not STOP.is_set():
        stop_reason = reason
        STOP.set()


def fetch_product(pid):
    """Return (pid, data, None, "") on success,
    or (pid, None, error reason, detail) on error."""
    global network_errors_in_a_row
    if STOP.is_set():
        # Already blocked or offline: DON'T send more requests (they would fail too,
        # and more requests make a block last longer). This batch will be redone.
        return pid, None, "skipped", stop_reason

    wait_turn()
    try:
        r = get_session().get(API_URL.format(pid), timeout=TIMEOUT)
    except requests.RequestException as e:
        # Circuit breaker: one network error can be bad luck, 20 in a row means the
        # connection is down. Then we stop, instead of recording thousands of
        # ConnectionErrors (that is what cost ~40 hours in the first run).
        with _count_lock:
            network_errors_in_a_row += 1
            if network_errors_in_a_row >= MAX_NETWORK_ERRORS:
                stop("network down")
        return pid, None, type(e).__name__, str(e)[:100]    # Timeout, ConnectionError, ...

    with _count_lock:
        network_errors_in_a_row = 0                         # an answer arrived: the network works
    if r.status_code != 200:
        return pid, None, f"HTTP {r.status_code}", ""
    try:
        return pid, extract(r.json()), None, ""
    except ValueError:
        # HTTP 200 but the body is not JSON = Tiki's anti-bot page. Tell the other threads to stop.
        stop("blocked")
        return pid, None, "blocked", f"{r.headers.get('Content-Type', '?')} | {r.text[:80]!r}"


def api_available(probe_id):
    """Send one test request: True if the API answers normal JSON."""
    try:
        r = get_session().get(API_URL.format(probe_id), timeout=TIMEOUT)
    except requests.RequestException:
        return False
    return r.status_code == 200 and "json" in r.headers.get("Content-Type", "")


def wait_until_available(probe_id):
    """Wait until the block or the network problem is over. False after MAX_WAIT."""
    waited = 0
    while waited < MAX_WAIT:
        logger.warning("[3] %s: waiting %ds, then trying again (waited %d min)",
                       stop_reason, CHECK_EVERY, waited // 60)
        time.sleep(CHECK_EVERY)
        waited += CHECK_EVERY
        if api_available(probe_id):
            logger.info("[3] API available again after ~%d min, continuing", waited // 60)
            return True
    return False


# ============================================================
# [2] Pre-flight: call the API once before the real run
# ============================================================
def explore_one(pid):
    """Return False if the API can't be used (no network, blocked, error).
    Stopping here avoids filling errors/ with thousands of rows for nothing."""
    try:
        r = get_session().get(API_URL.format(pid), timeout=TIMEOUT)
    except requests.RequestException as e:
        # v4 had no try here, so "no network" crashed with a long traceback
        logger.error("[2] No connection (%s). Check the network and run again.", type(e).__name__)
        return False

    content_type = r.headers.get("Content-Type", "?")
    logger.info("[2] Test call ID %s -> HTTP %s | %s", pid, r.status_code, content_type)
    if r.status_code != 200:
        logger.error("[2] The first product returned HTTP %s. Check the network and run again", r.status_code)
        return False
    try:
        p = r.json()
    except ValueError:
        logger.error("[2] The API returned %s instead of JSON -> we are blocked (anti-bot). "
                     "Wait until curl returns application/json, then run again.", content_type)
        return False
    sample = extract(p)
    logger.info("[2] After extract: %s | %s | price %s",
                sample["id"], sample["name"], sample["price"])
    return True


# ============================================================
# [3] Fetch the data in batches, with a checkpoint
# ============================================================
def save_batch(batch_no, products, errors):
    # One error file per batch -> redoing a batch never writes duplicate rows
    with open(ERROR_DIR / f"errors_{batch_no:04d}.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["id", "reason", "detail"])
        writer.writerows(errors)

    # Write to a temp file, then rename: a half-written JSON file can never exist.
    # The product file is written LAST, because its existence means "batch done".
    out = OUTPUT_DIR / f"products_{batch_no:04d}.json"
    tmp = out.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(products, f, ensure_ascii=False, indent=2)
    os.replace(tmp, out)


def run_batches(ids, probe_id):
    """Return True if every batch was saved, False if the run stopped early."""
    global network_errors_in_a_row
    batches = [ids[i:i + BATCH_SIZE] for i in range(0, len(ids), BATCH_SIZE)]
    total_ok = total_err = skipped = stops = 0

    # loggers=[logger]: only our "tiki" logger goes through tqdm, so lines aren't printed twice
    with ThreadPoolExecutor(MAX_WORKERS) as ex, logging_redirect_tqdm(loggers=[logger]):
        for batch_no, batch in enumerate(tqdm(batches, desc="Batches", unit="batch"), start=1):
            if (OUTPUT_DIR / f"products_{batch_no:04d}.json").exists():
                skipped += 1                          # checkpoint: finished in an earlier run
                continue

            start = time.time()
            while True:
                STOP.clear()
                network_errors_in_a_row = 0
                try:
                    results = list(ex.map(fetch_product, batch))
                except KeyboardInterrupt:
                    # Ctrl-c: without this, the pool would still send every queued request
                    # of the batch before exiting. STOP makes them return at once.
                    stop("interrupted")
                    raise
                if not STOP.is_set():
                    break                             # the whole batch ran without a stop
                # Stopped in the middle: results are incomplete, so DON'T save.
                # Wait until the API answers again, then redo the whole batch.
                stops += 1
                done = sum(1 for _, data, _, _ in results if data)
                logger.warning("[3] batch %04d stopped (%s) after %d products", batch_no, stop_reason, done)
                if not wait_until_available(probe_id):
                    logger.error("[3] API unavailable for over %d min. STOPPING. Run again later: "
                                 "the script continues from batch %04d.", MAX_WAIT // 60, batch_no)
                    return False

            products = [data for _, data, err, _ in results if data]
            errors = [(pid, err, detail) for pid, _, err, detail in results if err]
            save_batch(batch_no, products, errors)

            total_ok += len(products)
            total_err += len(errors)
            logger.info("[3] batch %04d: ok=%d errors=%d (%.0fs)",
                        batch_no, len(products), len(errors), time.time() - start)

    logger.info("[3] This run: ok=%d errors=%d | %d batches already done | stopped %d times",
                total_ok, total_err, skipped, stops)
    return True


# ============================================================
# [4] Check the result (reconciliation)
# ============================================================
def validate(ids):
    """products + errors must equal the number of IDs, with no duplicates.
    Only this run's batch numbers are read: v4 read every file in output/,
    so leftover files from other runs could make the check pass wrongly."""
    n_batches = (len(ids) + BATCH_SIZE - 1) // BATCH_SIZE      # round up
    product_ids, errors, missing = [], [], []
    for batch_no in range(1, n_batches + 1):
        p_file = OUTPUT_DIR / f"products_{batch_no:04d}.json"
        e_file = ERROR_DIR / f"errors_{batch_no:04d}.csv"
        if not (p_file.exists() and e_file.exists()):
            missing.append(batch_no)
            continue
        with open(p_file, encoding="utf-8") as f:
            product_ids += [str(p["id"]) for p in json.load(f)]
        with open(e_file, encoding="utf-8") as f:
            errors += list(csv.DictReader(f))

    ok, err = len(product_ids), len(errors)
    dup = ok - len(set(product_ids))
    match = not missing and dup == 0 and ok + err == len(ids)

    logger.info("[4] batches: %d/%d | products ok: %d | errors: %d | duplicate ids: %d",
                n_batches - len(missing), n_batches, ok, err, dup)
    logger.info("[4] ok + errors = %d | IDs = %d -> %s", ok + err, len(ids), "MATCH" if match else "MISMATCH")
    if missing:
        logger.warning("[4] missing batches: %s", missing[:10])
    for reason, count in Counter(e["reason"] for e in errors).most_common():
        logger.info("[4]   %-20s %d", reason, count)
    return match


# ============================================================
def main():
    setup_logging()
    logger.info("scraper.py %s | workers=%d interval=%.2fs (max %.2f req/s) | limit=%s",
                VERSION, MAX_WORKERS, INTERVAL, 1 / INTERVAL, LIMIT)
    OUTPUT_DIR.mkdir(exist_ok=True)
    ERROR_DIR.mkdir(exist_ok=True)

    ids = load_ids(ID_FILE)
    if LIMIT:
        if LIMIT % BATCH_SIZE:
            # Otherwise the last test batch is saved incomplete, and the full run skips it
            logger.error("The limit must be a multiple of %d", BATCH_SIZE)
            return 1
        ids = ids[:LIMIT]
        logger.info("Test mode: only the first %d IDs", len(ids))

    if not explore_one(ids[0]):
        logger.error("Stopped before fetching, so errors/ is not filled with failures.")
        return 1

    start = time.time()
    try:
        finished = run_batches(ids, probe_id=ids[0])     # ids[0] just returned JSON in step [2]
    except KeyboardInterrupt:
        logger.warning("Stopped with Ctrl-c. Finished batches are saved: run again to continue.")
        return 130
    logger.info("Total time: %.1f min", (time.time() - start) / 60)
    if not finished:
        return 1

    return 0 if validate(ids) else 2


if __name__ == "__main__":
    # The exit code tells the terminal (or a scheduler) how the run ended:
    # 0 = done and reconciled, 1 = stopped early, 2 = done but numbers don't match,
    # 130 = stopped with Ctrl-c.
    # Check it after a run with: echo $?
    sys.exit(main())
