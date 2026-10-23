# Tiki Product Scraper

Fetch about **200,000 products** from the Tiki product API with Python, keep the fields that matter, clean them, and save them as JSON files of 1,000 products each, with every failed ID recorded with its reason.

## Documentation

| Document | Read it to... |
|---|---|
| **This README** | see the problem, the results and what was learned |
| [Runbook](docs/RUNBOOK.md) | run the scraper: setup, test run, full run, monitoring, retry |
| [Step-by-step guide](docs/GUIDE.md) | follow how the solution was built, level by level, from one `curl` call to the production script |
| [`src/scraper.py`](src/scraper.py) | read the code (run `python src/scraper.py --help` for all options) |

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# put the ID list at data/ids.csv (one ID per line, header "id")

python -u src/scraper.py --limit 3000      # test run: 3 batches
python -u src/scraper.py --interval 0.85   # full run (~2 days): start it inside tmux
```
Details, monitoring and the retry pass are in the [Runbook](docs/RUNBOOK.md).

## Requirements (DoD)

- [x] Fetch product details for every ID in the input list
- [x] Keep `id`, `name`, `url_key`, `price`, `description`, `images`
- [x] Clean `description` (HTML → plain text)
- [x] Save about 1,000 products per `.json` file
- [x] Record failed IDs with the reason
- [x] Find a way to shorten the fetch time: see [Shortening the run time](#shortening-the-run-time)
- [x] Full run of all ~200,000 IDs: 200/200 batches (first pass); retry pass for temporary errors _in progress_

## Results

| Metric | Value |
|---|---|
| Unique input IDs | 200,000 |
| Products saved, first pass | **114,108 (57.1%)** |
| Failed IDs, first pass | **85,892 (42.9%)**: `HTTP 404` 73,965 (37.0%) · `ConnectionError` 11,915 (6.0%) · `ReadTimeout` 12 |
| Products + failed = input IDs | **MATCH ✅** (0 missing) |
| Duplicate product rows | **0** |
| Sent to the retry pass | 11,927 IDs (every error except `HTTP 404`) |
| Products saved after the retry pass | _[fill in after the retry pass]_ |
| Recovered in the retry pass | _[fill in]_ |
| Final failed IDs | _[fill in]_, mostly `HTTP 404` (products removed from Tiki) |
| Time per healthy batch of 1,000 (first pass) | about 23 min (≈ 1.4 s per ID) |
| Total wall-clock time (first pass) | about 6 days, see [Run timeline](#run-timeline-1521-oct-2026) |

## How it works

```
data/ids.csv ──► read & deduplicate IDs ──► batches of 1,000
                                              │
                  for each ID: API call ──► extract 6 fields ──► clean description
                                              │
           output/products_0001.json      errors/errors_0001.csv
           (skipped next run = checkpoint)  (id, reason, detail)
```

- **Rate limit:** one request start every `--interval` seconds (default 1.0, fastest safe 0.85). Faster runs were blocked by Tiki's anti-bot protection.
- **Errors never stop the run:** network errors, HTTP errors and non-JSON responses become a row in `errors/` with the reason.
- **Block detection:** an HTML response instead of JSON means "blocked". The scraper stops sending, waits until the API answers again, and redoes the whole batch.
- **Circuit breaker:** 20 network errors in a row mean the connection is down. The batch is halted and redone later instead of being saved as thousands of `ConnectionError` rows.
- **Resumable:** each finished batch is a file. After a crash or restart, finished batches are skipped.
- **Atomic writes:** each JSON file is written to `.tmp` and then renamed, so a crash never leaves a half-written file.
- **Reconciliation:** at the end, products + errors must equal the input IDs. The result sets the exit code (`0` OK, `1` stopped early, `2` mismatch).

## Run timeline (15–21 Oct 2026)

The first pass ran with earlier versions of the scraper (the simple version and v4). Measured from the time each output file was saved.

| When | What happened | Comment |
|---|---|---|
| 15 Oct 08:57 → 12:38 | Test run with v4 (3,000 IDs). Batch 1: 23 min, batch 2: **3h18m** | Blocked in batch 2; v4 waits, then redoes the whole batch |
| 15 Oct 12:38 → 16 Oct 07:27 | Batches 3 and 4 | Mostly idle time between test sessions, not slow scraping |
| 16 Oct 07:27 → 17 Oct 08:28 | Batches 5–61 at a steady pace | ✅ Healthy: about 23 min per batch (≈ 1.4 s per ID) |
| **17 Oct 08:28 → 18 Oct ~23:45** | Batches 62–73 took **exactly 3h03m each** | ❌ 3h03m ≈ 1,000 × (10 s timeout + 1 s delay): nearly every request timed out |
| 18 Oct ~23:45 | Windows restarted to fix the Wi-Fi | The batch in progress (74) was lost |
| 18 Oct 23:51 → 19 Oct 00:15 | Scraper restarted in `tmux`, batch 74 finished | ✅ The checkpoint skipped batches 1–73 |
| 19 Oct → 21 Oct morning | Batches 75–200 at a steady pace, two interruptions of ~1h40m (before 157 and 187) | ✅ Run complete: 200/200 batches |

**Key finding:** the timeout period lines up with the Wi-Fi problem. The connection was most likely unstable or down for about 1.5 days, and the simple version kept going, spending about 40 hours on requests that could not succeed and turning **11,915 IDs into `ConnectionError`** (no network at all, rather than a slow server). Those IDs go to the retry pass. The circuit breaker in `src/scraper.py` (v5) exists because of this.

| Time | Value |
|---|---|
| Wall-clock time of the full run | about 6 days |
| Time counted as pauses | 60h55m (43%), including idle time between the test and the main run |
| Time lost to the dead connection (batches 62–74) | about 40 h |
| Expected time with no incidents (200 × 23 min) | about 77 h (3.2 days) |
| Minimum possible at 1 s per ID | 55.5 h |

## Shortening the run time

| Approach | Result |
|---|---|
| `requests.Session()` (reuse the connection) | No new TCP/TLS handshake for every request |
| Measuring the safe rate (one request at a time) | **1 s delay (~0.75 req/s real):** 150 requests in 200 s, **no block**, 93 ok / 57 `HTTP 404` |
| | **0.2 s delay (~1.9 req/s real):** **blocked after only 11 requests (6 s)** |
| | Testing again straight after: blocked at request 1. The block was still active, so a rest between tests is needed. A `curl` check a few minutes later returned JSON again |
| Parallel requests with `ThreadPoolExecutor` | **1 worker (0.64 req/s):** 100 requests, no block, 55 ok / 45 `HTTP 404`. **2 workers (1.43 req/s):** **blocked**: 62 of 100 requests got the block page. Tiki's limit is **below ~1.4 req/s**, so more workers cannot speed this up → 1 worker |
| **Fixed interval between request starts** | The old pacing slept 1 s **after** each answer, wasting the ~0.4 s response time. Starting a request every **1.0 s**: 300 requests, **no block**, exactly 1.00 req/s (Tiki answered in 0.42 s on average). Every **0.85 s**: 300 requests, **no block**, **1.17 req/s**. Built into v5 as `--interval` |
| User-Agent check | `curl` with its default `curl/8.5.0` User-Agent also got JSON: the block is about **request rate**, not about the header |
| Never retrying `HTTP 404` | 37% of IDs (73,965) are products that no longer exist; each costs one request, not several |
| **Circuit breaker** | After 20 network errors in a row: stop, wait until the API answers, redo the batch. Would have saved most of the ~40 h. Built into v5 |
| Avoiding pauses | `tmux`, Windows sleep set to *Never*, fixing Wi-Fi without restarting |

**Conclusion:** the code was not the bottleneck. Tiki accepts at least 1.17 requests per second and blocks at 1.43, so the rate limit sets the minimum. With fixed-interval pacing at 0.85 s, 200,000 IDs take about **47 h (~2 days)** instead of ~74 h, and the biggest avoidable loss (about 40 h) came from running blind on a dead connection. The most effective way to shorten the run is to **detect failure early**, not to send requests faster.

## Mistakes and improvements

| # | Mistake | Effect | Improvement (in v5) |
|---|---|---|---|
| 1 | The simple version did not notice many timeouts in a row | ~40 h wasted, ~12,000 IDs to retry | Circuit breaker: halt after 20 network errors in a row |
| 2 | No monitoring during the run | The problem went unnoticed for 1.5 days | Clear `halted (network down)` / `halted (blocked)` log lines; check `tail logs/run.log` twice a day |
| 3 | The simple version was run without `tee` | No log to analyse afterwards | `logging` writes `logs/run.log` automatically, with the full date on every line |
| 4 | Restarting Windows to fix the Wi-Fi | Killed WSL, `tmux` and the scraper; one batch lost | Toggle Airplane mode or the Wi-Fi adapter instead of restarting |
| 5 | Redoing the whole batch after a block | Batch 2 took 3h18m | Kept as a deliberate trade-off: every saved batch is complete |
| 6 | Settings edited inside the code (`LIMIT = 3000` left in) | A "full run" could silently fetch only 3,000 IDs | Command-line options with safe defaults |
| 7 | Project on the Windows drive (`/mnt/c`) | Slower file writes; false "modified" errors from `git subtree` | Keep long-running projects in the WSL file system (`~/projects/`) |

Not a mistake: the 37% `HTTP 404` rate (about 31% in the first test batches) comes from the data. Those products were removed from Tiki after the ID list was created, and they are correctly never retried.

## Project structure

```
02-tiki-product-scraper/
├── README.md            # start here
├── docs/
│   ├── RUNBOOK.md       # how to run it, every command in order
│   └── GUIDE.md         # how it was built, level by level
├── src/
│   └── scraper.py       # the production scraper (v5)
├── requirements.txt
└── .gitignore
```

Not in Git (see `.gitignore`): `data/` (input IDs), `output/`, `errors/`, `logs/`, `retry/`, `.venv/`.

## Lessons learned

- **Estimate the runtime first.** At 1.4 s per ID, 200,000 IDs take more than 3 days. That decided the design: batches, checkpoints and `tmux`.
- **A 200 status can still be a failure.** Anti-bot pages arrive as `200 OK` with HTML. Checking `Content-Type` catches them.
- **Permanent and temporary errors are different.** `HTTP 404` (product gone) is never retried; timeouts, `429` and `5xx` are.
- **`tmux` does not survive a Windows restart.** Restarting to fix Wi-Fi stopped WSL and the scraper with it. The checkpoint limited the loss to one batch.
- **Handling each error is not enough; watch the error rate.** Every timeout was recorded correctly, yet 1.5 days of them went unnoticed. In real pipelines this is the job of **monitoring and alerting**.
- **"Finished" is not "complete".** Products + errors must equal the number of input IDs; the scraper checks this at the end and reports it in its exit code.
- **Production-ready means safe for someone else to run.** Configuration from the outside, failures that stop loudly, and a result that can be checked automatically.
