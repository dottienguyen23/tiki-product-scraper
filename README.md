# Tiki Product Scraper

A production-ready Python pipeline that fetches about **200,000 products** from the Tiki product API, keeps 6 fields, cleans the HTML description, and saves them as JSON files of 1,000 products each, with every failed ID recorded with its reason.

It is built to run unattended for 2–3 days on one machine: it survives crashes and restarts, stops itself when the API blocks it or the network dies, and checks at the end that no product was lost.

## Documentation

| Document | Read it to... |
|---|---|
| **This README** | see what the pipeline does, why it is production-ready, and the results |
| [Runbook](docs/RUNBOOK.md) | run it: setup, test run, full run, monitoring, retry |
| [Step-by-step guide](docs/GUIDE.md) | follow how it was built, from one `curl` call to the final script |
| [`src/scraper.py`](src/scraper.py) | read the code: one file, each step commented |

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# put the ID list at data/ids.csv (one ID per line, header "id")

python -u src/scraper.py 3000      # test run: the first 3,000 IDs (3 batches)
python -u src/scraper.py           # full run (~2–3 days): start it inside tmux
echo $?                            # 0 = finished and every ID accounted for
```
Monitoring, settings and the retry pass are in the [Runbook](docs/RUNBOOK.md).

## How it works

```
data/ids.csv ──► [1] read, validate, deduplicate IDs
                        │
                 [2] pre-flight: one test call. Blocked or offline? Stop before writing anything
                        │
                 [3] batches of 1,000 ── batch file exists? skip it (checkpoint)
                        │
                 for each ID: API call ──► keep 6 fields ──► clean description
                        │
     output/products_0001.json             errors/errors_0001.csv
     (written last: marks the batch done)  (id, reason, detail)
                        │
                 [4] reconciliation: products + errors = input IDs?  ──► exit code
```

## What makes it production-ready

Each feature exists because of a real problem met while building or running the pipeline.

| Feature | What it does | The problem it solves |
|---|---|---|
| **No code edits to run it** | Full run by default; the test limit is a command-line argument | An earlier version still had `LIMIT = 3000` in the code, so a "full run" would have fetched only 3,000 IDs |
| **Input validation** | Skips the header and invalid lines, removes duplicates, and logs how many of each | Bad input becomes visible instead of silently dropped |
| **Pre-flight check** | One test call before the run; stops with a clear message if blocked or offline | Without it, a blocked start fills `errors/` with thousands of false failures |
| **Rate limiting** | One request start every `INTERVAL` seconds (default 1.0 s), shared by all threads | Tiki blocks above ~1.4 req/s. Pacing by start time is exact and ~30% faster than "sleep, then request" |
| **Automatic retries** | `HTTP 429` / `5xx` and network errors are retried 3 times with growing waits | Short hiccups don't become failed IDs |
| **Block detection** | `HTTP 200` with HTML instead of JSON = Tiki's anti-bot page: all threads stop, the scraper waits, then redoes the batch | A block arrives as `200 OK`; checking the status code alone would record thousands of false errors |
| **Circuit breaker** | 20 network errors in a row = connection down: the batch stops and is redone when the network is back | In the first run, a Wi-Fi outage turned ~12,000 IDs into `ConnectionError` over ~40 hours |
| **Errors never stop the run** | Each failed ID becomes a row in `errors/` with its reason | One bad product never crashes 200,000 |
| **Checkpoint / resume** | Each finished batch is a file; on restart, finished batches are skipped | A 3-day job will be interrupted. A Windows restart cost one batch, not the whole run |
| **Atomic writes** | Each JSON file is written to `.tmp`, then renamed; the product file is written last | A crash never leaves a half-written file that looks finished |
| **Idempotent** | Running it again gives the same result: one error file per batch, no duplicate rows | Safe to rerun at any time |
| **Logging** | Screen and `logs/run.log`, with the full date on every line | A multi-day run needs a log on disk to diagnose problems afterwards |
| **Reconciliation** | At the end: products + errors must equal the input IDs, with no duplicates and no missing batches | "The script finished" is not the same as "the data is complete" |
| **Exit codes** | `0` OK · `1` stopped early · `2` numbers don't match · `130` Ctrl-c | A scheduler or another script can tell success from failure without reading the log |

**Known limits.** It runs on one machine from one IP address, so Tiki's rate limit sets the minimum runtime (~47 h at the fastest safe pace). There is no alerting: problems show up in the log, which has to be checked (see [Runbook, step 6](docs/RUNBOOK.md#6-monitor-at-least-twice-a-day)). The circuit breaker needs about 15 minutes to trip, because each request is retried first.

## Requirements (DoD)

- [x] Fetch product details for every ID in the input list
- [x] Keep `id`, `name`, `url_key`, `price`, `description`, `images`
- [x] Clean `description` (HTML → plain text)
- [x] Save about 1,000 products per `.json` file
- [x] Record failed IDs with the reason
- [x] Find a way to shorten the fetch time: see [Shortening the run time](#shortening-the-run-time)
- [x] Full run of all ~200,000 IDs: 200/200 batches (first pass); retry pass for temporary errors _in progress_

## Results

The first pass ran with earlier versions of the scraper. The problems it hit are what the production features above fix.

| Metric | Value |
|---|---|
| Unique input IDs | 200,000 |
| Products saved, first pass | **114,108 (57.1%)** |
| Failed IDs, first pass | **85,892 (42.9%)**: `HTTP 404` 73,965 (37.0%) · `ConnectionError` 11,915 (6.0%) · `ReadTimeout` 12 |
| Products + failed = input IDs | **MATCH ✅** (0 missing) |
| Duplicate product rows | **0** |
| Sent to the retry pass | 11,927 IDs (every error except `HTTP 404`) |
| Products saved after the retry pass | _[fill in after the retry pass]_ |
| Final failed IDs | _[fill in]_, mostly `HTTP 404` (products removed from Tiki) |
| Time per healthy batch of 1,000 (first pass) | about 23 min (≈ 1.4 s per ID) |
| Total wall-clock time (first pass) | about 6 days, see [Run timeline](#run-timeline-1521-oct-2026) |

The 37% `HTTP 404` rate comes from the data, not the code: those products were removed from Tiki after the ID list was created, and they are correctly never retried.

## Run timeline (15–21 Oct 2026)

Measured from the time each output file was saved.

| When | What happened | Comment |
|---|---|---|
| 15 Oct 08:57 → 12:38 | Test run (3,000 IDs). Batch 1: 23 min, batch 2: **3h18m** | Blocked in batch 2; the scraper waited, then redid the whole batch |
| 15 Oct 12:38 → 16 Oct 07:27 | Batches 3 and 4 | Mostly idle time between test sessions, not slow scraping |
| 16 Oct 07:27 → 17 Oct 08:28 | Batches 5–61 at a steady pace | ✅ Healthy: about 23 min per batch |
| **17 Oct 08:28 → 18 Oct ~23:45** | Batches 62–73 took **exactly 3h03m each** | ❌ 3h03m ≈ 1,000 × (10 s timeout + 1 s delay): nearly every request timed out |
| 18 Oct ~23:45 | Windows restarted to fix the Wi-Fi | The batch in progress (74) was lost |
| 18 Oct 23:51 → 19 Oct 00:15 | Scraper restarted in `tmux`, batch 74 finished | ✅ The checkpoint skipped batches 1–73 |
| 19 Oct → 21 Oct morning | Batches 75–200, two interruptions of ~1h40m | ✅ Run complete: 200/200 batches |

**Key finding:** the connection was unstable or down for about 1.5 days, and the scraper kept going, spending about 40 hours on requests that could not succeed. That is why the production version has a **circuit breaker**.

## Shortening the run time

| Approach | Result |
|---|---|
| `requests.Session()` | Reuses the connection: no new TCP/TLS handshake for every request |
| Measuring the safe rate | 1 s pause (~0.75 req/s): no block. 0.2 s pause (~1.9 req/s): **blocked after 11 requests** |
| Parallel requests (`ThreadPoolExecutor`) | 1 worker: no block. 2 workers (1.43 req/s): **blocked**. Tiki's limit is below ~1.4 req/s, so more workers cannot help → 1 worker |
| Pacing by request start | Sleeping 1 s *after* each answer wasted Tiki's ~0.4 s response time. Starting a request every 1.0 s: no block, 1.00 req/s. Every 0.85 s: no block, **1.17 req/s** |
| User-Agent check | `curl`'s default User-Agent also got JSON: the block is about **rate**, not the header |
| Never retrying `HTTP 404` | 37% of IDs are products that no longer exist; each costs one request, not several |
| Circuit breaker | Stops on a dead network instead of spending hours on requests that can't succeed |

**Conclusion:** the code was not the bottleneck. Tiki's rate limit sets the minimum: 200,000 IDs take about **47 h at 0.85 s** instead of ~74 h with the first pacing. The biggest avoidable loss (about 40 h) came from running blind on a dead connection, so the most effective speed-up was to **detect failure early**, not to send requests faster.

## Project structure

```
tiki-product-scraper/
├── README.md            # start here
├── docs/
│   ├── RUNBOOK.md       # how to run it, every command in order
│   └── GUIDE.md         # how it was built, level by level
├── src/
│   └── scraper.py       # the pipeline: one file, steps [1]–[4]
├── requirements.txt
└── .gitignore
```

Not in Git (see `.gitignore`): `data/` (input IDs), `output/`, `errors/`, `logs/`, `retry/`, `.venv/`.

## Lessons learned

- **Estimate the runtime first.** At 1.4 s per ID, 200,000 IDs take more than 3 days. That decided the design: batches, checkpoints and `tmux`.
- **A 200 status can still be a failure.** Anti-bot pages arrive as `200 OK` with HTML. Checking `Content-Type` catches them.
- **Permanent and temporary errors are different.** `HTTP 404` is never retried; timeouts, `429` and `5xx` are.
- **Handling each error is not enough; watch the error rate.** Every timeout was recorded correctly, yet 1.5 days of them went unnoticed.
- **"Finished" is not "complete".** Products + errors must equal the input IDs.
- **Production-ready means safe for someone else to run.** No code edits to start it, failures that stop loudly, and a result that can be checked automatically.
