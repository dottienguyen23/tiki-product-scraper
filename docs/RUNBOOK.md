# Runbook: running the Tiki scraper

Every command needed to run `src/scraper.py`, from an empty folder to a checked result. The *why* behind each design choice is in the [Guide](GUIDE.md); the results are in the [README](../README.md).

All commands run in WSL from the project root. **Every new terminal or `tmux` window** needs the virtual environment activated first; without it, `python` is "command not found":
```bash
cd /mnt/c/Users/Admin/Desktop/data-engineering/02-tiki-product-scraper
source .venv/bin/activate        # the prompt now starts with (.venv)
```

## Golden rules

1. **Only one job talks to Tiki at a time.** Every run from this machine shares one IP address, and Tiki adds their traffic together.
2. **Never restart Windows or let it sleep during a run.** That stops WSL, `tmux` and the scraper. Set *Settings → System → Power* → sleep **Never**. Fix Wi-Fi by toggling Airplane mode, not by restarting.
3. **Never change `BATCH_SIZE`**, and keep `--limit` a multiple of 1,000. The checkpoint depends on batch numbers.
4. **Stay below Tiki's limit.** Measured: 1.17 req/s is safe, 1.43 req/s gets blocked. Never use `--interval` below `0.85` or more than one worker.

---

## 1. Setup (once)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
mkdir -p data
```
Put the ID list at `data/ids.csv` (one ID per line, header `id`). Check it:
```bash
head -3 data/ids.csv
wc -l data/ids.csv          # about 200,001 lines (IDs + header)
```
`.gitignore` must contain: `.venv/`, `data/`, `output/`, `errors/`, `logs/`, `retry/`, `*.tmp`.

## 2. Check the API

```bash
curl -s -o /dev/null --max-time 10 -w "%{http_code} %{content_type}\n" -A "Mozilla/5.0" \
  https://api.tiki.vn/product-detail/api/v1/products/1391347
```
| Output | Meaning |
|---|---|
| `200 application/json; charset=utf-8` | API works, not blocked |
| `200 text/html` / `429` / `403` | blocked: wait and check again in a few minutes |
| `000` | no connection: check your network |

The scraper runs the same check itself (pre-flight) and stops with exit code `1` if it fails.

## 3. Options

```bash
python src/scraper.py --help
```
| Option | Default | Meaning |
|---|---|---|
| `--ids` | `data/ids.csv` | input ID list |
| `--root` | project root | where `output/`, `errors/` and `logs/` are created |
| `--limit` | none (all IDs) | test on the first N IDs (multiple of 1,000) |
| `--interval` | `1.0` | seconds between request starts. `0.85` is the fastest measured safe value |
| `--workers` | `1` | parallel requests. Keep at 1: the rate limit is what matters |
| `--timeout` | `10` | seconds to wait for one response |
| `--max-network-errors` | `20` | network errors in a row that mean "connection down" |
| `--check-every` / `--max-wait` | `120` / `3600` | while blocked or offline: check every 2 min, give up after 1 h |
| `--verbose` | off | also log debug details |

| `--interval` | Rate | One batch | 200,000 IDs |
|---|---|---|---|
| `1.0` (default) | 1.00 req/s | ~17 min | ~56 h |
| `0.85` | 1.17 req/s | ~14 min | ~47 h |

## 4. Test run

```bash
python -u src/scraper.py --limit 3000
echo $?                     # 0 = finished and reconciled
```
Check: 3 files in `output/`, errors mostly `HTTP 404`, and the last log lines say `-> OK`. These batches are kept: the full run skips them.

## 5. Full run (2–3 days)

```bash
tmux new -s tiki
source .venv/bin/activate
python -u src/scraper.py --interval 0.85
# Ctrl-b then d  -> detach; the scraper keeps running
```
The log is written to `logs/run.log` automatically, so `tee` is not needed.

| Command | Purpose |
|---|---|
| `tmux ls` | list sessions |
| `tmux attach -t tiki` | watch progress (leave with `Ctrl-b` then `d`; `Ctrl-c` stops the scraper) |
| `pgrep -af scraper` | is the process alive? |
| `kill <PID>` | stop politely; finished batches are kept |

To resume after any stop, run the same command again: finished batches are skipped.

## 6. Monitor (at least twice a day)

```bash
tail -5 logs/run.log                                     # last batches
ls -lt output/ | head -3                                 # newest batch and when it was saved
cut -d, -f2 "$(ls -t errors/*.csv | head -1)" | sort | uniq -c | sort -rn   # errors of the newest batch
```
| Log message or sign | Meaning | Action |
|---|---|---|
| `Batch NNNN halted (blocked)` | Tiki is blocking | Nothing: the scraper waits, then redoes the batch |
| `Batch NNNN halted (network down)` | 20 network errors in a row | Check Wi-Fi (step 2). Fix it without restarting |
| `API unavailable for over 60 min. Stopping.` | the wait ran out | Fix the cause, then rerun the step 5 command |
| No new batch for more than 1 hour, process alive | stuck, or Windows slept | `tmux attach -t tiki` and look |
| `pgrep` shows nothing | the scraper stopped | rerun the step 5 command |

## 7. Check the result

At the end of a full run the scraper reconciles the output itself and logs, for example:
```
Validation: 200/200 batches | products=... errors=... | duplicates=0
Validation: products + errors = 200000 | input IDs = 200000 -> OK
```
| Exit code | Meaning |
|---|---|
| `0` | finished, and products + errors = input IDs |
| `1` | stopped early: pre-flight failed, or blocked/offline for too long |
| `2` | finished, but the reconciliation failed: read the `Missing batches` line |
| `130` | stopped with `Ctrl-c` |

## 8. Retry the temporary errors (optional)

`HTTP 404` means the product no longer exists, so it is never retried. Everything else (`ConnectionError`, `ReadTimeout`, `HTTP 5xx`...) can succeed on a second pass. Build the retry list, then run the same scraper into a separate folder so the first pass is never touched:
```bash
mkdir -p retry
( echo id; awk -F, 'FNR > 1 && $2 != "HTTP 404" {print $1}' errors/errors_*.csv ) > retry/ids.csv
wc -l retry/ids.csv
python -u src/scraper.py --ids retry/ids.csv --root retry --interval 0.85
```
Results:
- products from both passes: `output/` + `retry/output/`
- final failed IDs: the `HTTP 404` rows in `errors/` + everything in `retry/errors/`

## 9. Commit and publish

```bash
cd /mnt/c/Users/Admin/Desktop/data-engineering/02-tiki-product-scraper
git status                 # nothing from data/, output/, errors/, logs/, retry/
git add -A
git commit -m "<what changed>"
git push
```
