# Next Steps

## Where we are now

- MongoDB Atlas (M0 free tier) — `renderboard` database, `miniStories` collection
- ETL script (`src/analytics_sync.py`) flattens nested documents into 3 collections:
  - `analytics_user_snapshots`
  - `analytics_chat_events`
  - `analytics_daily_stats`
- Metabase running locally via Docker on `http://localhost:3000`
- Metabase connected to MongoDB — but **no SQL support** (MongoDB connector only speaks aggregation pipeline JSON)

---

## The problem

Metabase's MongoDB connector does not support SQL. To write SQL queries in Metabase you need a SQL database as the data source.

---

## Option 1: MongoDB BI Connector — BLOCKED (requires Atlas M10+)

MongoDB Atlas provides a BI Connector that exposes collections as a MySQL-compatible SQL interface.

**Why it's blocked:** You are on the free M0 tier. The BI Connector requires M10 ($57/mo) or above.

**If you ever upgrade to M10:**
1. In Atlas → Data Federation → enable the SQL/BI interface
2. In Metabase → Add database → MySQL → use the BI Connector connection string
3. Write SQL directly against your flat collections

---

## Option 2: Add PostgreSQL layer (recommended, free)

Add a free managed PostgreSQL database on Render. The ETL script writes the same flat data to Postgres tables in addition to MongoDB. Metabase connects to Postgres and you get full SQL.

### Architecture after the change

```
MongoDB Atlas (miniStories)
    |
    └── analytics_sync.py
              |
              ├──► MongoDB flat collections  (keep as-is)
              |
              └──► Render PostgreSQL
                        |
                   Metabase (full SQL)
```

### Steps to implement

**1. Add Postgres to render.yaml**

Add a free Render PostgreSQL database to the blueprint. Render injects `DATABASE_URL` automatically into the `analytics-sync` service.

**2. Add psycopg2 to pyproject.toml**

```toml
dependencies = [
    "pymongo>=4.6",
    "python-dotenv>=1.0",
    "psycopg2-binary>=2.9",
]
```

**3. Update analytics_sync.py**

Add a Postgres write block after the existing MongoDB writes:
- `TRUNCATE` + `INSERT` for `user_snapshots` and `daily_stats` (full replace each run)
- `INSERT ... ON CONFLICT DO NOTHING` for `chat_events` (insert-once by messageId)

**4. Connect Metabase to Postgres**

In Metabase → Add database → PostgreSQL → use the `DATABASE_URL` from Render.

**5. Write SQL in Metabase**

Examples:

```sql
-- Level distribution
SELECT level, COUNT(*) AS users
FROM user_snapshots
WHERE is_test = false
GROUP BY level ORDER BY level;

-- Daily active users over time
SELECT day, dau, total_messages
FROM daily_stats
ORDER BY day;

-- Top engaged users
SELECT user_id, title, total_messages, mcq_responses, open_ended_responses
FROM user_snapshots
WHERE is_test = false
ORDER BY total_messages DESC
LIMIT 20;

-- MCQ vs open-ended per day
SELECT day,
       SUM(CASE WHEN msg_class = 'mcq_response' THEN 1 ELSE 0 END) AS mcq,
       SUM(CASE WHEN msg_class = 'open_ended'   THEN 1 ELSE 0 END) AS open_ended
FROM chat_events
GROUP BY day ORDER BY day;
```

### Postgres table schema

**user_snapshots**
| Column | Type |
|---|---|
| mongo_id | TEXT PRIMARY KEY |
| user_id | TEXT |
| story_id | TEXT |
| title | TEXT |
| level | INT |
| score | INT |
| story_status | TEXT |
| total_messages | INT |
| user_messages | INT |
| mcq_responses | INT |
| open_ended_responses | INT |
| regen_loops | INT |
| character_count | INT |
| location_count | INT |
| topic_entropy | FLOAT |
| total_input_tokens | INT |
| total_output_tokens | INT |
| total_cached_tokens | INT |
| first_activity | TIMESTAMPTZ |
| last_activity | TIMESTAMPTZ |
| model_id | TEXT |
| system_prompt_version | TEXT |
| is_test | BOOLEAN |
| snapshot_at | TIMESTAMPTZ |

**chat_events**
| Column | Type |
|---|---|
| message_id | TEXT PRIMARY KEY |
| story_id | TEXT |
| user_id | TEXT |
| role | TEXT |
| msg_class | TEXT |
| has_mcq | BOOLEAN |
| content | TEXT |
| timestamp | TIMESTAMPTZ |
| day | TEXT |
| level | INT |
| input_tokens | INT |
| output_tokens | INT |
| cached_tokens | INT |
| instruction_lag_seconds | FLOAT |
| is_test | BOOLEAN |

**daily_stats**
| Column | Type |
|---|---|
| day | TEXT PRIMARY KEY |
| dau | INT |
| new_stories | INT |
| total_messages | INT |
| total_user_messages | INT |
| mcq_responses | INT |
| open_ended_responses | INT |
| total_input_tokens | INT |
| total_output_tokens | INT |
| total_cached_tokens | INT |
| avg_instruction_lag_seconds | FLOAT |
| computed_at | TIMESTAMPTZ |

### Render free Postgres limits
- 256 MB storage
- Expires after 90 days (fine for a March 2026 contest)
- If you need permanent free Postgres: use Supabase or Neon instead (both free forever, ~500 MB)

---

## Pending task (unrelated to SQL)

Before building any dashboards, drop the old `analytics_user_snapshots` collection and re-run the sync so all 795 documents are present:

```bash
uv run python -c "
from dotenv import load_dotenv; load_dotenv()
from src.db import get_db
get_db().drop_collection('analytics_user_snapshots')
print('Dropped.')
"

uv run python -m src.analytics_sync --include-test
# Should print: Upserted 795 user snapshots.
```
