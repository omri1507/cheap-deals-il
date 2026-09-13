# Cheap deals — Florentin, Tel Aviv

Weekly job that pulls current promotions from Rami Levy, Shufersal, and
Victory (Israel's mandatory price-transparency data), filters to your
Florentin-area branches, and keeps only vegan/food-relevant deals.

It runs on GitHub Actions (real internet access, free), not inside Claude —
Claude's own sandbox can't reach the chains' data portals directly. Each
week Claude reads the result this job produces and messages you a digest.

## Setup (one-time, ~5 minutes)

1. Create a new **private** GitHub repo (any name — e.g. `cheap-deals-il`)
   and push everything in this folder to it:

   ```bash
   cd cheap-deals-il
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```

2. On GitHub, go to the repo's **Settings → Actions → General → Workflow
   permissions**, and set it to **"Read and write permissions"**. (The
   workflow needs this to commit the weekly results back to the repo.)

3. Go to the **Actions** tab, open "Weekly deals digest", and click
   **"Run workflow"** to trigger a first manual run instead of waiting for
   Sunday. It takes a few minutes.

4. Tell Claude the repo is set up (and its URL). Claude will set up a
   weekly scheduled task that reads
   `https://raw.githubusercontent.com/<you>/<repo>/main/data/latest_deals.json`
   and messages you the digest.

## First run: expect one calibration step

I couldn't test this end-to-end from inside Claude's sandbox (that's the
whole reason this runs on GitHub Actions instead) — so the first real run
is a "does the data look right?" check, not a guarantee:

- **`data/stores_seen.csv`** — every branch address found for these three
  chains. Open it, confirm which rows are actually your Florentin
  branches, and tighten `BRANCH_MATCH` in `config.py` if needed (e.g. add
  a street name if "פלורנטין" isn't in the address text one chain uses).
- **`data/_debug_columns.json`** — the raw column names the parser found.
  If `data/latest_deals.json` comes back with 0 deals, this file is the
  first place to look — it usually means one column name in
  `weekly_deals.py` (search for `pick_column`) needs a small tweak to
  match what that chain's file actually calls its fields.
- **`data/digest.md`** — human-readable version of the same output, good
  for eyeballing whether the vegan filter is behaving (see
  `NON_VEGAN_KEYWORDS` in `config.py` — it's a keyword heuristic, not a
  certified ingredient check, so it's worth skimming for false
  positives/negatives after the first couple of runs).

If something needs fixing, just paste the relevant file's content back to
Claude and it can adjust `config.py` or `weekly_deals.py` directly.

## Notes

- Nitzat HaDuvdevan isn't included — it's not part of the standard
  price-transparency scraper's chain list, likely because it falls under
  the size threshold the law applies to.
- The vegan filter is a Hebrew keyword blocklist (see `config.py`). It
  will miss non-obvious animal ingredients (gelatin, whey powder, some
  E-numbers) and can occasionally over-exclude (e.g. "חלבה" contains
  "חלב"). Treat the digest as a first pass, not a certified list.
