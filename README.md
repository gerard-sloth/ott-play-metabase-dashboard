# analytics-metabase

Monitoring dashboard for OTT Play's story-building contest (March 2026).
Self-hosted Metabase + a Python ETL sync pipeline on top of MongoDB Atlas.

---

## Architecture

```
MongoDB Atlas
  renderboard / miniStories (raw nested documents)
       │
       └── analytics_sync.py  (Python ETL — run manually or on schedule)
                │
                ├── analytics_user_snapshots   (one flat doc per user/story)
                ├── analytics_chat_events      (one flat doc per message)
                └── analytics_daily_stats      (one flat doc per day)
                         │
                         └── Metabase (self-hosted, port 3000)
                                  └── drag-and-drop dashboards for non-technical users
```

**Why flatten?**
MongoDB's nested arrays (`chatSession.messages[]`, `improvementState.pointsEvents[]`) can't be sliced in Metabase's GUI without custom aggregation pipelines. The ETL writes pre-computed, flat documents so all dashboard columns are available as simple fields.

---

## Repository structure

```
analytics-metabase/
├── docker-compose.yml        Metabase container
├── render.yaml               Render deployment blueprint
├── pyproject.toml            Python project (uv)
├── .env.example              Env var template
├── .gitignore
├── README.md
└── src/
    ├── __init__.py
    ├── db.py                 MongoDB connection + pipeline fragments
    ├── metrics.py            Pure metric computation functions
    └── analytics_sync.py     Main ETL sync script
```

---

## Quick start (local)

### 1. Set up environment

```bash
cp .env.example .env
# Edit .env — fill in your MONGODB_URI
```

### 2. Install Python dependencies

```bash
uv sync
```

### 3. Run the sync

```bash
# Production data only (excludes test=true documents)
uv run python -m src.analytics_sync

# Include test documents
uv run python -m src.analytics_sync --include-test
```

Expected output:
```
Starting analytics sync (include_test=False)…
  Ensuring indexes…
  Indexes verified.
  Found 42 stories to process.
  Upserted 42 user snapshots.
  Inserted 1038 chat events, skipped 0 duplicates.
  Wrote 12 daily stat records.
Sync complete.
```

### 4. Start Metabase

```bash
docker compose up -d
# Open http://localhost:3000
```

---

## MongoDB connection in Metabase

1. Complete the Metabase setup wizard at `http://localhost:3000`
2. **Add a database** → choose **MongoDB**
3. Enter the connection string from your `.env`:
   ```
   mongodb+srv://user:password@cluster.mongodb.net/renderboard
   ```
4. After connecting, use these collections (not the raw `miniStories`):
   - `analytics_user_snapshots`
   - `analytics_chat_events`
   - `analytics_daily_stats`

---

## Analytics collections schema

### `analytics_user_snapshots`

| Field | Type | Description |
|---|---|---|
| `userId` | string | Unique user ID (index) |
| `storyId` | string | Story document ID |
| `title` | string | Story title |
| `level` | int | Current level (0–7) |
| `score` | int | Total points |
| `storyStatus` | string | `not_started` / `wip` / `submitted` |
| `totalMessages` | int | All messages in chat |
| `userMessages` | int | User-only messages |
| `mcqResponses` | int | MCQ (preset options) responses |
| `openEndedResponses` | int | Free-text responses |
| `regenLoops` | int | "Other" signals — dissatisfaction count |
| `characterCount` | int | Number of characters developed |
| `locationCount` | int | Number of locations developed |
| `topicEntropy` | float | Shannon entropy of topic diversity (higher = more balanced) |
| `levelUpTimes` | array | Seconds between consecutive points events |
| `firstActivity` | datetime | First user message timestamp |
| `lastActivity` | datetime | Last user message timestamp |
| `totalInputTokens` | int | LLM input token total |
| `totalOutputTokens` | int | LLM output token total |

### `analytics_chat_events`

| Field | Type | Description |
|---|---|---|
| `messageId` | string | Unique message ID (index) |
| `storyId` | string | Parent story |
| `userId` | string | Author |
| `role` | string | `user` / `assistant` |
| `msgClass` | string | `mcq_response` / `open_ended` / `assistant_response` |
| `hasMCQ` | bool | Assistant message offered preset options |
| `timestamp` | datetime | Message time |
| `day` | string | `YYYY-MM-DD` partition key |
| `level` | int | Story level at time of message |
| `instructionLagSeconds` | float | Seconds between Guru prompt and user reply |
| `inputTokens` | int | LLM input tokens (assistant messages) |
| `outputTokens` | int | LLM output tokens |

### `analytics_daily_stats`

| Field | Type | Description |
|---|---|---|
| `day` | string | `YYYY-MM-DD` (index) |
| `dau` | int | Daily active users |
| `newStories` | int | Stories active that day |
| `totalMessages` | int | All messages that day |
| `mcqResponses` | int | MCQ responses that day |
| `openEndedResponses` | int | Open-ended responses that day |
| `avgInstructionLagSeconds` | float | Avg Guru → user response time |
| `totalInputTokens` | int | LLM cost proxy |

---

## Key dashboards to build in Metabase

**Progression & Funnel**
- Level distribution bar chart (`analytics_user_snapshots.level`)
- Drop-off funnel: Level 1 → 2 → 3 → 4 (`storyStatus`)
- Story status pie: `not_started` / `wip` / `submitted`

**Guru Chat Engagement**
- MCQ vs open-ended ratio (`mcqResponses` / `openEndedResponses`)
- Avg instruction lag over time (`analytics_chat_events.instructionLagSeconds` by `day`)
- Topic entropy distribution (`topicEntropy`)
- Regeneration loops per user (`regenLoops`)

**Daily Activity**
- DAU over time (`analytics_daily_stats.dau`)
- Messages per day (`totalMessages`)
- Token usage over time (`totalInputTokens` + `totalOutputTokens`)

---

## Deployment on Render

The `render.yaml` blueprint defines two services:

| Service | Type | Plan | Purpose |
|---|---|---|---|
| `metabase` | web (Docker) | Starter ($7/mo) | Hosts the Metabase UI with persistent disk |
| `analytics-sync` | worker (Python) | Free | Runs the ETL sync on demand |

**Steps:**
1. Push this repo to GitHub
2. In the Render dashboard: **New → Blueprint** → connect the repo
3. Set the `MONGODB_URI` secret in the `analytics-sync` service environment
4. Trigger the `analytics-sync` worker manually after each contest event, or set up a Render Cron Job

> **Disk note:** Metabase stores its own metadata (dashboards, users, saved questions) in an H2 embedded database at `/metabase-data/metabase.db`. Without a persistent disk (Starter plan), this resets on every deploy. For production use, the Starter plan disk ($1/mo extra) is strongly recommended.

---

## Security

- **Never commit `.env`** — it is listed in `.gitignore`
- Use `.env.example` as the template; fill in real values locally
- Set `MONGODB_URI` as a secret in the Render dashboard (not in `render.yaml`)
- Rotate credentials if they were ever committed to git history
