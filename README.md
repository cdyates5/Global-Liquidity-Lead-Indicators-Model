# Global Liquidity Lead Indicator Model

A self-updating dashboard. Each weekday morning (Melbourne time) a GitHub Actions job:
1. downloads about 48 public data series
2. rebuilds the model
3. commits the new `index.html` and `signal.csv` to this repo
4. publishes the page on GitHub Pages

No servers, no paid data, and no API keys required.

**Page address once set up:** `https://<your-github-username>.github.io/<repo-name>/`
**Signal history as CSV:** the same address plus `signal.csv`

## Files

| File | What it does |
|---|---|
| `rebuild.py` | Fetches the data, runs the model and writes `index.html` and `signal.csv`. One self-contained script. |
| `requirements.txt` | Python packages, pinned to the versions this was tested with. |
| `.github/workflows/refresh.yml` | The schedule, the build, the commit and the Pages deployment. |
| `index.html` | The dashboard. Overwritten by every run. |
| `signal.csv` | Monthly signal history (signal, sum of 7 adjusted indicators, minus liquidity z, full-window flag). Overwritten by every run. |
| `.gitignore` | Keeps the raw download cache (`raw/`) out of the repo. |

## Set up (about 10 minutes, one time)

### 1. Create the repository
On GitHub: **+** → **New repository**. Give it a name (e.g. `gli-liquidity-dashboard`) and leave it empty: no README, no .gitignore.

- **Public** repos get GitHub Pages free.
- **Private** repos need a paid plan (GitHub Pro, Team or Enterprise) for Pages. Otherwise the workflow still refreshes `index.html` in the repo, but the deploy step fails.

### 2. Upload the files
**Option A, command line** (from the unzipped folder):
```bash
git init -b main
git add .
git commit -m "Initial dashboard"
git remote add origin https://github.com/<you>/<repo-name>.git
git push -u origin main
```

**Option B, browser:** on the empty repo's page, click **uploading an existing file** and drag in everything from the unzipped folder, including the `.github` folder. Commit to `main`.

If your file browser hides `.github`, show hidden files: **Cmd+Shift+.** on macOS, or **View → Hidden items** in Windows Explorer. Then check that `.github/workflows/refresh.yml` appears in the repo after uploading.

### 3. Let the workflow write to the repo
**Settings → Actions → General → Workflow permissions** → choose **Read and write permissions** → **Save**.

### 4. Turn on GitHub Pages
**Settings → Pages → Build and deployment → Source** → choose **GitHub Actions**.

### 5. (Optional, recommended) Add a free FRED API key
FRED's public CSV endpoint works without a key, but its official API is more reliable from GitHub's servers.
1. Get a free key: sign in at fred.stlouisfed.org → **My Account → API Keys**.
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**. Name it `FRED_API_KEY` and paste the key as the value.

The key is never written to the page or the logs. GitHub masks secrets in logs, and the script strips the key from any error message.

### 6. Run it once
**Actions** tab → **Refresh dashboard** → **Run workflow** → **Run workflow**. The first run takes about 3–5 minutes. When both jobs (`build` and `deploy`) show a green tick, open the page address above.

After that it runs on schedule.

## Schedule

`cron: "23 22 * * 0-4"` = 22:23 UTC Sunday–Thursday. That is:
- 9:23 am Melbourne, Monday–Friday, in daylight time
- 8:23 am Melbourne in winter

At that hour the previous US trading day's data is available. GitHub sometimes starts scheduled runs late (up to around 30 minutes).

To change the schedule, edit the `cron` line in `.github/workflows/refresh.yml`. Times are always UTC.

Most inputs are monthly, so the signal usually moves only when:
- a month completes (market indicators), or
- a new release lands (money supply, reserves, CPI, unemployment).

Daily runs pick these up within a day. The **Built** time on the page doubles as a heartbeat: if it stops advancing, the job has stopped.

The job also runs when you push a change to `rebuild.py`, `requirements.txt` or the workflow file. You can run it any time with **Run workflow**.

## What happens when a data source fails

- **A source is down or rate-limited today:** the script uses the last good download of that series, saved from the previous run. The page then shows a red note under the title and lists the affected series under *Method and data*.
- **MSCI's index feed is unavailable and there is no saved copy:** the ACWI ETF price from Yahoo is used instead, and the page says so.
- **A series has no data at all (live or saved), the signal can't be computed, or the latest data is more than three months old:**
  - the script stops with a `NOT PUBLISHING:` message
  - nothing is committed or deployed
  - the live page keeps the last good build
  - GitHub emails you that the run failed

## Troubleshooting

| Symptom | Fix |
|---|---|
| Push step fails with `403` / `Permission denied` | Step 3: set workflow permissions to **Read and write**. |
| `deploy` job fails with "Pages not enabled" or similar | Step 4: set the Pages source to **GitHub Actions**. On a private repo, check your plan includes Pages. |
| First run fails with `NOT PUBLISHING: no data ... for fred_...` | Add the `FRED_API_KEY` secret (step 5) and run again. |
| First run fails on another source (e.g. `em_` = East Money, `msci_`, `yahoo_`) | That site may be blocking GitHub's servers. Run `python rebuild.py` once on your own machine, remove the `raw/` line from `.gitignore`, then commit and push the `raw/` folder. The workflow uses it as the fallback copy from then on. |
| The schedule stopped running | GitHub disables scheduled workflows in public repos after 60 days with no repo activity. The daily commit normally prevents this. If it does happen: **Actions → Refresh dashboard → Enable workflow**. |
| Page shows old numbers right after a run | Pages can take a minute or two to update. Hard-refresh the browser. |

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python rebuild.py              # live data; writes index.html and signal.csv
python rebuild.py --offline    # rebuild from the raw/ cache without network
```

Optional: `export FRED_API_KEY=...` before running.

## Method in brief

**Liquidity proxy.** Central bank balance sheets (Fed, ECB, BoJ), plus broad money (US, euro area, Japan, China, UK), plus FX reserves ex gold (China, Japan, India, Korea, Saudi Arabia, Brazil). In USD.

**Indicators.** Nine indicators, each as a YoY change:
- global stocks
- Bitcoin
- USD NEER
- FX volatility
- global 10y yields
- MOVE
- global real GDP
- global core CPI
- unemployment

**Calculation.** For each indicator, the structural (10-year) z-score is multiplied by the average of its trailing 10-year correlation with liquidity YoY 3 and 6 months ahead. The seven non-market indicators are summed, and liquidity's own YoY z-score is subtracted.

**Reading the signal.**
- Below 0.5 in absolute value: modest up- or downtrend.
- 0.5 and above in absolute value: significant.

The page covers the data sources, proxies, backtest and its limits in full.

## Sources
FRED (St. Louis Fed), ECB Data Portal, Eurostat, Bank of Japan, Statistics Bureau of Japan (e-Stat), Bank of England, ONS, BIS, MSCI, East Money, Yahoo Finance.
