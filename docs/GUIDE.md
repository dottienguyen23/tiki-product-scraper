# Tiki Product Scraper – Step-by-step Guide (Project 02)

**Goal:** fetch about 200,000 Tiki products through the API, keep 6 fields (`id`, `name`, `url_key`, `price`, `description`, `images`), clean the `description`, save about 1,000 products per JSON file, and record every failed ID with its reason.

This guide does not start with the final code. Each level begins with a **problem**. Try to answer **🤔 Think first** before you open the solution.

The published code is the final production version, [`src/scraper.py`](../src/scraper.py) (level 10). The intermediate scripts from levels 1–9 are not in the repository; each level shows the key code instead. How to run the scraper is in the [Runbook](RUNBOOK.md).

| Section | Meaning |
|---|---|
| 🎯 Goal | What this level achieves |
| ⚠️ Problem | What the previous level was missing, or what broke in a real run |
| 🤔 Think first | Stop and think before reading on |
| 💡 Hint | The function or concept to use |
| ✅ Solution | Code (collapsed, click to open) |
| 📌 Lesson | The data engineering idea behind it |

## Roadmap

| Level | What you build | Problem that leads to the next level |
|---|---|---|
| 0 | `curl` one ID by hand | Doing this 200k times by hand is impossible |
| 1 | Python: one product, 6 fields | `description` is messy HTML |
| 2 | `clean_description()` | Only one ID so far |
| 3 | Loop over many IDs, save one file | One bad response crashes everything |
| 4 | Error handling | 3+ days of running; a restart means starting over |
| 5 | Batches + checkpoint | Tiki blocks bots |
| 6 | Block detection (simple version) | Closing the terminal or restarting kills the scraper |
| 7 | Operations: `tmux`, logs | Is the result complete and correct? |
| 8 | Check the result | Want it more robust and automatic |
| 9 | Hardening (v4) | Settings live in the code; a dead network still produces thousands of errors |
| 10 | Production → `src/scraper.py` (v4.1) | — |

---

## Level 0 – Look at the API by hand

🎯 **Goal:** understand what one API call returns before writing any Python.

⚠️ **Problem:** we don't know the response format yet. Is it JSON? Does it need a special header?

🤔 **Think first:** how can you check whether an endpoint returns JSON or an HTML page **without** printing the whole body?

💡 **Hint:** `curl` options `-o` (where the body goes) and `-w` (print a variable after the request).

<details>
<summary>✅ Solution</summary>

```bash
# Status code + content type only
curl -s -o /dev/null -w "%{http_code} %{content_type}\n" -A "Mozilla/5.0" \
  https://api.tiki.vn/product-detail/api/v1/products/1391347
# -> 200 application/json; charset=utf-8

# Pretty-print the first 40 lines of the body
curl -s -A "Mozilla/5.0" https://api.tiki.vn/product-detail/api/v1/products/1391347 \
  | python -m json.tool | head -40
```

| Option | Meaning |
|---|---|
| `-s` | silent, no progress meter |
| `-o /dev/null` | throw the body away |
| `-w "..."` | write-out: print only these variables |
| `-A "Mozilla/5.0"` | set the User-Agent header |
| `\` | continue the command on the next line |

</details>

📌 **Lesson:** inspect the source before writing code. `Content-Type` tells you whether you received **data** (`application/json`) or a **block page** (`text/html`).

---

## Level 1 – One product in Python

🎯 **Goal:** the same request in Python, keeping only the 6 fields we need.

⚠️ **Problem:** the API returns about 60 fields (`badges`, `inventory`, `seller`...). We need 6.

🤔 **Think first:** why use `p.get("name")` instead of `p["name"]`?

💡 **Hint:** `requests.get(..., timeout=10)`, `r.json()`, `dict.get()`.

<details>
<summary>✅ Solution</summary>

```python
r = requests.get(API_URL.format(PRODUCT_ID), headers=HEADERS, timeout=10)
p = r.json()
product = {
    "id": p.get("id"),
    "name": p.get("name"),
    "url_key": p.get("url_key"),
    "price": p.get("price"),
    "description": p.get("description"),
    "images": [img.get("base_url") for img in (p.get("images") or [])],
}
```

**Answer:** `p["name"]` raises `KeyError` if a product has no `name`; `p.get("name")` returns `None` and the program continues.
`(p.get("images") or [])` handles both a missing field and `null`.

</details>

**Observed in the real data**
- The API has a field spelled `promitions` (Tiki's typo). Keep source keys exactly as they are, or your mapping breaks.
- `name` can contain double spaces: `Tia Sáng  Gương`.
- `price` is an integer in VND: `211200`.

📌 **Lesson:** this is the **Extract** step: take only what the next step needs.

---

## Level 2 – Clean the description

🎯 **Goal:** turn the HTML `description` into clean plain text.

⚠️ **Problem:** `description` contains tags (`<p>`, `<br>`), entities (`&amp;`, `&nbsp;`) and many line breaks.

🤔 **Think first:** what are the three different kinds of "noise" to remove?

💡 **Hint:** `BeautifulSoup(...).get_text(" ")`, `html.unescape()`, `re.sub(r"\s+", " ", ...)`.

<details>
<summary>✅ Solution</summary>

```python
def clean_description(raw_html):
    if not raw_html:
        return ""
    text = BeautifulSoup(raw_html, "html.parser").get_text(" ")  # 1. remove tags
    text = html.unescape(text)                                   # 2. &amp; -> &
    return re.sub(r"\s+", " ", text).strip()                     # 3. collapse whitespace
```

</details>

**Gotchas**
- Without the `" "` separator, `get_text()` glues words together: `<p>Hello</p><p>World</p>` → `HelloWorld`.
- `\s` also matches `\xa0` (the character produced by `&nbsp;`), so step 3 removes it too.

📌 **Lesson:** this is a **Transform** step. Normalize text **before** storing it, so every consumer gets the same clean value.

---

## Level 3 – Loop over many IDs

🎯 **Goal:** read the ID list, fetch each product, save everything to one JSON file.

⚠️ **Problem:** the IDs are in `data/ids.csv` (200,000 lines).

🤔 **Think first:**
1. The first line of the file is the header `id`. What happens if you treat it as a product ID?
2. What if the same ID appears twice?
3. How long will 200,000 requests take at 1 request per second?

💡 **Hint:** `str.isdigit()`, `list(dict.fromkeys(ids))`, `time.sleep()`, `json.dump(..., ensure_ascii=False)`.

<details>
<summary>✅ Solution</summary>

```python
def read_ids(path):
    ids = []
    with open(path, encoding="utf-8-sig") as f:    # utf-8-sig drops the Excel BOM
        for line in f:
            value = line.strip()
            if value.isdigit():                    # skips the header "id" and empty lines
                ids.append(value)
    return list(dict.fromkeys(ids))                # remove duplicates, keep order
```

`ensure_ascii=False` keeps Vietnamese readable in the file (`Gương`) instead of `\u01b0\u01a1...`.

</details>

**Observed in the real run**
- The log showed `1 không hợp lệ ['id']`: the header line, counted as invalid. Harmless, and it proves the validation works.
- Measured speed: about **1.4 s per ID** (1 s delay + response time) → 200,000 × 1.4 s ≈ **78 hours (3+ days)**.

📌 **Lesson:** **estimate the runtime before you run.** A 3-day job needs very different design from a 3-minute one.

⚠️ **New problem:** `r.json()` crashes on the first bad response, and all products are only in memory until the end, so everything is lost.

---

## Level 4 – Handle errors without stopping

🎯 **Goal:** one bad ID is recorded with a reason; the loop continues.

🤔 **Think first:** list everything that can go wrong for **one** request.

<details>
<summary>Answer</summary>

| What goes wrong | How it shows up |
|---|---|
| No network, Wi-Fi drops | `ConnectionError` |
| Server too slow | `Timeout` |
| Product no longer exists | `HTTP 404` |
| Too many requests | `HTTP 429` |
| Server error | `HTTP 500`–`504` |
| Anti-bot page | `200` but HTML instead of JSON |
| Broken body | `r.json()` raises `ValueError` |

</details>

💡 **Hint:** `try/except requests.RequestException`, `timeout=10`, return a tuple `(product, error)`, `requests.Session()`.

<details>
<summary>✅ Solution</summary>

```python
def get_product(session, product_id):
    """Return (product, None) on success, or (None, reason) on failure."""
    try:
        r = session.get(API_URL.format(product_id), headers=HEADERS, timeout=10)
    except requests.RequestException as e:   # base class of Timeout, ConnectionError, ...
        return None, type(e).__name__
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        p = r.json()
    except ValueError:
        return None, "bad_json"
    return {...}, None
```

The caller:
```python
product, error = get_product(session, product_id)
if product:
    products.append(product)
else:
    errors.append([product_id, error])   # -> errors.csv with columns id, reason
```

</details>

**Key points**
- `timeout=10` matters: without it, a dead connection can hang **forever**. The flip side: every timeout costs 10 seconds.
- `requests.Session()` reuses the TCP/TLS connection, which is much faster than `requests.get()` for thousands of calls.
- **404 and 429 need different handling:** 404 is permanent (never retry); 429, 5xx, `Timeout`, `ConnectionError` are temporary (retry later).

**Observed in the real run:** about **31% of each 1,000-ID batch failed** (689 ok / 311 errors, then 687 / 313). An error rate that steady usually comes from the **data**, most likely products deleted since the ID list was made. The `reason` column is how you check that instead of guessing.

**A mistake in the first version:** `p = r.json()` was outside any `try`, so one broken body could crash the whole batch.

📌 **Lesson:** one bad record must never stop the pipeline. Failed records go to a separate place **with the reason** (often called a *dead-letter* file or queue), so they can be analysed and retried.

---

## Level 5 – Batches + checkpoint

🎯 **Goal:** if the run stops, restart it and continue where it stopped.

⚠️ **Problem:** a 3-day job **will** be interrupted (restart, Wi-Fi, crash). With one big file, everything is lost.

🤔 **Think first:**
1. After a restart, how can the script know which work is already done?
2. What if the computer dies **while** a file is being written?

💡 **Hint:** one file per 1,000 IDs, `os.path.exists()`, write to `.tmp` then `os.replace()`.

<details>
<summary>✅ Solution</summary>

```python
for start in range(0, len(ids), BATCH_SIZE):
    batch_no = start // BATCH_SIZE + 1
    batch = ids[start:start + BATCH_SIZE]

    # The output file itself proves the batch is done
    if os.path.exists(f"{OUTPUT_DIR}/products_{batch_no:04d}.json"):
        continue
    ...
    save_batch(batch_no, products, errors)
```

```python
# Atomic write: a crash never leaves a half-written JSON
out = f"{OUTPUT_DIR}/products_{batch_no:04d}.json"
with open(out + ".tmp", "w", encoding="utf-8") as f:
    json.dump(products, f, ensure_ascii=False, indent=2)
os.replace(out + ".tmp", out)
```

</details>

**Answers**
1. The output file **is** the checkpoint. If `products_0007.json` exists, batch 7 is done.
2. If the script wrote straight to `products_0007.json` and died halfway, a broken file would exist and be skipped next time. Writing `.tmp` then renaming with `os.replace()` makes the final file appear **all at once**, because the rename is atomic.

**Gotchas**
- **Never change `BATCH_SIZE` between runs.** Batch numbers would point to different IDs.
- **`LIMIT` must be a multiple of `BATCH_SIZE`.** With `LIMIT = 3500`, batch 4 holds only 500 IDs but is saved as `products_0004.json`. In the full run it counts as "done", and the other 500 IDs are **never fetched**.

**Observed in the real run:** restarting Windows to reconnect Wi-Fi killed the scraper. Only the batch **in progress** was lost; finished batches were skipped on restart.

📌 **Lesson:** long jobs must be **resumable** and **idempotent** (running them again gives the same result, without duplicates). Atomic writes keep the output trustworthy.

---

## Level 6 – Getting blocked (simple version)

🎯 **Goal:** recognize when Tiki blocks the bot, and wait instead of writing thousands of fake errors.

⚠️ **Problem:** anti-bot systems often answer `200 OK` with an **HTML page** ("access denied", captcha). The status looks fine, but there is no data.

🤔 **Think first:** how do you tell a block page apart from a real product? What should the script do then?

💡 **Hint:** check `Content-Type` for `"json"`; on a block, `sleep` and retry **the same ID**.

<details>
<summary>✅ Solution</summary>

```python
if "json" not in r.headers.get("Content-Type", ""):
    return None, "blocked"
```

```python
def get_product_wait_if_blocked(session, product_id):
    while True:
        product, error = get_product(session, product_id)
        if error != "blocked":
            return product, error
        print(f"Blocked at ID {product_id}, waiting {WAIT_IF_BLOCKED}s...")
        time.sleep(WAIT_IF_BLOCKED)
```

</details>

**Limits of this version**
- It waits **forever** if the block never ends.
- A block page returned as `HTTP 403` is not detected and is recorded as a normal error. Many consecutive `HTTP 403` errors almost certainly mean "blocked".
- `r.headers.get("Content-Type", "")`: the `""` default avoids a `TypeError` when the header is missing.

📌 **Lesson:** don't trust the status code alone; check that the payload has the **type** you expect.

---

## Level 7 – Running for days (operations)

🎯 **Goal:** the scraper survives closing the terminal and leaves logs on disk.

⚠️ **Problem:** a process started in a terminal dies with that terminal, and `print` output vanishes.

<details>
<summary>✅ Solution</summary>

```bash
cd /mnt/c/Users/Admin/Desktop/data-engineering/02-tiki-product-scraper
tmux new -s tiki                          # named session that survives closing the window
source .venv/bin/activate
python -u src/scraper.py
# (the simple version needed: python -u scraper_simple.py 2>&1 | tee -a logs/run.log)
# Ctrl-b then d  -> detach, scraper keeps running
```

| Command | Purpose |
|---|---|
| `python -u` | unbuffered: log lines appear immediately |
| `2>&1` | merge errors into normal output |
| `tee -a logs/run.log` | print **and** append to a file |
| `tmux ls` | list sessions |
| `tmux attach -t tiki` | reattach to watch progress |
| `pgrep -af scraper` | is the process alive? (shows the PID) |
| `tail -f logs/run.log` | follow the log live |
| `kill <PID>` | polite stop (SIGTERM) |

</details>

**Observed in the real run**
- **Restarting Windows killed WSL**, and with it tmux and the scraper. tmux protects against closing the window, **not** against shutdown or sleep → set Windows sleep to *Never* while scraping.
- VS Code showed `* History restored`: it brought back the **text** of the old terminal, not the **processes**. The screen looked alive; nothing was running.
- `^[[A^[[B` in the progress bar = the ↑/↓ arrow keys pressed while the script ran. Harmless.
- The project lives on `/mnt/c/...` (Windows drive). WSL is slower there for many small writes; `~/projects/` is faster for heavy I/O.

📌 **Lesson:** a pipeline is code **plus** the way it runs. Logs on disk and processes that survive disconnection are the basics of operations.

---

## Level 8 – Check the result

🎯 **Goal:** prove that nothing is missing or duplicated, and know which errors are worth retrying.

🤔 **Think first:** what equation must be true at the end?

<details>
<summary>Answer</summary>

**products OK + errors = number of unique IDs**, with **0 duplicate ids** in the output. If not, some batch is unfinished or missing.

</details>

💡 **Hint:** `glob`, `csv.DictReader`, `collections.Counter`.

<details>
<summary>✅ Solution</summary>

The script:
1. counts products in `output/products_*.json` and errors in `errors/errors_*.csv`;
2. checks `ok + errors == len(ids)` and counts duplicate ids;
3. prints the errors grouped by `reason`;
4. writes every non-404 error ID to `retry_ids.csv` for a second pass.

`csv.DictReader` reads by column name, so it works even when error files have 2 columns (simple version) or 3 columns (v4 and later).

In v4.1 this check is built in (step `[4]`): it runs at the end of every full run and sets the exit code.

</details>

📌 **Lesson:** this is **reconciliation**, a core data quality check. "The script finished" does not mean "the data is complete".

---

## Level 9 – Hardening (v4)

What v4 adds on top of the simple version, and why:

| Addition | Why |
|---|---|
| `BASE_DIR = Path(__file__).resolve().parent` | Paths work from any working directory (in v4.1 `.parent.parent`, because the script moved into `src/`) |
| `load_ids()` reports invalid and duplicate counts | Bad input is visible instead of silently dropped |
| `explore_one()` test call before the run | Stop early if the API is down or blocked |
| `Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, ...])` | Temporary errors are retried automatically, with growing waits |
| `logging` to screen **and** `logs/run.log` | Logs survive without `tee` |
| `tqdm` + `logging_redirect_tqdm` | Progress bar with an average-based ETA |
| `BLOCKED` (`threading.Event`) | When blocked, stop sending requests (more requests extend the block) |
| `BLOCK_MAX_WAIT` | Give up after 1 hour instead of waiting forever |
| Redo the whole batch after a block | A saved batch is always complete |
| `ThreadPoolExecutor` + `MAX_WORKERS` | Parallel requests (kept at 1: faster got blocked) |
| `validate()` | Level 8 built in |

**Observed in the real run:** batch 1 took **1,382 s**, batch 2 took **11,880 s** (8.6× slower). The likely cause is the "redo the whole batch after a block" rule: a block in the middle of batch 2 means waiting, then fetching all 1,000 IDs again. Check with:
```bash
grep "bị chặn" logs/run.log
```

📌 **Lesson:** every robustness feature is a **trade-off**. Redoing a batch costs up to 999 extra requests but guarantees complete files; waiting per ID (simple version) costs nothing extra but can hang forever.

---

## Level 10 – Production → `src/scraper.py` (v4.1)

🎯 **Goal:** one script that anyone can run safely without editing code, and that tells the terminal (or a scheduler) whether the run succeeded.

⚠️ **Problem:** v4 still had development habits:
- Settings were edited inside the code. `LIMIT = 3000` was left in, so a "full run" would only fetch 3,000 IDs.
- A dead network still turned every ID into `ConnectionError` (the 40-hour Wi-Fi outage).
- The pacing slept 1 s **before** each request, so the real gap was 1 s + Tiki's answer time (~1.4 s per ID).
- No network at startup crashed the pre-flight call with a traceback.
- `validate()` read every file in `output/`, so leftover files from other runs could make it pass wrongly.
- It always exited with code 0, even when it stopped early.

🤔 **Think first:** if this script ran every night from a scheduler, with nobody watching the screen, how would the scheduler know whether the run worked?

💡 **Hint:** keep the v4 structure and fix only what is broken. Small changes, no new framework.

<details>
<summary>✅ Solution: <code>src/scraper.py</code></summary>

| Change | Why |
|---|---|
| `LIMIT` from the command line: `python -u src/scraper.py 3000`; no number = full run | Nobody has to edit the code before running it |
| `wait_turn()`: one request **start** every `INTERVAL` seconds, shared by all threads | Exact rate. 1.0 s = 1 req/s, about 30% faster than v4 at the same safety |
| Circuit breaker: 20 network errors in a row set `STOP` ("network down") | A Wi-Fi outage becomes a wait-and-redo, not thousands of `ConnectionError` rows |
| `try` around the pre-flight request | A clear message instead of a traceback |
| `validate()` reads only this run's batch numbers and reports missing ones | Leftover files can't fake a pass |
| `main()` returns an exit code: `0` / `1` / `2` / `130` | The terminal or a scheduler can tell success from failure |
| `Ctrl-c` sets `STOP` before exiting | Otherwise the thread pool still sends every queued request of the batch |
| Full date in every log line | A run lasts days; `17:03` alone is ambiguous |
| `benchmark()` removed | Only needed during development; its results are in the README |

The pacing, as simple as possible:
```python
def wait_turn():
    global _next_start
    with _pace_lock:                       # threads take turns
        now = time.monotonic()
        start = max(now, _next_start)      # my turn: now, or later if the last request was too recent
        _next_start = start + INTERVAL     # the next request may start INTERVAL seconds after mine
    time.sleep(start - now)                # sleep only the time that is left
```

The circuit breaker, inside `fetch_product()`:
```python
except requests.RequestException as e:
    with _count_lock:
        network_errors_in_a_row += 1
        if network_errors_in_a_row >= MAX_NETWORK_ERRORS:
            stop("network down")
    return pid, None, type(e).__name__, str(e)[:100]
```
Any answer from the server resets the counter to 0, so only errors **in a row** count.

The file names, batch size and checkpoint did **not** change, so v4.1 resumes from v4's output.

</details>

**Answer:** the **exit code**. `echo $?` prints it after a run; schedulers, `cron` and CI systems all read it.

**Gotcha:** the circuit breaker is not instant. `Retry(total=3)` retries each failed request first, so 20 dead IDs in a row take about 15 minutes. Still far better than 40 hours.

📌 **Lesson:** "production-ready" means the script is safe to run by someone who didn't write it: no code edits to start it, failures that stop loudly, and a result that can be checked automatically. It does **not** mean more code: v4.1 is v4 plus small, targeted fixes.

---

## Appendix – Git mistakes made along the way

| Mistake | Effect | Fix |
|---|---|---|
| `git config user.name/email` run **after** the first commit | The first commit has the old identity | `git commit --amend --reset-author --no-edit` (only before pushing) |
| `user.email "<me@gmail.com>"` with angle brackets | The brackets are stored in the email; GitHub can't link commits to the account | `git config --global user.email "me@gmail.com"` |
| `data/` not in `.gitignore` | `git add .` would commit large data files | Add `data/` to `.gitignore` |
| `U` next to a file in VS Code | The file is untracked, not in any commit yet | `git add <file>` then commit |

| `git mv *.py src/` | Only top-level `.py` files moved; the `steps/` folder stayed behind | Move folders separately: `git mv steps src/` |
| `git rm` without `--cached` | Deletes the file from disk, not only from Git | `git rm --cached <file>` keeps it locally; then add it to `.gitignore` |
| Restructuring the subtree copy (`project_02/`) instead of the source folder | The next `git subtree pull` conflicts | Change `Project_02` first, then pull into the portfolio repo |

Tip: commit once per level (`step 3: loop over IDs`) so `git log` tells the same story as this guide.
