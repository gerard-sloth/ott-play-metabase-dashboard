# Automated Analytics Scheduling

## Option A — GitHub Actions (recommended, MongoDB Atlas)

Since MongoDB is on Atlas (cloud-accessible), GitHub Actions handles scheduling for free. No machine needs to stay on. Runs even when your laptop is closed.

### Step 1 — Add secrets to GitHub

Go to your repo on GitHub → **Settings → Secrets and variables → Actions → New repository secret**

Add these two secrets:

| Name | Value |
|---|---|
| `MONGO_URI` | Your Atlas connection string (same as in `.env`) |
| `OPENAI_API_KEY` | Your OpenAI key (used by GEval) |

### Step 2 — Push the workflows

The workflow files are already in `.github/workflows/`:
- `analytics_sync.yml` — ETL sync, runs every hour
- `geval_sync.yml` — GEval sync, runs every 8 hours

Just push to `main` and they activate automatically:

```bash
git add .github/
git commit -m "Add scheduled analytics workflows"
git push
```

### Step 3 — Verify

Go to your repo → **Actions** tab. You'll see both workflows listed. You can also click **"Run workflow"** to trigger them manually without waiting for the schedule.

### To check logs

GitHub Actions → click the workflow run → click the job → expand the step output.

---

## Option B — Cron (local Mac, machine must stay on)

Use this if GitHub Actions is not available. Requires your Mac to be awake.

### What is cron?

Cron is the built-in macOS/Linux task scheduler. You give it a list of commands with a time pattern and it runs them automatically in the background — even if you close your terminal. No Airflow, no Docker, no extra setup needed.

---

## What gets scheduled

| Job | Script | Frequency | Why |
|---|---|---|---|
| ETL sync | `scripts/sync_analytics.sh` | Every hour | Keeps MongoDB analytics collections fresh |
| GEval evaluation | `scripts/run_geval_cron.sh` | Every 8 hours | Makes LLM API calls — costs money, don't run too often |

**MongoDB views** (`create_views.py`) do NOT need to be scheduled — they are live aggregations that always reflect the latest data automatically.

---

## One-time setup

### Step 1 — Get your absolute project path

Open a terminal, `cd` into this project, then run:

```bash
pwd
```

Example output:
```
/Users/gerard/Projects/ott-play-metabase-dashboard
```

Copy this path — you'll need it in the next step.

---

### Step 2 — Open your crontab

```bash
crontab -e
```

This opens a text editor (usually `vim` or `nano`). If it opens vim and you don't know how to use it, type `i` to enter insert mode first.

---

### Step 3 — Add the two cron lines

Paste the following at the bottom of the file. **Replace `/Users/gerard/Projects/ott-play-metabase-dashboard` with your actual path from Step 1.**

```cron
# ETL sync — every hour (user snapshots, chat events, daily stats, topic events)
0 * * * * /bin/bash /Users/gerard/Projects/ott-play-metabase-dashboard/scripts/sync_analytics.sh >> /Users/gerard/Projects/ott-play-metabase-dashboard/logs/sync.log 2>&1

# GEval evaluation — every 8 hours at 00:00, 08:00, 16:00 (makes LLM API calls)
0 0,8,16 * * * /bin/bash /Users/gerard/Projects/ott-play-metabase-dashboard/scripts/run_geval_cron.sh >> /Users/gerard/Projects/ott-play-metabase-dashboard/logs/geval.log 2>&1
```

---

### Step 4 — Save and exit

- **vim:** press `Esc`, then type `:wq` and hit Enter
- **nano:** press `Ctrl+O` to save, then `Ctrl+X` to exit

You should see: `crontab: installing new crontab`

---

### Step 5 — Verify it was saved

```bash
crontab -l
```

This prints your current crontab. You should see the two lines you just added.

---

## How to read the cron time pattern

```
0 * * * *   →  "at minute 0 of every hour"
0 0,8,16 * * *   →  "at minute 0, when the hour is 0, 8, or 16"

┌─── minute (0-59)
│  ┌─── hour (0-23)
│  │  ┌─── day of month (1-31)
│  │  │  ┌─── month (1-12)
│  │  │  │  ┌─── day of week (0-6, Sunday=0)
│  │  │  │  │
0  *  *  *  *
```

---

## Monitoring

### Watch the ETL log live

```bash
tail -f logs/sync.log
```

### Watch the GEval log live

```bash
tail -f logs/geval.log
```

### Check the last 50 lines of any log

```bash
tail -50 logs/sync.log
tail -50 logs/geval.log
```

### See all scheduled jobs

```bash
crontab -l
```

---

## Important notes

- **`.env` is loaded automatically** — the scripts source your `.env` file so `MONGO_URI`, `OPENAI_API_KEY`, and other variables are available even though cron doesn't inherit your shell environment.
- **Logs are saved** in the `logs/` folder (gitignored). If a sync fails, the error will be there.
- **The machine must be on** — cron only runs when your Mac is awake. If you need it to run on a server 24/7, copy the same crontab lines onto the server.
- **To stop a job**, run `crontab -e` again and delete the relevant line.

---

## Removing the cron jobs

```bash
crontab -e
# Delete the lines you added, save and exit
```

Or to remove ALL your cron jobs at once (careful!):

```bash
crontab -r
```

---

## Manual run (anytime)

You can always trigger a sync manually without waiting for the schedule:

```bash
# ETL sync
bash scripts/sync_analytics.sh

# GEval evaluation
bash scripts/run_geval_cron.sh
```
