# PostHog SQL Queries — OTT Play Story Builder Dashboard

## Table names

| PostHog table | MongoDB collection |
|---|---|
| `mongodb_analytics_daily_stats` | analytics_daily_stats |
| `mongodb_analytics_user_snapshots` | analytics_user_snapshots |
| `mongodb_analytics_chat_events` | analytics_chat_events |
| `mongodb_analytics_topic_events` | analytics_topic_events *(needs ETL addition — see bottom)* |
| `mongodb_analytics_geval_scores` | analytics_geval_scores *(populate with run_geval.py)* |

## Notes

- All fields are accessed via dot notation: `data.field`
- Filter out test data with: `WHERE data.isTest = 'false'`
- Boolean fields from MongoDB arrive as strings in PostHog — always use `data.isTest = 'false'` (with quotes)
- For timestamp parsing: use `toDateTime(data.timestampStr)` — the ETL stores a pre-formatted `YYYY-MM-DD HH:MM:SS` string

---

## ENGAGEMENT

### 1. DAU over time

```sql
SELECT
  toDate(data.day) AS day,
  toInt(data.dau) AS dau
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
      AND data.isTest = 'false'
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
        toDateTime(data.timestampStr) AS msg_ts
    FROM mongodb_analytics_chat_events
    WHERE data.isTest = 'false'
      AND data.timestampStr IS NOT NULL
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
        toDateTime(data.timestampStr) AS msg_ts
    FROM mongodb_analytics_chat_events
    WHERE data.isTest = 'false'
      AND data.timestampStr IS NOT NULL
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
    WHERE data.isTest = 'false'
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
    toMonday(toDate(data.day)) AS week_start,
    multiIf(
        toDayOfWeek(toDate(data.day)) = 1, '1-Mon',
        toDayOfWeek(toDate(data.day)) = 2, '2-Tue',
        toDayOfWeek(toDate(data.day)) = 3, '3-Wed',
        toDayOfWeek(toDate(data.day)) = 4, '4-Thu',
        toDayOfWeek(toDate(data.day)) = 5, '5-Fri',
        toDayOfWeek(toDate(data.day)) = 6, '6-Sat',
                                           '7-Sun'
    ) AS day_of_week,
    count(DISTINCT data.userId) AS active_users
FROM mongodb_analytics_chat_events
WHERE data.isTest = 'false'
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
        toInt(data.score) < 25,  '0-24',
        toInt(data.score) < 50,  '25-49',
        toInt(data.score) < 75,  '50-74',
        toInt(data.score) < 100, '75-99',
        toInt(data.score) < 150, '100-149',
        toInt(data.score) < 200, '150-199',
                                   '200+'
    ) AS score_bucket,
    count(*) AS story_count
FROM mongodb_analytics_user_snapshots
WHERE data.isTest = 'false'
GROUP BY score_bucket
ORDER BY min(toInt(data.score))
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
WHERE data.isTest = 'false'
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
WHERE data.isTest = 'false'
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
    round(avg(toFloat(data.gained)), 2)   AS avg_points,
    count(*)                                AS event_count
FROM mongodb_analytics_topic_events
WHERE data.isTest = 'false'
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
    toInt(data.gained) AS pitch_points,
    count(*)             AS story_count
FROM mongodb_analytics_topic_events
WHERE data.isTest = 'false'
  AND data.eventType = 'pitch'
GROUP BY pitch_points
ORDER BY pitch_points
```

Chart: Bar chart — X: `pitch_points`, Y: `story_count`

---

## GURU CHAT PERFORMANCE

### 12. MCQ vs open-ended response rate

Share of user responses that were MCQ picks vs free-text.

```sql
SELECT
    data.msgClass AS msg_class,
    count(*) AS count,
    round(count(*) / sum(count(*)) OVER () * 100, 1) AS pct
FROM mongodb_analytics_chat_events
WHERE data.isTest = 'false'
  AND data.role = 'user'
GROUP BY msg_class
ORDER BY count DESC
```

Chart: Pie or bar — `msg_class` vs `count`

---

### 12b. MCQ vs open-ended vs "Other" response rate

Same as 12, but splits `mcq_response` into two: users who picked a preset option vs users who tapped "Other" and typed their own answer. The "Other" tap is a dissatisfaction signal — the Guru's options didn't cover what the user wanted to say.

```sql
SELECT
    multiIf(
        data.isRegenLoop = 'true',      'mcq_response_other',
        data.msgClass = 'mcq_response', 'mcq_response',
        data.msgClass = 'open_ended',   'open_ended',
        data.msgClass
    ) AS msg_class,
    count(*)                                             AS count,
    round(count(*) / sum(count(*)) OVER () * 100, 1)    AS pct
FROM mongodb_analytics_chat_events
WHERE data.isTest = 'false'
  AND data.role = 'user'
GROUP BY msg_class
ORDER BY count DESC
```

Chart: Pie or bar — `msg_class` vs `count`. A high `mcq_response_other` share means users frequently reject the Guru's preset options.

---

### 13. Instruction lag distribution

How quickly users respond after the Guru sends a message (seconds).

```sql
SELECT
    lag_bucket,
    ROUND(100 * response_count / SUM(response_count) OVER (), 2) AS pct
FROM (
    SELECT
        multiIf(
            toFloat(data.instructionLagSeconds) < 10,  '< 10s',
            toFloat(data.instructionLagSeconds) < 30,  '10-30s',
            toFloat(data.instructionLagSeconds) < 60,  '30-60s',
            toFloat(data.instructionLagSeconds) < 300, '1-5 min',
                                                       '> 5 min'
        ) AS lag_bucket,
        multiIf(
            toFloat(data.instructionLagSeconds) < 10,  1,
            toFloat(data.instructionLagSeconds) < 30,  2,
            toFloat(data.instructionLagSeconds) < 60,  3,
            toFloat(data.instructionLagSeconds) < 300, 4,
                                                       5
        ) AS sort_order,
        count(*) AS response_count
    FROM mongodb_analytics_chat_events
    WHERE data.isTest = 'false'
      AND data.role = 'user'
      AND data.instructionLagSeconds IS NOT NULL
    GROUP BY lag_bucket, sort_order
)
ORDER BY sort_order

```

Chart: Bar chart — `lag_bucket` vs `response_count`

---

### 14. Token usage by day

Daily input / output / cached token totals.

```sql
SELECT
    toDate(data.day)                         AS day,
    sum(toInt(data.totalInputTokens))      AS input_tokens,
    sum(toInt(data.totalOutputTokens))     AS output_tokens,
    sum(toInt(data.totalCachedTokens))     AS cached_tokens
FROM mongodb_analytics_daily_stats
GROUP BY day
ORDER BY day
```

Chart: Stacked bar or multi-line — X: `day`, series: token types

---

### 15. "Other" selections per story

How many times users skipped the Guru's MCQ options and typed their own answer instead.

```sql
SELECT
    regen_bucket,
    story_count,
    ROUND(100 * story_count / SUM(story_count) OVER (), 2) AS pct
FROM (
    SELECT
        multiIf(
            toInt(data.regenLoops) = 0, '0',
            toInt(data.regenLoops) = 1, '1',
            toInt(data.regenLoops) < 5, '2-4',
                                        '5+'
        ) AS regen_bucket,
        multiIf(
            toInt(data.regenLoops) = 0, 1,
            toInt(data.regenLoops) = 1, 2,
            toInt(data.regenLoops) < 5, 3,
                                        4
        ) AS sort_order,
        count(*) AS story_count
    FROM mongodb_analytics_user_snapshots
    WHERE data.isTest = 'false'
    GROUP BY regen_bucket, sort_order
)
ORDER BY sort_order
```

Chart: Bar chart — `regen_bucket` vs `story_count`

**What this shows:** Each row in `analytics_user_snapshots` is one story. `regenLoops` counts how many times the user chose "Other — type your own answer" instead of picking one of the Guru's MCQ options — a signal they didn't like what was offered. The query buckets stories by how many times that happened. High counts in the `2-4` or `5+` buckets suggest the Guru's MCQ options are consistently missing what users actually want to say.

---

### 16. Topic diversity entropy

How evenly users spread across story / character / location topics. Higher = more balanced.

```sql
SELECT
    multiIf(
        toFloat(data.topicEntropy) = 0,   'Single topic',
        toFloat(data.topicEntropy) < 0.5, 'Low diversity',
        toFloat(data.topicEntropy) < 1.0, 'Medium diversity',
                                             'High diversity'
    ) AS entropy_bucket,
    count(*) AS story_count
FROM mongodb_analytics_user_snapshots
WHERE data.isTest = 'false'
GROUP BY entropy_bucket
ORDER BY min(toFloat(data.topicEntropy))
```

Chart: Bar chart — `entropy_bucket` vs `story_count`

---

### 17. Distribution of total messages per story 

How long are conversations? Shows if most users have short or deep interactions.

```sql
SELECT
    multiIf(
        toInt(data.totalMessages) < 5,  '1-4',
        toInt(data.totalMessages) < 10, '5-9',
        toInt(data.totalMessages) < 20, '10-19',
        toInt(data.totalMessages) < 50, '20-49',
                                        '50+'
    ) AS msg_bucket,
    count(*) AS story_count,
    ROUND(100 * count(*) / SUM(count(*)) OVER (), 1) AS pct
FROM mongodb_analytics_user_snapshots
WHERE data.isTest = 'false'
GROUP BY msg_bucket
ORDER BY min(toInt(data.totalMessages))
```

Chart: Bar chart — `msg_bucket` vs `story_count`

---

### 18. MCQ usage rate by level

Do users rely more or less on MCQ options as they progress? Signals whether the Guru adapts well across levels.

```sql
SELECT
    toInt(data.level) AS level,
    sum(toInt(data.mcqResponses))  AS mcq_responses,
    sum(toInt(data.userMessages))  AS total_user_msgs,
    round(
        100 * sum(toInt(data.mcqResponses)) / sum(toInt(data.userMessages)),
        1
    ) AS mcq_rate_pct,
    count(*) AS story_count
FROM mongodb_analytics_user_snapshots
WHERE data.isTest = 'false'
  AND toInt(data.userMessages) > 0
GROUP BY level
ORDER BY level
```

Chart: Bar chart — X: `level`, Y: `mcq_rate_pct`

---

### 19. Level-up time by transition

Wall-clock minutes between a story's first points event and the moment it crossed each 100-point threshold (level 2 = 100 pts, level 3 = 200, level 4 = 300). If one transition takes 10× longer than the others, that's your progression wall. Note: this measures real elapsed time, not active time — a user who pauses mid-session inflates the number.

> **Requires `analytics_topic_events`** — uses `totalAfter` to detect when score crossed each 100-point threshold.

```sql
WITH level_times AS (
    SELECT
        data.storyId AS story_id,
        min(toDateTime(data.createdAt))                                          AS first_event_at,
        minIf(toDateTime(data.createdAt), toInt(data.totalAfter) >= 100)         AS reached_l2_at,
        minIf(toDateTime(data.createdAt), toInt(data.totalAfter) >= 200)         AS reached_l3_at,
        minIf(toDateTime(data.createdAt), toInt(data.totalAfter) >= 300)         AS reached_l4_at
    FROM mongodb_analytics_topic_events
    WHERE data.isTest = 'false'
    GROUP BY story_id
)
SELECT
    round(avg(dateDiff('minute', first_event_at, reached_l2_at)), 0)  AS avg_min_to_level2,
    count(reached_l2_at)                                               AS stories_reached_l2,
    round(avg(dateDiff('minute', reached_l2_at, reached_l3_at)), 0)   AS avg_min_level2_to_3,
    count(reached_l3_at)                                               AS stories_reached_l3,
    round(avg(dateDiff('minute', reached_l3_at, reached_l4_at)), 0)   AS avg_min_level3_to_4,
    count(reached_l4_at)                                               AS stories_reached_l4
FROM level_times
WHERE first_event_at IS NOT NULL
```

Chart: Table or horizontal bar — one row per transition, showing avg time + how many stories made it

---

### 20. Level-up active time by transition

Median active minutes to complete each level transition. Only time between consecutive interactions within the same session is counted — idle gaps over 30 min are excluded, so a user who left and came back the next day doesn't inflate the number. Level thresholds: level 2 = 100 pts, level 3 = 200, level 4 = 300. If one transition takes 10× longer than the others, that's your progression wall.

> **Requires `analytics_topic_events`**

```sql
WITH story_events AS (
    SELECT
        data.storyId AS story_id,
        toDateTime(data.createdAt) AS evt_ts,
        toInt(data.totalAfter) AS total_after
    FROM mongodb_analytics_topic_events
    WHERE data.isTest = 'false'
),
with_gap AS (
    SELECT
        story_id,
        evt_ts,
        total_after,
        multiIf(
            lagInFrame(evt_ts) OVER (PARTITION BY story_id ORDER BY evt_ts) IS NULL, 0,
            dateDiff('minute', lagInFrame(evt_ts) OVER (PARTITION BY story_id ORDER BY evt_ts), evt_ts) > 30, 0,
            dateDiff('minute', lagInFrame(evt_ts) OVER (PARTITION BY story_id ORDER BY evt_ts), evt_ts)
        ) AS active_gap
    FROM story_events
),
cumulative AS (
    SELECT
        story_id,
        total_after,
        sum(active_gap) OVER (PARTITION BY story_id ORDER BY evt_ts) AS cum_active_min
    FROM with_gap
),
level_times AS (
    SELECT
        story_id,
        minIf(cum_active_min, total_after >= 100) AS active_min_to_l2,
        minIf(cum_active_min, total_after >= 200) AS active_min_to_l3,
        minIf(cum_active_min, total_after >= 300) AS active_min_to_l4
    FROM cumulative
    GROUP BY story_id
),
agg AS (
    SELECT
        round(median(active_min_to_l2), 0)                    AS med_l1_l2,
        round(median(active_min_to_l3 - active_min_to_l2), 0) AS med_l2_l3,
        round(median(active_min_to_l4 - active_min_to_l3), 0) AS med_l3_l4
    FROM level_times
),
ordered AS (
    SELECT '1. L1 → L2' AS transition, med_l1_l2 AS median_active_min, 1 AS sort_order FROM agg
    UNION ALL
    SELECT '2. L2 → L3', med_l2_l3, 2 FROM agg
    UNION ALL
    SELECT '3. L3 → L4', med_l3_l4, 3 FROM agg
)
SELECT * FROM ordered ORDER BY 3 ASC
```

Chart: Table or horizontal bar — one row per transition, showing median active time + how many stories made it

---

### 21. Score distribution by story status

How far along are stories at each stage? Compares the score profile of in-progress (`wip`) vs completed (`submitted`) stories — high scores in `wip` mean users are close to finishing but haven't submitted yet.

```sql
SELECT
    multiIf(
        story_status = 'submitted', '2. submitted',
        story_status = 'wip',       '1. wip',
                                    story_status
    ) AS story_status,
    score_bucket,
    story_count,
    percent_story
FROM (
    SELECT
        data.storyStatus AS story_status,
        multiIf(
            toInt(data.score) < 25,  '1. 0-24',
            toInt(data.score) < 50,  '2. 25-49',
            toInt(data.score) < 75,  '3. 50-74',
            toInt(data.score) < 100, '4. 75-99',
            toInt(data.score) < 150, '5. 100-149',
            toInt(data.score) < 200, '6. 150-199',
                                     '7. 200+'
        ) AS score_bucket,
        count(*) AS story_count,
        ROUND(100 * count(*) / SUM(count(*)) OVER (PARTITION BY data.storyStatus), 1) AS percent_story
    FROM mongodb_analytics_user_snapshots
    WHERE data.isTest = 'false'
      AND data.storyStatus != 'not_started'
    GROUP BY story_status, score_bucket
)
ORDER BY story_status, score_bucket
```

Chart: Grouped bar — X: `score_bucket`, color: `story_status`, Y: `percent_story`

---

### 22. Free-text response word count distribution

How much are users actually saying when they type freely? Filters to open-ended responses only (excludes MCQ picks). A distribution skewed toward 1-3 words means users are giving minimal answers; 8+ words means they're engaged and building the story with detail.

```sql
SELECT
    multiIf(
        length(splitByChar(' ', trim(data.content))) <= 3,  '1. 1-3 words',
        length(splitByChar(' ', trim(data.content))) <= 7,  '2. 4-7 words',
        length(splitByChar(' ', trim(data.content))) <= 15, '3. 8-15 words',
        length(splitByChar(' ', trim(data.content))) <= 30, '4. 16-30 words',
                                                             '5. 30+ words'
    ) AS word_count_bucket,
    count(*) AS response_count,
    ROUND(100 * count(*) / SUM(count(*)) OVER (), 1) AS pct
FROM mongodb_analytics_chat_events
WHERE data.isTest = 'false'
  AND data.role = 'user'
  AND data.msgClass = 'open_ended'
  AND data.content IS NOT NULL
GROUP BY word_count_bucket
ORDER BY word_count_bucket
```

Chart: Bar chart — X: `word_count_bucket`, Y: `pct`

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

---

## GURU QUALITY — GEval Metrics

Source: `mongodb_analytics_geval_scores` (one row per story)

All scores are **0–1 scale** (deepeval normalises from 0–10 internally):
- `0.0` = best (no frustration / no hallucination / simple language)
- `1.0` = worst (max frustration / strong hallucination / very complex language)

> **Requires `analytics_geval_scores`** — populate with `uv run python scripts/run_geval.py`, then add the collection in PostHog → Data pipeline → Sources.

---

### 23. User frustration distribution

How frustrated are users across stories? Scores near 0 = calm, near 1 = high frustration.

```sql
SELECT
    multiIf(
        toFloat(data.frustration.score) < 0.2, '😌 Low (0–0.2)',
        toFloat(data.frustration.score) < 0.4, '😐 Mild (0.2–0.4)',
        toFloat(data.frustration.score) < 0.6, '😕 Moderate (0.4–0.6)',
        toFloat(data.frustration.score) < 0.8, '😠 High (0.6–0.8)',
                                                  '🤬 Very high (0.8–1.0)'
    ) AS frustration_bucket,
    multiIf(
        toFloat(data.frustration.score) < 0.2, 1,
        toFloat(data.frustration.score) < 0.4, 2,
        toFloat(data.frustration.score) < 0.6, 3,
        toFloat(data.frustration.score) < 0.8, 4,
                                                  5
    ) AS sort_order,
    count(*) AS story_count,
    round(100 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM mongodb_analytics_geval_scores
WHERE data.isTest = 'false'
  AND data.frustration.score IS NOT NULL
GROUP BY frustration_bucket, sort_order
ORDER BY sort_order
```

Chart: Bar chart — X: `frustration_bucket`, Y: `story_count`

---

### 24. GEval quality metrics over time

Track all three key Guru quality signals together — are frustration, hallucination, and language complexity trending up or down as we ship new versions?

```sql
SELECT
    toDate(data.evaluatedAt)                                          AS eval_date,
    round(avg(toFloat(data.frustration.score)), 3)                  AS avg_frustration,
    round(avg(toFloat(data.storyHallucination.score)), 3)           AS avg_hallucination,
    round(avg(toFloat(data.languageComplexity.avg_score)), 3)       AS avg_language_complexity,
    count(*)                                                           AS stories_evaluated
FROM mongodb_analytics_geval_scores
WHERE data.isTest = 'false'
  AND data.frustration.score IS NOT NULL
GROUP BY eval_date
ORDER BY eval_date
```

Chart: Multi-line chart — X: `eval_date`, series: `avg_frustration`, `avg_hallucination`, `avg_language_complexity`

**What to watch:** All three should trend down over time. A spike after a deploy = that version made things worse. If hallucination goes up while frustration stays flat, Guru is inventing story facts but users haven't noticed yet.

---

### 25. Language complexity distribution

How complex is Guru's language across stories? Ideal: most stories in the 0.2–0.5 range (clear but not dumbed down).

```sql
SELECT
    multiIf(
        toFloat(data.languageComplexity.avg_score) < 0.2, '1. Very simple (0–0.2)',
        toFloat(data.languageComplexity.avg_score) < 0.4, '2. Simple (0.2–0.4)',
        toFloat(data.languageComplexity.avg_score) < 0.6, '3. Moderate (0.4–0.6)',
        toFloat(data.languageComplexity.avg_score) < 0.8, '4. Complex (0.6–0.8)',
                                                             '5. Very complex (0.8–1.0)'
    ) AS complexity_bucket,
    count(*) AS story_count,
    round(100 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM mongodb_analytics_geval_scores
WHERE data.isTest = 'false'
  AND data.languageComplexity.avg_score IS NOT NULL
GROUP BY complexity_bucket
ORDER BY complexity_bucket
```

Chart: Bar chart — X: `complexity_bucket`, Y: `story_count`

---

### 26. Story hallucination distribution

How often does Guru introduce facts that contradict or aren't supported by the story context?

```sql
SELECT
    multiIf(
        toFloat(data.storyHallucination.score) < 0.2, '✅ None (0–0.2)',
        toFloat(data.storyHallucination.score) < 0.4, '🟡 Minor (0.2–0.4)',
        toFloat(data.storyHallucination.score) < 0.6, '🟠 Moderate (0.4–0.6)',
        toFloat(data.storyHallucination.score) < 0.8, '🔴 High (0.6–0.8)',
                                                         '🚨 Severe (0.8–1.0)'
    ) AS hallucination_bucket,
    multiIf(
        toFloat(data.storyHallucination.score) < 0.2, 1,
        toFloat(data.storyHallucination.score) < 0.4, 2,
        toFloat(data.storyHallucination.score) < 0.6, 3,
        toFloat(data.storyHallucination.score) < 0.8, 4,
                                                         5
    ) AS sort_order,
    count(*) AS story_count,
    round(100 * count(*) / sum(count(*)) OVER (), 1) AS pct
FROM mongodb_analytics_geval_scores
WHERE data.isTest = 'false'
  AND data.storyHallucination.score IS NOT NULL
GROUP BY hallucination_bucket, sort_order
ORDER BY sort_order
```

Chart: Bar chart — X: `hallucination_bucket`, Y: `story_count`

---

### 27. GEval quality summary (single-row scorecard)

One-row overview of all 5 metrics — useful as a dashboard headline card.

```sql
SELECT
    round(avg(toFloat(data.frustration.score)), 3)              AS avg_frustration,
    round(avg(toFloat(data.languageComplexity.avg_score)), 3)   AS avg_language_complexity,
    round(avg(toFloat(data.storyHallucination.score)), 3)       AS avg_hallucination,
    round(avg(toFloat(data.detailFixation.score)), 3)           AS avg_detail_fixation,
    round(avg(toFloat(data.offTopic.avg_score)), 3)             AS avg_offtopic,
    count(*)                                                       AS stories_evaluated
FROM mongodb_analytics_geval_scores
WHERE data.isTest = 'false'
```

Chart: Table / stat tiles — one value per column

---

### 28. Frustration vs language complexity

Do complex Guru responses correlate with more frustrated users?

**Option A — Raw table (export to Google Sheets for scatter plot)**

Run this in PostHog → copy results → paste into Google Sheets → Insert → Chart → Scatter.

```sql
SELECT
    data.storyId                                           AS story_id,
    round(toFloat(data.frustration.score), 2)             AS frustration,
    round(toFloat(data.languageComplexity.avg_score), 2)  AS language_complexity,
    toInt(data.userMessageCount)                          AS user_messages
FROM mongodb_analytics_geval_scores
WHERE data.isTest = 'false'
  AND data.frustration.score IS NOT NULL
  AND data.languageComplexity.avg_score IS NOT NULL
ORDER BY frustration DESC
```

**Option B — 2D heatmap grid (PostHog table)**

Groups stories into a frustration × complexity matrix. Shows which combinations are most common — e.g., if most stories land in "high complexity + high frustration" that's the smoking gun.

```sql
SELECT
    multiIf(
        toFloat(data.frustration.score) < 0.3, 'frustration: low',
        toFloat(data.frustration.score) < 0.6, 'frustration: mid',
                                                'frustration: high'
    ) AS frustration_band,
    multiIf(
        toFloat(data.languageComplexity.avg_score) < 0.3, 'complexity: low',
        toFloat(data.languageComplexity.avg_score) < 0.6, 'complexity: mid',
                                                           'complexity: high'
    ) AS complexity_band,
    count(*)                                              AS story_count,
    round(100 * count(*) / sum(count(*)) OVER (), 1)     AS pct
FROM mongodb_analytics_geval_scores
WHERE data.isTest = 'false'
  AND data.frustration.score IS NOT NULL
  AND data.languageComplexity.avg_score IS NOT NULL
GROUP BY frustration_band, complexity_band
ORDER BY frustration_band, complexity_band
```

Chart: Table — read it as a 3×3 grid. The cell with the highest `story_count` is where most conversations land.

---

### 29. MCQ ratio distribution (passive vs active engagement histogram)

**How passive or active are users?** Groups stories by the share of responses that were MCQ taps (vs typed free-text). A bimodal distribution — spikes near 0–10% and 90–100% — reveals two distinct user types: one that always taps MCQ options and one that always types. A flat distribution means engagement is mixed across users.

Source: `mongodb_analytics_user_snapshots`. Only stories with at least one user message are included.

```sql
SELECT
    multiIf(
        toFloat(data.mcqResponses) / toFloat(data.userMessages) < 0.1, '0–10%',
        toFloat(data.mcqResponses) / toFloat(data.userMessages) < 0.2, '10–20%',
        toFloat(data.mcqResponses) / toFloat(data.userMessages) < 0.3, '20–30%',
        toFloat(data.mcqResponses) / toFloat(data.userMessages) < 0.4, '30–40%',
        toFloat(data.mcqResponses) / toFloat(data.userMessages) < 0.5, '40–50%',
        toFloat(data.mcqResponses) / toFloat(data.userMessages) < 0.6, '50–60%',
        toFloat(data.mcqResponses) / toFloat(data.userMessages) < 0.7, '60–70%',
        toFloat(data.mcqResponses) / toFloat(data.userMessages) < 0.8, '70–80%',
        toFloat(data.mcqResponses) / toFloat(data.userMessages) < 0.9, '80–90%',
        '90–100%'
    )                                                        AS mcq_ratio_bucket,
    count(*)                                                 AS story_count,
    round(100 * count(*) / sum(count(*)) OVER (), 1)        AS pct
FROM mongodb_analytics_user_snapshots
WHERE data.isTest = 'false'
  AND toFloat(data.userMessages) > 0
GROUP BY mcq_ratio_bucket
ORDER BY mcq_ratio_bucket
```

Chart: Bar chart — X: `mcq_ratio_bucket`, Y: `story_count`. Look for spikes at the extremes (bimodal = two clear user types) or a bell curve around 50% (mostly mixed engagement).
