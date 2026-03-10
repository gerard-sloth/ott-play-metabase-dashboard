# PostHog SQL Queries — OTT Play Story Builder Dashboard

## Table names

| PostHog table | MongoDB collection |
|---|---|
| `mongodb_analytics_daily_stats` | analytics_daily_stats |
| `mongodb_analytics_user_snapshots` | analytics_user_snapshots |
| `mongodb_analytics_chat_events` | analytics_chat_events |
| `mongodb_analytics_topic_events` | analytics_topic_events *(needs ETL addition — see bottom)* |

## Notes

- All fields are accessed via dot notation: `data.field`
- Filter out test data with: `WHERE data.isTest = false`
- If boolean filter doesn't work, try: `WHERE data.isTest != 'true'`
- For timestamp parsing: `toDateTime(replace(substring(data.timestamp, 1, 19), 'T', ' '))`

---

## ENGAGEMENT

### 1. DAU over time

```sql
SELECT
  toDate(data.day) AS day,
  toInt32(data.dau) AS dau
FROM mongodb_analytics_daily_stats
ORDER BY day
```

Chart: Line chart — X: `day`, Y: `dau`

---

### 2. D1 / D3 / D7 Retention

% of users who returned on day N after their first activity day.

```sql
WITH user_active_days AS (
    SELECT
        data.userId AS user_id,
        data.day    AS active_day
    FROM mongodb_analytics_chat_events
    WHERE data.role = 'user'
      AND data.isTest = false
    GROUP BY user_id, active_day
),
user_first_day AS (
    SELECT user_id, MIN(active_day) AS first_day
    FROM user_active_days
    GROUP BY user_id
),
retention AS (
    SELECT
        uf.user_id,
        MAX(CASE WHEN ua.active_day = toString(addDays(toDate(uf.first_day), 1)) THEN 1 ELSE 0 END) AS d1,
        MAX(CASE WHEN ua.active_day = toString(addDays(toDate(uf.first_day), 3)) THEN 1 ELSE 0 END) AS d3,
        MAX(CASE WHEN ua.active_day = toString(addDays(toDate(uf.first_day), 7)) THEN 1 ELSE 0 END) AS d7
    FROM user_first_day uf
    LEFT JOIN user_active_days ua ON uf.user_id = ua.user_id
    GROUP BY uf.user_id
)
SELECT
    count(*)                                AS total_users,
    sum(d1)                                 AS d1_retained,
    round(sum(d1) / count(*) * 100, 1)      AS d1_pct,
    sum(d3)                                 AS d3_retained,
    round(sum(d3) / count(*) * 100, 1)      AS d3_pct,
    sum(d7)                                 AS d7_retained,
    round(sum(d7) / count(*) * 100, 1)      AS d7_pct
FROM retention
```

Chart: Table or single-value scorecards for `d1_pct`, `d3_pct`, `d7_pct`

---

### 3. Sessions per user (30-min inactivity gap)

Distribution of how many sessions each user had.

```sql
WITH msg_times AS (
    SELECT
        data.userId AS user_id,
        toDateTime(replace(substring(data.timestamp, 1, 19), 'T', ' ')) AS msg_ts
    FROM mongodb_analytics_chat_events
    WHERE data.isTest = false
      AND data.timestamp IS NOT NULL
),
with_prev AS (
    SELECT
        user_id,
        msg_ts,
        lagInFrame(msg_ts) OVER (PARTITION BY user_id ORDER BY msg_ts) AS prev_ts
    FROM msg_times
),
with_flag AS (
    SELECT
        user_id,
        msg_ts,
        CASE WHEN prev_ts IS NULL OR dateDiff('minute', prev_ts, msg_ts) > 30
             THEN 1 ELSE 0
        END AS is_new_session
    FROM with_prev
),
user_sessions AS (
    SELECT user_id, SUM(is_new_session) AS session_count
    FROM with_flag
    GROUP BY user_id
)
SELECT
    session_count,
    count(*) AS num_users
FROM user_sessions
GROUP BY session_count
ORDER BY session_count
```

Chart: Bar chart — X: `session_count`, Y: `num_users`

---

### 4. Session duration distribution

Time from first to last message within a session (30-min gap split).

```sql
WITH msg_times AS (
    SELECT
        data.userId AS user_id,
        toDateTime(replace(substring(data.timestamp, 1, 19), 'T', ' ')) AS msg_ts
    FROM mongodb_analytics_chat_events
    WHERE data.isTest = false
      AND data.timestamp IS NOT NULL
),
with_prev AS (
    SELECT
        user_id,
        msg_ts,
        lagInFrame(msg_ts) OVER (PARTITION BY user_id ORDER BY msg_ts) AS prev_ts
    FROM msg_times
),
with_flag AS (
    SELECT
        user_id,
        msg_ts,
        CASE WHEN prev_ts IS NULL OR dateDiff('minute', prev_ts, msg_ts) > 30
             THEN 1 ELSE 0
        END AS is_new_session
    FROM with_prev
),
with_session_id AS (
    SELECT
        user_id,
        msg_ts,
        sum(is_new_session) OVER (PARTITION BY user_id ORDER BY msg_ts) AS session_id
    FROM with_flag
),
sessions AS (
    SELECT
        user_id,
        session_id,
        dateDiff('minute', min(msg_ts), max(msg_ts)) AS duration_minutes
    FROM with_session_id
    GROUP BY user_id, session_id
)
SELECT
    multiIf(
        duration_minutes < 5,  '< 5 min',
        duration_minutes < 15, '5-15 min',
        duration_minutes < 30, '15-30 min',
        duration_minutes < 60, '30-60 min',
                               '> 60 min'
    ) AS duration_bucket,
    count(*) AS session_count
FROM sessions
GROUP BY duration_bucket
ORDER BY min(duration_minutes)
```

Chart: Bar chart — X: `duration_bucket`, Y: `session_count`

---

### 5. Active days per user

Distribution of unique calendar days each user was active.

```sql
SELECT
    active_days,
    count(*) AS user_count
FROM (
    SELECT
        data.userId              AS user_id,
        count(DISTINCT data.day) AS active_days
    FROM mongodb_analytics_chat_events
    WHERE data.isTest = false
      AND data.role = 'user'
    GROUP BY user_id
)
GROUP BY active_days
ORDER BY active_days
```

Chart: Bar chart — X: `active_days`, Y: `user_count`

---

### 6. Weekly activity heatmap

Number of distinct active users per day of the week, per week.

```sql
SELECT
    toMonday(toDate(data.day))    AS week_start,
    toDayOfWeek(toDate(data.day)) AS day_of_week,
    count(DISTINCT data.userId)   AS active_users
FROM mongodb_analytics_chat_events
WHERE data.isTest = false
  AND data.role = 'user'
GROUP BY week_start, day_of_week
ORDER BY week_start, day_of_week
```

Chart: Table — rows: `week_start`, columns: `day_of_week` (1=Mon … 7=Sun), values: `active_users`

---

## PROGRESSION FUNNEL

### 7. Score distribution (histogram)

Cumulative points earned per story.

```sql
SELECT
    multiIf(
        toInt32(data.score) < 25,  '0-24',
        toInt32(data.score) < 50,  '25-49',
        toInt32(data.score) < 75,  '50-74',
        toInt32(data.score) < 100, '75-99',
        toInt32(data.score) < 150, '100-149',
        toInt32(data.score) < 200, '150-199',
                                   '200+'
    ) AS score_bucket,
    count(*) AS story_count
FROM mongodb_analytics_user_snapshots
WHERE data.isTest = false
GROUP BY score_bucket
ORDER BY min(toInt32(data.score))
```

Chart: Bar chart — X: `score_bucket`, Y: `story_count`

---

### 8. Story status breakdown

Share of stories by current stage.

```sql
SELECT
    data.storyStatus                                     AS story_status,
    count(*)                                             AS count,
    round(count(*) / sum(count(*)) OVER () * 100, 1)    AS pct
FROM mongodb_analytics_user_snapshots
WHERE data.isTest = false
GROUP BY story_status
ORDER BY count DESC
```

Chart: Pie chart or bar chart — `story_status` vs `count`

---

### 9. Interactions by topic quality

Count of interactions per topic (story / character / location) broken down by quality label (poor / basic / strong).

> **Requires `analytics_topic_events` collection — see ETL Addition section below.**

```sql
SELECT
    data.topic   AS topic,
    data.quality AS quality,
    count(*)     AS interaction_count
FROM mongodb_analytics_topic_events
WHERE data.isTest = false
  AND data.eventType = 'interaction'
GROUP BY topic, quality
ORDER BY topic, quality
```

Chart: Grouped bar chart — X: `topic`, color: `quality`, Y: `interaction_count`

---

### 10. Average points by topic

Average points earned per interaction, grouped by topic.

> **Requires `analytics_topic_events` collection — see ETL Addition section below.**

```sql
SELECT
    data.topic                              AS topic,
    round(avg(toFloat64(data.gained)), 2)   AS avg_points,
    count(*)                                AS event_count
FROM mongodb_analytics_topic_events
WHERE data.isTest = false
  AND data.eventType = 'interaction'
GROUP BY topic
ORDER BY avg_points DESC
```

Chart: Bar chart — X: `topic`, Y: `avg_points`

---

### 11. Pitch quality score distribution

Distribution of points earned from the initial story pitch. Reflects strength of story concept.

> **Requires `analytics_topic_events` collection — see ETL Addition section below.**

```sql
SELECT
    toInt32(data.gained) AS pitch_points,
    count(*)             AS story_count
FROM mongodb_analytics_topic_events
WHERE data.isTest = false
  AND data.eventType = 'pitch'
GROUP BY pitch_points
ORDER BY pitch_points
```

Chart: Bar chart — X: `pitch_points`, Y: `story_count`

---

## ETL Addition: `analytics_topic_events`

Metrics 9, 10, and 11 require a new flat collection that exposes one row per `pointsEvent` from `miniStories.improvementState.pointsEvents`.

**Schema:**

| Field | Type | Description |
|---|---|---|
| `storyId` | string | Parent story ID |
| `userId` | string | User who owns the story |
| `eventType` | string | `pitch` or `interaction` |
| `topic` | string | `story`, `character`, or `location` |
| `quality` | string | `poor`, `basic`, or `strong` |
| `gained` | int | Points earned in this event |
| `totalAfter` | int | Cumulative score after this event |
| `createdAt` | datetime | When the event occurred |
| `day` | string | `YYYY-MM-DD` |
| `isTest` | bool | Inherited from parent story |

**To populate:** run `uv run python -m src.analytics_sync`, then connect the new collection in PostHog → Data pipeline → Sources.
