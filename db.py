"""SQLite raw-data tier for activity analysis.

This module is the single source of truth for the raw activity data layer
(see `cache/garmin.db`).

Design notes:
- One DB per DATA_DIR (matches the per-user instance pattern).
- Hand-rolled migrations via a `schema_version` table — no ORM, no Alembic.
- Connections are short-lived (open per call, close on exit) to avoid
  leaking handles across requests.
- All time-series writes use bulk `executemany` inside a single transaction.
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Iterator

DATA_DIR = os.environ.get("DATA_DIR",
                          os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(DATA_DIR, "cache")
DB_PATH   = os.path.join(CACHE_DIR, "garmin.db")

# ── Schema migrations ─────────────────────────────────────────────────────────
# Append a new function to MIGRATIONS to evolve the schema. _apply_migrations
# runs every connection-open; only un-applied versions execute.

_MIGRATIONS: list[tuple[int, str]] = [
    (1, """
        CREATE TABLE activities (
          activity_id           INTEGER PRIMARY KEY,
          activity_name         TEXT,
          activity_type_key     TEXT,
          start_time_local      TEXT,
          start_time_gmt        TEXT,
          distance_m            REAL,
          duration_s            REAL,
          moving_duration_s     REAL,
          elapsed_duration_s    REAL,
          average_hr            REAL,
          max_hr                REAL,
          min_hr                REAL,
          average_speed_mps     REAL,
          max_speed_mps         REAL,
          elevation_gain_m      REAL,
          elevation_loss_m      REAL,
          calories              REAL,
          training_load         REAL,
          aerobic_te            REAL,
          anaerobic_te          REAL,
          te_label              TEXT,
          vo2max                REAL,
          raw_summary_json      TEXT,
          has_full_detail       INTEGER NOT NULL DEFAULT 0,
          fetched_at            TEXT
        );
        CREATE INDEX idx_activities_time ON activities(start_time_local DESC);

        CREATE TABLE activity_metrics (
          activity_id      INTEGER NOT NULL,
          sec_offset       INTEGER NOT NULL,
          ts_ms            INTEGER,
          hr               REAL,
          speed_mps        REAL,
          elevation_m      REAL,
          cadence_spm      REAL,
          power_w          REAL,
          lat              REAL,
          lon              REAL,
          distance_cum_m   REAL,
          stride_cm        REAL,
          gct_ms           REAL,
          vert_osc_cm      REAL,
          vert_ratio       REAL,
          air_temp_c       REAL,
          grade_adj_speed  REAL,
          performance_cond REAL,
          available_stamina REAL,
          potential_stamina REAL,
          impact_load      REAL,
          vert_speed_mps   REAL,
          PRIMARY KEY (activity_id, sec_offset),
          FOREIGN KEY (activity_id) REFERENCES activities(activity_id) ON DELETE CASCADE
        );

        CREATE TABLE activity_gps (
          activity_id  INTEGER NOT NULL,
          point_index  INTEGER NOT NULL,
          ts_ms        INTEGER,
          lat          REAL,
          lon          REAL,
          PRIMARY KEY (activity_id, point_index),
          FOREIGN KEY (activity_id) REFERENCES activities(activity_id) ON DELETE CASCADE
        );

        CREATE TABLE activity_laps (
          activity_id        INTEGER NOT NULL,
          lap_index          INTEGER NOT NULL,
          intensity_type     TEXT,
          distance_m         REAL,
          duration_s         REAL,
          moving_duration_s  REAL,
          average_hr         REAL,
          max_hr             REAL,
          average_speed_mps  REAL,
          cadence_spm        REAL,
          gct_ms             REAL,
          power_w            REAL,
          elevation_gain_m   REAL,
          elevation_loss_m   REAL,
          raw_lap_json       TEXT,
          PRIMARY KEY (activity_id, lap_index)
        );

        CREATE TABLE activity_splits (
          activity_id        INTEGER NOT NULL,
          split_type         TEXT NOT NULL,
          no_of_splits       INTEGER,
          distance_m         REAL,
          duration_s         REAL,
          average_hr         REAL,
          average_speed_mps  REAL,
          raw_split_json     TEXT,
          PRIMARY KEY (activity_id, split_type)
        );

        CREATE TABLE activity_hr_zones (
          activity_id       INTEGER NOT NULL,
          zone_number       INTEGER NOT NULL,
          secs_in_zone      REAL,
          zone_low_boundary INTEGER,
          PRIMARY KEY (activity_id, zone_number)
        );

        CREATE TABLE activity_power_zones (
          activity_id       INTEGER NOT NULL,
          zone_number       INTEGER NOT NULL,
          secs_in_zone      REAL,
          zone_low_boundary INTEGER,
          PRIMARY KEY (activity_id, zone_number)
        );

        CREATE TABLE activity_weather (
          activity_id      INTEGER PRIMARY KEY,
          temp_c           REAL,
          apparent_temp_c  REAL,
          humidity_pct     REAL,
          wind_speed_mps   REAL,
          wind_direction   TEXT,
          precip_type      TEXT,
          weather_desc     TEXT,
          raw_weather_json TEXT,
          fetched_at       TEXT
        );
    """),
    (2, """
        -- Cycling streams expose `directBikeCadence` (rpm), distinct from
        -- running's `directDoubleCadence` (spm). Separate column avoids
        -- builders needing to disambiguate by activity type.
        ALTER TABLE activity_metrics ADD COLUMN cadence_bike_rpm REAL;
    """),
    (3, """
        -- Discovery log: every metric-stream descriptor key we encounter that
        -- we don't yet map (excluding connectIQDeveloperField-* which are
        -- inherently user-specific 3rd-party data). Lets us decide later
        -- which channels are worth promoting to columns. Activities are
        -- immutable on Garmin, so we can always re-fetch to backfill.
        CREATE TABLE activity_unknown_channels (
          activity_id   INTEGER NOT NULL,
          channel_key   TEXT NOT NULL,
          sample_value  TEXT,           -- first non-None value, str-formatted
          PRIMARY KEY (activity_id, channel_key)
        );
    """),
    (4, """
        -- Builder output cache. The context_md is what gets fed to the coach
        -- LLM as a user-message — NOT the LLM's response. (The response lives
        -- in review_chat as msg_index=0.)
        --
        -- builder_version_hash lets us detect "the builder code changed since
        -- this cache was written" → UI can prompt "regenerate?" rather than
        -- silently using stale context. tag_at_generation lets us detect
        -- "user re-tagged this activity" → invalidate.
        CREATE TABLE activity_review_context (
          activity_id              INTEGER PRIMARY KEY,
          tag_at_generation        TEXT,
          builder_name             TEXT,
          builder_version_hash     TEXT,
          context_md               TEXT,
          highlight_windows_json   TEXT,
          generated_at             TEXT
        );
    """),
    (5, """
        -- ── User-state tier ────────────────────────────────────────────────
        -- Naming convention:
        --   user_*  = user-typed/owned, NOT regenerable from Garmin sync
        --   chat_*  = multi-turn AI conversation logs, also user-owned
        -- Both prefixes are preserved on disconnect/reset (only activity_*
        -- tables get wiped), so user-typed data survives a fresh re-sync.
        --
        -- Activity-linked tables (user_activity_tags, user_activity_comments,
        -- chat_review*) reference activity_id but DO NOT use a FK constraint
        -- to activities(activity_id). After a disconnect+resync, Garmin
        -- typically re-issues the same activity IDs → tags/comments/chats
        -- auto-reconnect. If an ID changes (rare), the row becomes orphan
        -- but harmless — JOINs against activities just exclude it.

        -- Singleton key/value: phase, personal_note, onboarded,
        -- chat_overall_summary, chat_overall_summary_through_idx,
        -- chat_overall_updated_at — anything that's one-of-a-kind
        CREATE TABLE user_app_config (
          key         TEXT PRIMARY KEY,
          value       TEXT,
          updated_at  TEXT
        );

        -- Race entries (replaces user_config.json["races"])
        CREATE TABLE user_races (
          race_id     INTEGER PRIMARY KEY AUTOINCREMENT,
          name        TEXT NOT NULL,
          date        TEXT,            -- ISO YYYY-MM-DD
          distance_km REAL,
          terrain     TEXT,
          goal_time   TEXT,
          notes       TEXT,
          added_at    TEXT
        );
        CREATE INDEX idx_user_races_date ON user_races(date);

        -- Pinned long-term insights (replaces user_config.json["coach_insights"])
        CREATE TABLE user_coach_insights (
          insight_id  INTEGER PRIMARY KEY AUTOINCREMENT,
          text        TEXT NOT NULL,
          saved_at    TEXT,
          source      TEXT             -- 'overall' | 'review_<aid>' | 'manual'
        );

        -- Per-activity tag (replaces user_config.json["activity_tags"])
        CREATE TABLE user_activity_tags (
          activity_id  INTEGER PRIMARY KEY,
          tag          TEXT NOT NULL,
          tagged_at    TEXT
        );

        -- Per-activity comment (replaces user_config.json["activity_comments"])
        CREATE TABLE user_activity_comments (
          activity_id  INTEGER PRIMARY KEY,
          comment      TEXT,
          updated_at   TEXT
        );

        -- AI-generated overall coaching report (singleton — only the latest
        -- is kept; replaces garmin_data.json["coaching_report"])
        CREATE TABLE user_coaching_report (
          id            INTEGER PRIMARY KEY CHECK (id=1),
          content       TEXT NOT NULL,
          horizon       TEXT,
          generated_at  TEXT
        );

        -- Overall coach chat thread (replaces garmin_data.json["overall_chat"])
        -- msg_index is contiguous 0..N so order is preserved without ts parsing
        CREATE TABLE chat_overall (
          msg_index INTEGER PRIMARY KEY,
          role      TEXT NOT NULL,        -- 'user'|'assistant'|'system'|'tool'
          content   TEXT NOT NULL,
          model     TEXT,                 -- which LLM produced (assistant/tool only)
          ts        TEXT
        );

        -- Per-activity review chat (replaces activities/<aid>.json["review_chat"])
        -- msg_index 0 is the first assistant message (= the coach's review report)
        CREATE TABLE chat_review (
          activity_id INTEGER NOT NULL,
          msg_index   INTEGER NOT NULL,
          role        TEXT NOT NULL,
          content     TEXT NOT NULL,
          model       TEXT,
          ts          TEXT,
          PRIMARY KEY (activity_id, msg_index)
        );

        -- Per-activity rolling summary state (covers messages older than
        -- the last RECENT_N kept verbatim). Separate from chat_review so
        -- thread + summary can be loaded/persisted independently.
        CREATE TABLE chat_review_meta (
          activity_id          INTEGER PRIMARY KEY,
          summary              TEXT,
          summary_through_idx  INTEGER NOT NULL DEFAULT 0,
          updated_at           TEXT
        );
    """),
    (6, """
        -- ── Wellness tier (Garmin-derived, regenerable) ─────────────────────
        -- daily_summaries / sleep / hrv / training_status — single source of
        -- truth. One row per calendar date (UPSERT by calendar_date). Each
        -- table keeps a raw_json column for fields we don't column-explode
        -- (so downstream code that needs uncommon fields can still parse).
        -- Part of the `activity_*` lifecycle (regenerable from Garmin), so a
        -- reset wipes them; user_*/chat_* stay.

        CREATE TABLE daily_summary (
          calendar_date              TEXT PRIMARY KEY,    -- ISO YYYY-MM-DD
          resting_hr                 INTEGER,
          average_stress             INTEGER,
          body_battery_highest       INTEGER,
          body_battery_lowest        INTEGER,
          body_battery_most_recent   INTEGER,
          body_battery_charged       INTEGER,
          body_battery_drained       INTEGER,
          total_steps                INTEGER,
          total_kilocalories         REAL,
          raw_json                   TEXT,
          fetched_at                 TEXT
        );

        CREATE TABLE daily_sleep (
          calendar_date              TEXT PRIMARY KEY,
          deep_sleep_s               INTEGER,
          light_sleep_s              INTEGER,
          rem_sleep_s                INTEGER,
          awake_sleep_s              INTEGER,
          overall_score              INTEGER,
          overall_qualifier          TEXT,
          rem_pct                    INTEGER,
          deep_pct                   INTEGER,
          light_pct                  INTEGER,
          avg_spo2                   REAL,
          avg_respiration            REAL,
          avg_sleep_stress           REAL,
          raw_json                   TEXT,
          fetched_at                 TEXT
        );

        CREATE TABLE daily_hrv (
          calendar_date              TEXT PRIMARY KEY,
          last_night_avg             INTEGER,
          weekly_avg                 INTEGER,
          status                     TEXT,         -- BALANCED / LOW / UNBALANCED / NONE
          last_night_5min_high       INTEGER,
          feedback_phrase            TEXT,
          baseline_balanced_low      INTEGER,
          baseline_balanced_upper    INTEGER,
          baseline_low_upper         INTEGER,
          baseline_marker_value      INTEGER,
          raw_json                   TEXT,
          fetched_at                 TEXT
        );

        -- training_status: Garmin currently returns just the latest snapshot
        -- (per-day historical isn't exposed by the endpoint). Keep one row
        -- per calendar_date in case Garmin ever broadens the API, but in
        -- practice this table has 1 row.
        CREATE TABLE daily_training_status (
          calendar_date              TEXT PRIMARY KEY,
          training_status            INTEGER,
          acwr_status                TEXT,
          acwr_percent               REAL,
          acwr_ratio                 REAL,
          fitness_trend              INTEGER,
          training_load_acute        REAL,
          training_load_chronic      REAL,
          weekly_training_load       REAL,
          raw_json                   TEXT,
          fetched_at                 TEXT
        );
    """),
    (7, """
        -- ── i18n locale seed ────────────────────────────────────────────────
        -- Pre-i18n instances already have activities and expect zh-CN — seed
        -- it explicitly so they remain on zh after the default-EN policy
        -- takes effect. Fresh installs (no activities yet) get no row here,
        -- so locale_get() falls back to 'en-US'; the LoginPanel picker
        -- captures the user's choice on first visit and writes it via
        -- config_set('locale', ...).
        INSERT OR IGNORE INTO user_app_config (key, value, updated_at)
        SELECT 'locale', 'zh-CN', datetime('now')
        WHERE EXISTS (SELECT 1 FROM activities LIMIT 1);
    """),
    (8, """
        -- ── P2: tag taxonomy decoupling ─────────────────────────────────────
        -- Convert historic Chinese tag values in user_activity_tags and
        -- activity_review_context to stable English keys. After this runs,
        -- the application code (user_config.ACTIVITY_TAG_KEYS / dispatch /
        -- builders) only ever sees keys; UI rendering goes through i18n.t().
        -- Idempotent: any non-Chinese tag (already a key) is left alone via
        -- the WHERE filter.

        UPDATE user_activity_tags
        SET tag = CASE tag
          WHEN '有氧恢复'  THEN 'aerobic_recovery'
          WHEN '有氧基础'  THEN 'aerobic_base'
          WHEN '长距离'    THEN 'long_run'
          WHEN '节奏跑'    THEN 'tempo'
          WHEN '阈值跑'    THEN 'threshold'
          WHEN '间歇训练'  THEN 'intervals'
          WHEN '爬坡训练'  THEN 'hill'
          WHEN '越野'      THEN 'trail'
          WHEN '比赛'      THEN 'race'
          WHEN '其他'      THEN 'other'
          ELSE tag
        END
        WHERE tag IN ('有氧恢复','有氧基础','长距离','节奏跑','阈值跑',
                      '间歇训练','爬坡训练','越野','比赛','其他');

        UPDATE activity_review_context
        SET tag_at_generation = CASE tag_at_generation
          WHEN '有氧恢复'  THEN 'aerobic_recovery'
          WHEN '有氧基础'  THEN 'aerobic_base'
          WHEN '长距离'    THEN 'long_run'
          WHEN '节奏跑'    THEN 'tempo'
          WHEN '阈值跑'    THEN 'threshold'
          WHEN '间歇训练'  THEN 'intervals'
          WHEN '爬坡训练'  THEN 'hill'
          WHEN '越野'      THEN 'trail'
          WHEN '比赛'      THEN 'race'
          WHEN '其他'      THEN 'other'
          ELSE tag_at_generation
        END
        WHERE tag_at_generation IN ('有氧恢复','有氧基础','长距离','节奏跑','阈值跑',
                                    '间歇训练','爬坡训练','越野','比赛','其他');
    """),
    (9, """
        -- ── P3 batch 3: race-terrain decoupling ─────────────────────────────
        -- Settings race form's terrain dropdown stored Chinese display labels
        -- ("公路"/"越野") directly in user_races.terrain. Convert to stable
        -- English keys so the UI can render either locale via i18n.
        UPDATE user_races
        SET terrain = CASE terrain
          WHEN '公路' THEN 'road'
          WHEN '越野' THEN 'trail'
          ELSE terrain
        END
        WHERE terrain IN ('公路', '越野');
    """),
]


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Short-lived DB connection. Auto-runs migrations on first call per process."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        _apply_migrations(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS schema_version (
                      version INTEGER PRIMARY KEY,
                      applied_at TEXT NOT NULL
                    )""")
    cur = conn.execute("SELECT version FROM schema_version")
    applied = {row[0] for row in cur.fetchall()}
    # Always apply in version order so list ordering can't break things
    for version, sql in sorted(_MIGRATIONS, key=lambda m: m[0]):
        if version in applied:
            continue
        conn.executescript(sql)
        conn.execute("INSERT INTO schema_version VALUES (?, ?)",
                     (version, datetime.now().isoformat(timespec="seconds")))
        conn.commit()


# ── INSERT helpers ────────────────────────────────────────────────────────────
# All UPSERT semantics: re-running a sync overwrites prior rows for the same
# activity_id (idempotent). Time-series rows are deleted+inserted as a unit.

def upsert_activity_metadata(conn: sqlite3.Connection, a: dict, raw_summary: dict | None = None) -> None:
    """Insert/update the lightweight metadata row from the activities[] list.
    Does NOT set has_full_detail — call upsert_activity_full_detail for that."""
    conn.execute("""
        INSERT INTO activities (
          activity_id, activity_name, activity_type_key,
          start_time_local, start_time_gmt,
          distance_m, duration_s,
          average_hr, max_hr, average_speed_mps, max_speed_mps,
          elevation_gain_m, elevation_loss_m,
          calories, training_load, aerobic_te, anaerobic_te, te_label, vo2max,
          raw_summary_json, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(activity_id) DO UPDATE SET
          activity_name=excluded.activity_name,
          activity_type_key=excluded.activity_type_key,
          start_time_local=excluded.start_time_local,
          distance_m=excluded.distance_m,
          duration_s=excluded.duration_s,
          average_hr=excluded.average_hr,
          max_hr=excluded.max_hr,
          average_speed_mps=excluded.average_speed_mps,
          elevation_gain_m=excluded.elevation_gain_m,
          elevation_loss_m=excluded.elevation_loss_m,
          calories=excluded.calories,
          training_load=excluded.training_load,
          aerobic_te=excluded.aerobic_te,
          anaerobic_te=excluded.anaerobic_te,
          te_label=excluded.te_label,
          vo2max=excluded.vo2max,
          fetched_at=excluded.fetched_at
    """, (
        a.get("activityId"), a.get("activityName"), a.get("activityTypeKey"),
        a.get("startTimeLocal"), a.get("startTimeGMT"),
        a.get("distance"), a.get("duration"),
        a.get("averageHR"), a.get("maxHR"),
        a.get("averageSpeed"), a.get("maxSpeed"),
        a.get("elevationGain"), a.get("elevationLoss"),
        a.get("calories"),
        a.get("activityTrainingLoad"),
        a.get("aerobicTrainingEffect"), a.get("anaerobicTrainingEffect"),
        a.get("trainingEffectLabel"), a.get("vO2MaxValue"),
        json.dumps(raw_summary, ensure_ascii=False) if raw_summary else None,
        datetime.now().isoformat(timespec="seconds"),
    ))


def upsert_activity_full_detail(conn: sqlite3.Connection, activity_id: int,
                                summary: dict, laps: list, splits: list,
                                hr_zones: list, power_zones: list | None,
                                weather: dict | None,
                                metrics_rows: list[dict],
                                gps_points: list[dict],
                                unknown_channels: list[tuple[str, str | None]] | None = None) -> None:
    """Write the full per-activity detail. Sets has_full_detail=1 on the
    activities row. Replaces any existing time-series rows for this aid."""
    # Update fields the metadata-only insert may not have populated
    conn.execute("""
        UPDATE activities SET
          start_time_gmt = COALESCE(?, start_time_gmt),
          moving_duration_s = ?,
          elapsed_duration_s = ?,
          min_hr = ?,
          max_speed_mps = COALESCE(?, max_speed_mps),
          raw_summary_json = ?,
          has_full_detail = 1,
          fetched_at = ?
        WHERE activity_id = ?
    """, (
        summary.get("startTimeGMT"),
        summary.get("movingDuration"),
        summary.get("elapsedDuration"),
        summary.get("minHR"),
        summary.get("maxSpeed"),
        json.dumps(summary, ensure_ascii=False),
        datetime.now().isoformat(timespec="seconds"),
        activity_id,
    ))

    # Replace time-series + sub-tables (DELETE then INSERT — idempotent)
    for tbl in ("activity_metrics", "activity_gps", "activity_laps",
                "activity_splits", "activity_hr_zones", "activity_power_zones",
                "activity_weather", "activity_unknown_channels"):
        conn.execute(f"DELETE FROM {tbl} WHERE activity_id = ?", (activity_id,))

    # Laps
    if laps:
        conn.executemany("""
            INSERT INTO activity_laps (
              activity_id, lap_index, intensity_type,
              distance_m, duration_s, moving_duration_s,
              average_hr, max_hr, average_speed_mps,
              cadence_spm, gct_ms, power_w,
              elevation_gain_m, elevation_loss_m, raw_lap_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            (
                activity_id, i, lap.get("intensityType"),
                lap.get("distance"), lap.get("duration"), lap.get("movingDuration"),
                lap.get("averageHR"), lap.get("maxHR"), lap.get("averageSpeed"),
                lap.get("averageRunCadence"), lap.get("groundContactTime"), lap.get("averagePower"),
                lap.get("elevationGain"), lap.get("elevationLoss"),
                json.dumps(lap, ensure_ascii=False, default=str),
            ) for i, lap in enumerate(laps)
        ])

    # Splits (aggregated by type)
    if splits:
        conn.executemany("""
            INSERT INTO activity_splits (
              activity_id, split_type, no_of_splits,
              distance_m, duration_s, average_hr, average_speed_mps, raw_split_json
            ) VALUES (?,?,?,?,?,?,?,?)
        """, [
            (
                activity_id, sp.get("splitType", f"UNKNOWN_{i}"),
                sp.get("noOfSplits"),
                sp.get("distance"), sp.get("duration"),
                sp.get("averageHR"), sp.get("averageSpeed"),
                json.dumps(sp, ensure_ascii=False, default=str),
            ) for i, sp in enumerate(splits)
        ])

    # HR zones
    if hr_zones:
        conn.executemany("""
            INSERT INTO activity_hr_zones
              (activity_id, zone_number, secs_in_zone, zone_low_boundary)
            VALUES (?,?,?,?)
        """, [
            (activity_id, z.get("zoneNumber"), z.get("secsInZone"), z.get("zoneLowBoundary"))
            for z in hr_zones if isinstance(z, dict)
        ])

    # Power zones (cycling only — may be None for runs)
    if power_zones:
        conn.executemany("""
            INSERT INTO activity_power_zones
              (activity_id, zone_number, secs_in_zone, zone_low_boundary)
            VALUES (?,?,?,?)
        """, [
            (activity_id, z.get("zoneNumber"), z.get("secsInZone"), z.get("zoneLowBoundary"))
            for z in power_zones if isinstance(z, dict)
        ])

    # Weather. Garmin's /weather endpoint returns temp in Fahrenheit and wind
    # in mph regardless of the user's unit settings (verified empirically:
    # Melbourne autumn morning came back as temp=55, which only makes sense
    # as °F = 12.8°C). Convert to metric here so the schema column names
    # (_c, _mps) match what they hold.
    if weather:
        wt = _weather_to_metric(weather)
        conn.execute("""
            INSERT INTO activity_weather (
              activity_id, temp_c, apparent_temp_c, humidity_pct,
              wind_speed_mps, wind_direction, precip_type,
              weather_desc, raw_weather_json, fetched_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """, (
            activity_id,
            wt["temp_c"], wt["apparent_temp_c"], wt["humidity_pct"],
            wt["wind_speed_mps"], wt["wind_direction"],
            wt["precip_type"], wt["weather_desc"],
            json.dumps(weather, ensure_ascii=False, default=str),
            datetime.now().isoformat(timespec="seconds"),
        ))

    # Metrics stream (bulk insert). No body_battery column — that channel
    # is meaningless for users who only wear the watch during runs (BB
    # calibration requires continuous all-day wear).
    if metrics_rows:
        conn.executemany("""
            INSERT INTO activity_metrics (
              activity_id, sec_offset, ts_ms,
              hr, speed_mps, elevation_m, cadence_spm, cadence_bike_rpm, power_w,
              lat, lon, distance_cum_m,
              stride_cm, gct_ms, vert_osc_cm, vert_ratio,
              air_temp_c, grade_adj_speed,
              performance_cond, available_stamina, potential_stamina,
              impact_load, vert_speed_mps
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, [
            (
                activity_id, r["sec_offset"], r.get("ts_ms"),
                r.get("hr"), r.get("speed_mps"), r.get("elevation_m"),
                r.get("cadence_spm"), r.get("cadence_bike_rpm"), r.get("power_w"),
                r.get("lat"), r.get("lon"), r.get("distance_cum_m"),
                r.get("stride_cm"), r.get("gct_ms"),
                r.get("vert_osc_cm"), r.get("vert_ratio"),
                r.get("air_temp_c"), r.get("grade_adj_speed"),
                r.get("performance_cond"),
                r.get("available_stamina"), r.get("potential_stamina"),
                r.get("impact_load"), r.get("vert_speed_mps"),
            ) for r in metrics_rows
        ])

    # GPS polyline
    if gps_points:
        conn.executemany("""
            INSERT INTO activity_gps (activity_id, point_index, ts_ms, lat, lon)
            VALUES (?,?,?,?,?)
        """, [
            (activity_id, i, p.get("time"), p.get("lat"), p.get("lon"))
            for i, p in enumerate(gps_points)
        ])

    # Unknown-channel discovery log
    if unknown_channels:
        conn.executemany("""
            INSERT INTO activity_unknown_channels (activity_id, channel_key, sample_value)
            VALUES (?,?,?)
        """, [(activity_id, k, v) for k, v in unknown_channels])


# ── Stream-parsing helpers (used by garmin_data.fetch_activity_detail) ───────

# Mapping from Garmin metricDescriptor.key to our column name in activity_metrics.
# Channels not in this map are silently dropped (we only care about these 22).
_STREAM_CHANNEL_MAP = {
    "directHeartRate":          "hr",
    "directSpeed":              "speed_mps",
    "directElevation":          "elevation_m",
    "directDoubleCadence":      "cadence_spm",       # running (spm)
    "directBikeCadence":        "cadence_bike_rpm",  # cycling (rpm)
    "directPower":              "power_w",
    "directLatitude":           "lat",
    "directLongitude":          "lon",
    "sumDistance":              "distance_cum_m",
    "directStrideLength":       "stride_cm",
    "directGroundContactTime":  "gct_ms",
    "directVerticalOscillation":"vert_osc_cm",
    "directVerticalRatio":      "vert_ratio",
    "directAirTemperature":     "air_temp_c",
    "directGradeAdjustedSpeed": "grade_adj_speed",
    "directPerformanceCondition":"performance_cond",
    "directAvailableStamina":   "available_stamina",
    "directPotentialStamina":   "potential_stamina",
    "directImpactLoadFactor":   "impact_load",
    "directVerticalSpeed":      "vert_speed_mps",
    "directTimestamp":          "ts_ms",
}


def extract_unknown_channels(stream: dict) -> list[tuple[str, str | None]]:
    """Walk a /details stream and collect every descriptor key we don't yet
    map to a column. Excludes connectIQDeveloperField-* (always user-specific
    3rd-party data, not worth tracking). Returns [(channel_key, sample_value_str), ...]
    where sample_value_str is the first non-None observed value, or None if all
    rows were None.

    Use `list_unknown_channels(conn)` to query the accumulated discoveries
    across all activities — when the same key shows up frequently, it's a
    candidate for promoting to a real column."""
    descriptors = stream.get("metricDescriptors") or []
    rows        = stream.get("activityDetailMetrics") or []
    if not descriptors:
        return []

    unknown_idxs: dict[int, str] = {}
    for d in descriptors:
        key = d.get("key", "")
        idx = d.get("metricsIndex")
        if idx is None or key in _STREAM_CHANNEL_MAP:
            continue
        if key.startswith("connectIQDeveloperField"):
            continue            # user-specific 3rd-party noise; never worth tracking
        unknown_idxs[idx] = key

    if not unknown_idxs:
        return []

    # First non-None sample per unknown channel
    samples: dict[str, str | None] = {key: None for key in unknown_idxs.values()}
    pending = set(unknown_idxs.values())
    for r in rows:
        if not pending:
            break
        m = r.get("metrics") or []
        for idx, key in unknown_idxs.items():
            if key not in pending:
                continue
            if idx < len(m) and m[idx] is not None:
                samples[key] = str(m[idx])
                pending.discard(key)
    return list(samples.items())


def parse_full_metrics_stream(stream: dict) -> list[dict]:
    """Parse Garmin's /details metric stream into list of per-second dicts.
    NO downsampling — every row in activityDetailMetrics becomes one dict.
    Returns rows with 'sec_offset' (relative seconds from start) populated."""
    descriptors = stream.get("metricDescriptors", []) or []
    rows = stream.get("activityDetailMetrics", []) or []
    if not descriptors or not rows:
        return []

    # Build (column_index → our_key) mapping
    col_map: dict[int, str] = {}
    for d in descriptors:
        key = d.get("key")
        idx = d.get("metricsIndex")
        if key in _STREAM_CHANNEL_MAP and idx is not None:
            col_map[idx] = _STREAM_CHANNEL_MAP[key]

    if not col_map:
        return []

    out: list[dict] = []
    base_ts: int | None = None
    ts_col = next((i for i, name in col_map.items() if name == "ts_ms"), None)
    for r in rows:
        m = r.get("metrics") or []
        row_dict: dict = {}
        for i, name in col_map.items():
            if i < len(m):
                row_dict[name] = m[i]

        ts = row_dict.get("ts_ms")
        if ts is None and ts_col is not None and ts_col < len(m):
            ts = m[ts_col]
        if ts is None:
            continue

        if base_ts is None:
            base_ts = int(ts)
        row_dict["sec_offset"] = max(0, int((int(ts) - base_ts) / 1000))
        row_dict["ts_ms"] = int(ts)
        out.append(row_dict)

    return out


def _weather_to_metric(w: dict) -> dict:
    """Garmin /weather returns Fahrenheit + mph; normalize to metric."""
    def f_to_c(v):
        return None if v is None else round((v - 32) * 5 / 9, 1)
    def mph_to_mps(v):
        return None if v is None else round(v * 0.44704, 2)

    wd = w.get("windDirectionCompassPoint")
    wd_str = wd.get("compassPointType") if isinstance(wd, dict) else wd

    wt = w.get("weatherTypeDTO")
    desc = wt.get("desc") if isinstance(wt, dict) else w.get("weatherTypeDescription")

    return {
        "temp_c":          f_to_c(w.get("temp")),
        "apparent_temp_c": f_to_c(w.get("apparentTemp")),
        "humidity_pct":    w.get("relativeHumidity"),
        "wind_speed_mps":  mph_to_mps(w.get("windSpeed")),
        "wind_direction":  wd_str,
        "precip_type":     None,                    # Garmin doesn't expose this here
        "weather_desc":    desc,
    }


def parse_legacy_cached_metrics(metrics_dict: dict) -> list[dict]:
    """Convert legacy JSON-cache metrics (parallel arrays already thinned to ~10s)
    to per-row dicts for SQLite insert. Lossy on:
      - Resolution (already at ~10s, not 1Hz — Phase 1 backfill of pre-existing
        cache files; new fetches go through parse_full_metrics_stream at 1Hz)
      - Channels (cache has 10 channels, SQLite has 22 — extra cols stay NULL,
        including lat/lon/distance_cum_m/temp/etc.)
    """
    ts = metrics_dict.get("ts") or []
    if not ts:
        return []
    cm = {  # JSON cache key → SQLite column
        "hr":         "hr",
        "speed":      "speed_mps",
        "elev":       "elevation_m",
        "cadence":    "cadence_spm",
        "power":      "power_w",
        "stride":     "stride_cm",
        "gct":        "gct_ms",
        "vert_osc":   "vert_osc_cm",
        "vert_ratio": "vert_ratio",
    }
    base_ts = next((t for t in ts if t), None)
    if base_ts is None:
        return []
    base_ts = int(base_ts)
    rows: list[dict] = []
    for i, t in enumerate(ts):
        if t is None:
            continue
        row: dict = {
            "ts_ms":      int(t),
            "sec_offset": max(0, int((int(t) - base_ts) / 1000)),
        }
        for src, dst in cm.items():
            arr = metrics_dict.get(src) or []
            if i < len(arr) and arr[i] is not None:
                row[dst] = arr[i]
        rows.append(row)
    return rows


# ── Read helpers ─────────────────────────────────────────────────────────────

def get_activity_metric_rowcount(conn: sqlite3.Connection, activity_id: int) -> int:
    cur = conn.execute("SELECT count(*) FROM activity_metrics WHERE activity_id = ?",
                       (activity_id,))
    return cur.fetchone()[0]


def has_full_detail(conn: sqlite3.Connection, activity_id: int) -> bool:
    cur = conn.execute("SELECT has_full_detail FROM activities WHERE activity_id = ?",
                       (activity_id,))
    row = cur.fetchone()
    return bool(row and row[0])


def save_review_context(conn: sqlite3.Connection, activity_id: int,
                        tag_at_generation: str, builder_name: str,
                        builder_version_hash: str, context_md: str,
                        highlight_windows: list[dict]) -> None:
    """UPSERT the builder's output for one activity. Replaces any prior
    cached context — typically called after the user clicks 生成复盘
    (or after a re-tag invalidation triggers regeneration)."""
    conn.execute("""
        INSERT INTO activity_review_context (
          activity_id, tag_at_generation, builder_name,
          builder_version_hash, context_md, highlight_windows_json, generated_at
        ) VALUES (?,?,?,?,?,?,?)
        ON CONFLICT(activity_id) DO UPDATE SET
          tag_at_generation     = excluded.tag_at_generation,
          builder_name          = excluded.builder_name,
          builder_version_hash  = excluded.builder_version_hash,
          context_md            = excluded.context_md,
          highlight_windows_json= excluded.highlight_windows_json,
          generated_at          = excluded.generated_at
    """, (
        activity_id, tag_at_generation, builder_name,
        builder_version_hash, context_md,
        json.dumps(highlight_windows or [], ensure_ascii=False),
        datetime.now().isoformat(timespec="seconds"),
    ))


def load_review_context(conn: sqlite3.Connection, activity_id: int) -> dict | None:
    """Return the cached BuildResult fields, or None if no cache exists.
    Caller should compare tag_at_generation + builder_version_hash against
    the current tag/hash to decide whether the cache is still valid."""
    row = conn.execute("""
        SELECT tag_at_generation, builder_name, builder_version_hash,
               context_md, highlight_windows_json, generated_at
          FROM activity_review_context
         WHERE activity_id = ?
    """, (activity_id,)).fetchone()
    if not row:
        return None
    return {
        "tag_at_generation":    row[0],
        "builder_name":         row[1],
        "builder_version_hash": row[2],
        "context_md":           row[3],
        "highlight_windows":    json.loads(row[4]) if row[4] else [],
        "generated_at":         row[5],
    }


def clear_review_context(conn: sqlite3.Connection, activity_id: int) -> None:
    """Drop the cached context for one activity. Call when invalidating
    (e.g. user changed tag, or hash mismatch + user clicked regenerate)."""
    conn.execute("DELETE FROM activity_review_context WHERE activity_id = ?",
                 (activity_id,))


def list_unknown_channels(conn: sqlite3.Connection) -> list[tuple[str, int, str | None]]:
    """Aggregate unknown channels seen across all activities.
    Returns [(channel_key, n_activities, sample_value), ...] sorted by frequency.
    Use this to decide which un-mapped channels are worth promoting to columns."""
    rows = conn.execute("""
        SELECT channel_key,
               count(DISTINCT activity_id) AS n_acts,
               max(sample_value) AS sample
          FROM activity_unknown_channels
         GROUP BY channel_key
         ORDER BY n_acts DESC, channel_key
    """).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


# ── Activity-data readers ────────────────────────────────────────────────────
# These readers reconstruct the LEGACY JSON-shaped dicts that the rest of the
# app (sidebar / trends / coach / builders) consumes. SQLite is the single
# source of truth.
#
# Key/type contract is preserved EXACTLY:
#   activities list  : camelCase keys (activityId, startTimeLocal, ...)
#   weekly summary   : {week, runKm, rideKm, otherKm, activities, weeklyLoad}
#   per-activity     : {summary, laps, splits, gps, metrics, hr_zones,
#                       fetched_at, activity_id}
#   detail.metrics   : thinned parallel arrays {ts, hr, speed, elev, cadence,
#                       power, stride, gct, vert_osc, vert_ratio} at ~10s
#                       resolution (every 5th row from the 1Hz SQLite store).

def get_recent_activities(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """Return activities in the last N days, NEWEST FIRST, shaped like the
    legacy detailed['activities'] list. camelCase keys preserved verbatim
    so sidebar / trends / coach / build_coaching_context don't need to
    change."""
    from datetime import date as _date, timedelta as _td
    cutoff = (_date.today() - _td(days=days)).isoformat()
    rows = conn.execute("""
        SELECT activity_id, activity_name, activity_type_key, start_time_local,
               distance_m, duration_s, average_hr, max_hr, calories,
               elevation_gain_m, elevation_loss_m, average_speed_mps,
               te_label, aerobic_te, anaerobic_te, vo2max, training_load,
               start_time_gmt
          FROM activities
         WHERE start_time_local >= ?
         ORDER BY start_time_local DESC
    """, (cutoff,)).fetchall()
    return [
        {
            "activityId":              r[0],
            "activityName":            r[1] or "",
            "activityTypeKey":         r[2] or "",
            "startTimeLocal":          r[3] or "",
            "distance":                r[4],
            "duration":                r[5],
            "averageHR":               r[6],
            "maxHR":                   r[7],
            "calories":                r[8],
            "elevationGain":           r[9],
            "elevationLoss":           r[10],
            "averageSpeed":            r[11],
            "trainingEffectLabel":     r[12],
            "aerobicTrainingEffect":   r[13],
            "anaerobicTrainingEffect": r[14],
            "vO2MaxValue":             r[15],
            "activityTrainingLoad":    r[16],
            "startTimeGMT":            r[17] or "",
        }
        for r in rows
    ]


def get_weekly_summary(conn: sqlite3.Connection, weeks: int = 26) -> list[dict]:
    """Compute ISO-week aggregation. Joins activities (run/ride/other km +
    weekly load) with daily_hrv + daily_sleep for avgHRV + avgSleepScore.
    Returns list of {week, runKm, rideKm, otherKm, activities, weeklyLoad,
    avgHRV, avgSleepScore} ordered by week ASCENDING — matches the legacy
    longterm['weeks'][] shape from _compute_weekly_summary."""
    from collections import defaultdict
    from datetime import date as _date, timedelta as _td

    cutoff = (_date.today() - _td(days=weeks * 7)).isoformat()

    buckets: dict[str, dict] = defaultdict(
        lambda: {"run_km": 0.0, "ride_km": 0.0, "other_km": 0.0,
                 "load": 0.0, "n_acts": 0,
                 "hrv": [], "sleep": []}
    )

    def _wk_for(date_str: str) -> str | None:
        try:
            d = _date.fromisoformat(date_str[:10])
        except (ValueError, TypeError):
            return None
        iso_year, iso_week, _ = d.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    for start, type_key, dist, load in conn.execute("""
        SELECT start_time_local, activity_type_key, distance_m, training_load
          FROM activities
         WHERE start_time_local >= ?
    """, (cutoff,)).fetchall():
        wk = _wk_for(start or "")
        if not wk:
            continue
        km = (dist or 0) / 1000
        tk = type_key or ""
        if "run" in tk:
            buckets[wk]["run_km"] += km
        elif any(x in tk for x in ("ride", "virtual", "cycl")):
            buckets[wk]["ride_km"] += km
        else:
            buckets[wk]["other_km"] += km
        buckets[wk]["load"]   += load or 0
        buckets[wk]["n_acts"] += 1

    for cd, last_night in conn.execute("""
        SELECT calendar_date, last_night_avg FROM daily_hrv
         WHERE calendar_date >= ? AND last_night_avg IS NOT NULL
    """, (cutoff,)).fetchall():
        wk = _wk_for(cd or "")
        if wk:
            buckets[wk]["hrv"].append(last_night)

    for cd, score in conn.execute("""
        SELECT calendar_date, overall_score FROM daily_sleep
         WHERE calendar_date >= ? AND overall_score IS NOT NULL
    """, (cutoff,)).fetchall():
        wk = _wk_for(cd or "")
        if wk:
            buckets[wk]["sleep"].append(score)

    return [
        {
            "week":          wk,
            "runKm":         round(b["run_km"], 1),
            "rideKm":        round(b["ride_km"], 1),
            "otherKm":       round(b["other_km"], 1),
            "activities":    b["n_acts"],
            "weeklyLoad":    round(b["load"]),
            "avgHRV":        round(sum(b["hrv"]) / len(b["hrv"])) if b["hrv"] else None,
            "avgSleepScore": round(sum(b["sleep"]) / len(b["sleep"])) if b["sleep"] else None,
        }
        for wk, b in sorted(buckets.items())
    ]


def get_activity_detail(conn: sqlite3.Connection, activity_id: int) -> dict | None:
    """Return the legacy-shaped detail dict for one activity:
       {fetched_at, activity_id, summary, laps, splits, gps, metrics, hr_zones}

    metrics is the THINNED parallel-arrays shape (every 5th 1Hz sample, ~10s
    resolution) — exactly what the legacy `_parse_metrics_stream` produced,
    so chart rendering + default builder consumers work unchanged. For the
    full-1Hz time series, query activity_metrics directly.

    Returns None when the activity has no rows in `activities` table OR has
    has_full_detail=0 (= never fetched). Callers should treat None as
    "user hasn't opened this activity in 🔬 复盘 yet — trigger a fetch."""
    meta = conn.execute("""
        SELECT raw_summary_json, fetched_at, has_full_detail
          FROM activities WHERE activity_id = ?
    """, (activity_id,)).fetchone()
    if not meta or not meta[2]:
        return None
    summary = json.loads(meta[0]) if meta[0] else {}
    fetched_at = meta[1] or ""

    lap_rows = conn.execute("""
        SELECT raw_lap_json FROM activity_laps
         WHERE activity_id = ? ORDER BY lap_index
    """, (activity_id,)).fetchall()
    laps = [json.loads(r[0]) for r in lap_rows if r[0]]

    split_rows = conn.execute("""
        SELECT raw_split_json FROM activity_splits WHERE activity_id = ?
    """, (activity_id,)).fetchall()
    splits = [json.loads(r[0]) for r in split_rows if r[0]]

    hr_zones = [
        {"zoneNumber": r[0], "secsInZone": r[1], "zoneLowBoundary": r[2]}
        for r in conn.execute("""
            SELECT zone_number, secs_in_zone, zone_low_boundary
              FROM activity_hr_zones WHERE activity_id = ? ORDER BY zone_number
        """, (activity_id,)).fetchall()
    ]

    gps = [
        {"lat": r[0], "lon": r[1], "time": r[2]}
        for r in conn.execute("""
            SELECT lat, lon, ts_ms FROM activity_gps
             WHERE activity_id = ? ORDER BY point_index
        """, (activity_id,)).fetchall()
    ]

    # Metrics: read 1Hz from activity_metrics, thin to ~10s for legacy callers.
    # Order channels to match _parse_metrics_stream's output exactly.
    metric_rows = conn.execute("""
        SELECT ts_ms, hr, speed_mps, elevation_m, cadence_spm, power_w,
               stride_cm, gct_ms, vert_osc_cm, vert_ratio
          FROM activity_metrics
         WHERE activity_id = ?
         ORDER BY sec_offset
    """, (activity_id,)).fetchall()
    keys = ("ts", "hr", "speed", "elev", "cadence",
            "power", "stride", "gct", "vert_osc", "vert_ratio")
    metrics: dict[str, list] = {k: [] for k in keys}
    for r in metric_rows[::5]:    # thin to ~10s resolution
        for i, k in enumerate(keys):
            metrics[k].append(r[i])

    return {
        "fetched_at":  fetched_at,
        "activity_id": activity_id,
        "summary":     summary,
        "splits":      splits,
        "laps":        laps,
        "gps":         gps,
        "metrics":     metrics,
        "hr_zones":    hr_zones,
    }


def get_app_metadata(conn: sqlite3.Connection) -> dict:
    """Return singleton sync metadata that lived in garmin_data.json:
    fetched_at + display_name. Stored in user_app_config under
    'sync_fetched_at' and 'garmin_display_name'."""
    return {
        "fetched_at":   config_get(conn, "sync_fetched_at", "") or "",
        "display_name": config_get(conn, "garmin_display_name", "") or "",
    }


def set_app_metadata(conn: sqlite3.Connection, fetched_at: str | None = None,
                     display_name: str | None = None) -> None:
    """Update singleton sync metadata. Each arg only writes if not None."""
    if fetched_at is not None:
        config_set(conn, "sync_fetched_at", fetched_at)
    if display_name is not None:
        config_set(conn, "garmin_display_name", display_name)


# ── Wellness tier readers/writers (daily_*) ─────────────────────────────────
# Replace the daily_summaries / sleep / hrv / training_status arrays that
# used to live in garmin_data.json. Each reader returns a list of dicts in
# the legacy JSON shape (e.g. daily_summaries items keyed by 'calendarDate',
# hrv items keyed by 'calendar_date' to match Garmin's API mixed casing).
# Writers UPSERT by calendar_date — re-running a sync is idempotent.

def get_recent_daily_summaries(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """List of {calendarDate, restingHeartRate, averageStressLevel, body*,
    totalSteps, totalKilocalories} for the last N days, sorted by date ASC.
    Matches the legacy 'daily_summaries' list shape exactly."""
    from datetime import date as _date, timedelta as _td
    cutoff = (_date.today() - _td(days=days)).isoformat()
    rows = conn.execute("""
        SELECT calendar_date, resting_hr, average_stress,
               body_battery_highest, body_battery_lowest, body_battery_most_recent,
               body_battery_charged, body_battery_drained,
               total_steps, total_kilocalories
          FROM daily_summary
         WHERE calendar_date >= ?
         ORDER BY calendar_date ASC
    """, (cutoff,)).fetchall()
    return [
        {
            "calendarDate":               r[0],
            "restingHeartRate":           r[1],
            "averageStressLevel":         r[2],
            "bodyBatteryHighestValue":    r[3],
            "bodyBatteryLowestValue":     r[4],
            "bodyBatteryMostRecentValue": r[5],
            "bodyBatteryChargedValue":    r[6],
            "bodyBatteryDrainedValue":    r[7],
            "totalSteps":                 r[8],
            "totalKilocalories":          r[9],
        }
        for r in rows
    ]


def upsert_daily_summary(conn: sqlite3.Connection, rec: dict) -> None:
    """UPSERT one daily summary row. `rec` is the Garmin-shaped dict (camelCase
    keys, e.g. 'calendarDate' / 'restingHeartRate' / 'bodyBatteryHighestValue')."""
    cd = rec.get("calendarDate")
    if not cd:
        return
    conn.execute("""
        INSERT INTO daily_summary (
          calendar_date, resting_hr, average_stress,
          body_battery_highest, body_battery_lowest, body_battery_most_recent,
          body_battery_charged, body_battery_drained,
          total_steps, total_kilocalories, raw_json, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(calendar_date) DO UPDATE SET
          resting_hr               = excluded.resting_hr,
          average_stress           = excluded.average_stress,
          body_battery_highest     = excluded.body_battery_highest,
          body_battery_lowest      = excluded.body_battery_lowest,
          body_battery_most_recent = excluded.body_battery_most_recent,
          body_battery_charged     = excluded.body_battery_charged,
          body_battery_drained     = excluded.body_battery_drained,
          total_steps              = excluded.total_steps,
          total_kilocalories       = excluded.total_kilocalories,
          raw_json                 = excluded.raw_json,
          fetched_at               = excluded.fetched_at
    """, (
        cd, rec.get("restingHeartRate"), rec.get("averageStressLevel"),
        rec.get("bodyBatteryHighestValue"), rec.get("bodyBatteryLowestValue"),
        rec.get("bodyBatteryMostRecentValue"), rec.get("bodyBatteryChargedValue"),
        rec.get("bodyBatteryDrainedValue"),
        rec.get("totalSteps"), rec.get("totalKilocalories"),
        json.dumps(rec, ensure_ascii=False, default=str),
        _now_iso(),
    ))


def get_recent_sleep(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """List of sleep dicts (calendarDate + deep/light/rem/awake seconds +
    score/qualifier + percentages + spO2 + respiration + stress) for last N days."""
    from datetime import date as _date, timedelta as _td
    cutoff = (_date.today() - _td(days=days)).isoformat()
    rows = conn.execute("""
        SELECT calendar_date, deep_sleep_s, light_sleep_s, rem_sleep_s,
               awake_sleep_s, overall_score, overall_qualifier,
               rem_pct, deep_pct, light_pct,
               avg_spo2, avg_respiration, avg_sleep_stress
          FROM daily_sleep
         WHERE calendar_date >= ?
         ORDER BY calendar_date ASC
    """, (cutoff,)).fetchall()
    return [
        {
            "calendarDate":            r[0],
            "deepSleepSeconds":        r[1] or 0,
            "lightSleepSeconds":       r[2] or 0,
            "remSleepSeconds":         r[3] or 0,
            "awakeSleepSeconds":       r[4] or 0,
            "overallScore":            r[5],
            "overallQualifier":        r[6],
            "remPercentage":           r[7],
            "deepPercentage":          r[8],
            "lightPercentage":         r[9],
            "averageSpO2Value":        r[10],
            "averageRespirationValue": r[11],
            "avgSleepStress":          r[12],
        }
        for r in rows
    ]


def upsert_daily_sleep(conn: sqlite3.Connection, rec: dict) -> None:
    """UPSERT one sleep row. `rec` is Garmin-shaped dict from _parse_sleep."""
    cd = rec.get("calendarDate")
    if not cd:
        return
    conn.execute("""
        INSERT INTO daily_sleep (
          calendar_date, deep_sleep_s, light_sleep_s, rem_sleep_s, awake_sleep_s,
          overall_score, overall_qualifier,
          rem_pct, deep_pct, light_pct,
          avg_spo2, avg_respiration, avg_sleep_stress,
          raw_json, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(calendar_date) DO UPDATE SET
          deep_sleep_s       = excluded.deep_sleep_s,
          light_sleep_s      = excluded.light_sleep_s,
          rem_sleep_s        = excluded.rem_sleep_s,
          awake_sleep_s      = excluded.awake_sleep_s,
          overall_score      = excluded.overall_score,
          overall_qualifier  = excluded.overall_qualifier,
          rem_pct            = excluded.rem_pct,
          deep_pct           = excluded.deep_pct,
          light_pct          = excluded.light_pct,
          avg_spo2           = excluded.avg_spo2,
          avg_respiration    = excluded.avg_respiration,
          avg_sleep_stress   = excluded.avg_sleep_stress,
          raw_json           = excluded.raw_json,
          fetched_at         = excluded.fetched_at
    """, (
        cd, rec.get("deepSleepSeconds"), rec.get("lightSleepSeconds"),
        rec.get("remSleepSeconds"), rec.get("awakeSleepSeconds"),
        rec.get("overallScore"), rec.get("overallQualifier"),
        rec.get("remPercentage"), rec.get("deepPercentage"), rec.get("lightPercentage"),
        rec.get("averageSpO2Value"), rec.get("averageRespirationValue"),
        rec.get("avgSleepStress"),
        json.dumps(rec, ensure_ascii=False, default=str),
        _now_iso(),
    ))


def upsert_daily_sleep_score(conn: sqlite3.Connection, calendar_date: str,
                             score: int | None) -> None:
    """Score-only UPSERT used by the 6-month sync — `gs.DailySleep.list`
    returns only {calendar_date, value}, NOT the rich DTO. This helper
    updates `overall_score` alone, preserving deep/light/rem/qualifier
    if the detailed sync already filled them in for that day.

    If no row exists yet, inserts a sparse row with just the score."""
    if not calendar_date:
        return
    conn.execute("""
        INSERT INTO daily_sleep (calendar_date, overall_score, fetched_at)
        VALUES (?, ?, ?)
        ON CONFLICT(calendar_date) DO UPDATE SET
          overall_score = excluded.overall_score,
          fetched_at    = excluded.fetched_at
    """, (calendar_date, score, _now_iso()))


def get_recent_hrv(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """List of HRV dicts (calendar_date + last_night_avg + weekly_avg + status
    + 5min_high + feedback + baseline dict) for last N days. NOTE: HRV uses
    'calendar_date' snake_case to match Garmin API + legacy JSON."""
    from datetime import date as _date, timedelta as _td
    cutoff = (_date.today() - _td(days=days)).isoformat()
    rows = conn.execute("""
        SELECT calendar_date, last_night_avg, weekly_avg, status,
               last_night_5min_high, feedback_phrase,
               baseline_balanced_low, baseline_balanced_upper,
               baseline_low_upper, baseline_marker_value
          FROM daily_hrv
         WHERE calendar_date >= ?
         ORDER BY calendar_date ASC
    """, (cutoff,)).fetchall()
    out = []
    for r in rows:
        baseline = {}
        if r[6] is not None: baseline["balanced_low"]    = r[6]
        if r[7] is not None: baseline["balanced_upper"]  = r[7]
        if r[8] is not None: baseline["low_upper"]       = r[8]
        if r[9] is not None: baseline["marker_value"]    = r[9]
        out.append({
            "calendar_date":          r[0],
            "last_night_avg":         r[1],
            "weekly_avg":             r[2],
            "status":                 r[3],
            "last_night_5_min_high":  r[4],
            "feedback_phrase":        r[5],
            "baseline":               baseline,
        })
    return out


def upsert_daily_hrv(conn: sqlite3.Connection, rec: dict) -> None:
    """UPSERT one HRV row. `rec` is Garmin-shaped dict (snake_case keys,
    e.g. 'calendar_date' + 'last_night_avg' + nested 'baseline' dict)."""
    cd = rec.get("calendar_date")
    if not cd:
        return
    b = rec.get("baseline") or {}
    conn.execute("""
        INSERT INTO daily_hrv (
          calendar_date, last_night_avg, weekly_avg, status,
          last_night_5min_high, feedback_phrase,
          baseline_balanced_low, baseline_balanced_upper,
          baseline_low_upper, baseline_marker_value,
          raw_json, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(calendar_date) DO UPDATE SET
          last_night_avg          = excluded.last_night_avg,
          weekly_avg              = excluded.weekly_avg,
          status                  = excluded.status,
          last_night_5min_high    = excluded.last_night_5min_high,
          feedback_phrase         = excluded.feedback_phrase,
          baseline_balanced_low   = excluded.baseline_balanced_low,
          baseline_balanced_upper = excluded.baseline_balanced_upper,
          baseline_low_upper      = excluded.baseline_low_upper,
          baseline_marker_value   = excluded.baseline_marker_value,
          raw_json                = excluded.raw_json,
          fetched_at              = excluded.fetched_at
    """, (
        cd, rec.get("last_night_avg"), rec.get("weekly_avg"), rec.get("status"),
        rec.get("last_night_5_min_high"), rec.get("feedback_phrase"),
        b.get("balanced_low"), b.get("balanced_upper"),
        b.get("low_upper"), b.get("marker_value"),
        json.dumps(rec, ensure_ascii=False, default=str),
        _now_iso(),
    ))


def get_recent_training_status(conn: sqlite3.Connection, days: int = 90) -> list[dict]:
    """List of training_status dicts (most recent first) for last N days.
    Garmin's endpoint typically returns 1 row (just current state)."""
    from datetime import date as _date, timedelta as _td
    cutoff = (_date.today() - _td(days=days)).isoformat()
    rows = conn.execute("""
        SELECT calendar_date, training_status, acwr_status, acwr_percent,
               acwr_ratio, fitness_trend,
               training_load_acute, training_load_chronic, weekly_training_load,
               raw_json
          FROM daily_training_status
         WHERE calendar_date >= ?
         ORDER BY calendar_date ASC
    """, (cutoff,)).fetchall()
    out = []
    for r in rows:
        # Decode raw_json so the result carries all fields the legacy JSON had,
        # not just the column-exploded ones.
        full = {}
        if r[9]:
            try:
                full = json.loads(r[9])
            except json.JSONDecodeError:
                full = {}
        # Make sure the column-exploded fields override any drift in raw_json
        full.update({
            "calendar_date":                       r[0],
            "training_status":                     r[1],
            "acwr_status":                         r[2],
            "acwr_percent":                        r[3],
            "daily_acute_chronic_workload_ratio":  r[4],
            "fitness_trend":                       r[5],
            "daily_training_load_acute":           r[6],
            "daily_training_load_chronic":         r[7],
            "weekly_training_load":                r[8],
        })
        out.append(full)
    return out


def upsert_daily_training_status(conn: sqlite3.Connection, rec: dict) -> None:
    """UPSERT one training_status row. `rec` is Garmin-shaped dict (snake_case)."""
    cd = rec.get("calendar_date")
    if not cd:
        return
    conn.execute("""
        INSERT INTO daily_training_status (
          calendar_date, training_status, acwr_status, acwr_percent,
          acwr_ratio, fitness_trend,
          training_load_acute, training_load_chronic, weekly_training_load,
          raw_json, fetched_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(calendar_date) DO UPDATE SET
          training_status       = excluded.training_status,
          acwr_status           = excluded.acwr_status,
          acwr_percent          = excluded.acwr_percent,
          acwr_ratio            = excluded.acwr_ratio,
          fitness_trend         = excluded.fitness_trend,
          training_load_acute   = excluded.training_load_acute,
          training_load_chronic = excluded.training_load_chronic,
          weekly_training_load  = excluded.weekly_training_load,
          raw_json              = excluded.raw_json,
          fetched_at            = excluded.fetched_at
    """, (
        cd, rec.get("training_status"), rec.get("acwr_status"),
        rec.get("acwr_percent"),
        rec.get("daily_acute_chronic_workload_ratio"),
        rec.get("fitness_trend"),
        rec.get("daily_training_load_acute"),
        rec.get("daily_training_load_chronic"),
        rec.get("weekly_training_load"),
        json.dumps(rec, ensure_ascii=False, default=str),
        _now_iso(),
    ))


# ── User-state helpers ──────────────────────────────────────────────────────
# All `user_*` and `chat_*` table accessors. Function names omit the table
# prefix (e.g. `tag_get`, not `user_activity_tag_get`) — the prefix expresses
# data lifecycle (user-typed, not regenerable from sync), the function name
# stays terse and unambiguous within db's namespace.
#
# Caller pattern: `with db.connect() as conn: db.tag_set(conn, aid, tag)`.
# All writes auto-commit on context-manager exit (see db.connect).

def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ── Singleton key/value config ────────────────────────────────────────────────

def config_get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM user_app_config WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def config_set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    """value=None deletes the key (uniform with config_get default semantics)."""
    if value is None:
        conn.execute("DELETE FROM user_app_config WHERE key = ?", (key,))
        return
    conn.execute("""
        INSERT INTO user_app_config (key, value, updated_at) VALUES (?,?,?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
    """, (key, value, _now_iso()))


def config_all(conn: sqlite3.Connection) -> dict[str, str]:
    """Bulk-read all keys — for build_context() and similar consumers."""
    return {k: v for k, v in conn.execute("SELECT key, value FROM user_app_config").fetchall()}


# ── Locale (BCP-47) ──────────────────────────────────────────────────────────
# Sugar over config_get/set('locale', ...) — used hot enough that a typed
# helper is worth it. Default 'en-US' for fresh installs (existing instances
# were seeded 'zh-CN' in migration #7).

LOCALE_DEFAULT = "en-US"
LOCALES_SUPPORTED = ("en-US", "zh-CN")


def locale_get(conn: sqlite3.Connection) -> str:
    return config_get(conn, "locale", LOCALE_DEFAULT) or LOCALE_DEFAULT


def locale_set(conn: sqlite3.Connection, value: str) -> None:
    if value not in LOCALES_SUPPORTED:
        raise ValueError(f"unsupported locale {value!r} (supported: {LOCALES_SUPPORTED})")
    config_set(conn, "locale", value)


# ── Races ─────────────────────────────────────────────────────────────────────

def races_list(conn: sqlite3.Connection) -> list[dict]:
    """Return all races, sorted by date ascending (matches uc.next_race ordering)."""
    rows = conn.execute("""
        SELECT race_id, name, date, distance_km, terrain, goal_time, notes, added_at
          FROM user_races ORDER BY date IS NULL, date
    """).fetchall()
    return [
        {"race_id": r[0], "name": r[1], "date": r[2], "distance_km": r[3],
         "terrain": r[4], "goal_time": r[5], "notes": r[6], "added_at": r[7]}
        for r in rows
    ]


def races_add(conn: sqlite3.Connection, name: str, date: str | None = None,
              distance_km: float | None = None, terrain: str | None = None,
              goal_time: str | None = None, notes: str | None = None) -> int:
    cur = conn.execute("""
        INSERT INTO user_races (name, date, distance_km, terrain, goal_time, notes, added_at)
        VALUES (?,?,?,?,?,?,?)
    """, (name, date, distance_km, terrain, goal_time, notes, _now_iso()))
    return cur.lastrowid


def races_update(conn: sqlite3.Connection, race_id: int, **fields) -> None:
    """Update a subset of columns; unknown keys raise."""
    allowed = {"name", "date", "distance_km", "terrain", "goal_time", "notes"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"races_update: unknown field(s) {bad}")
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE user_races SET {cols} WHERE race_id = ?",
                 (*fields.values(), race_id))


def races_delete(conn: sqlite3.Connection, race_id: int) -> None:
    conn.execute("DELETE FROM user_races WHERE race_id = ?", (race_id,))


# ── Coach insights ────────────────────────────────────────────────────────────

def insights_list(conn: sqlite3.Connection) -> list[dict]:
    """Return all pinned insights, oldest first (matches uc.list_insights)."""
    rows = conn.execute("""
        SELECT insight_id, text, saved_at, source FROM user_coach_insights
        ORDER BY insight_id
    """).fetchall()
    return [{"insight_id": r[0], "text": r[1], "saved_at": r[2], "source": r[3]}
            for r in rows]


def insights_add(conn: sqlite3.Connection, text: str, source: str = "manual") -> int:
    cur = conn.execute("""
        INSERT INTO user_coach_insights (text, saved_at, source) VALUES (?,?,?)
    """, (text.strip(), _now_iso(), source))
    return cur.lastrowid


def insights_update(conn: sqlite3.Connection, insight_id: int, text: str) -> None:
    conn.execute("UPDATE user_coach_insights SET text = ? WHERE insight_id = ?",
                 (text.strip(), insight_id))


def insights_delete(conn: sqlite3.Connection, insight_id: int) -> None:
    conn.execute("DELETE FROM user_coach_insights WHERE insight_id = ?", (insight_id,))


# ── Activity tags ─────────────────────────────────────────────────────────────

def tag_get(conn: sqlite3.Connection, activity_id: int) -> str:
    """Return the tag, or "" if untagged (matches uc.get_activity_tag semantics)."""
    row = conn.execute("SELECT tag FROM user_activity_tags WHERE activity_id = ?",
                       (activity_id,)).fetchone()
    return row[0] if row else ""


def tag_set(conn: sqlite3.Connection, activity_id: int, tag: str) -> None:
    """Empty tag deletes the row (matches uc.set_activity_tag semantics)."""
    if not tag:
        conn.execute("DELETE FROM user_activity_tags WHERE activity_id = ?", (activity_id,))
        return
    conn.execute("""
        INSERT INTO user_activity_tags (activity_id, tag, tagged_at) VALUES (?,?,?)
        ON CONFLICT(activity_id) DO UPDATE SET tag = excluded.tag, tagged_at = excluded.tagged_at
    """, (activity_id, tag, _now_iso()))


def tags_all(conn: sqlite3.Connection) -> dict[int, str]:
    """All tags as {activity_id: tag} — for build_coaching_context bulk-injection."""
    return {r[0]: r[1] for r in conn.execute("SELECT activity_id, tag FROM user_activity_tags").fetchall()}


# ── Activity comments ─────────────────────────────────────────────────────────

def comment_get(conn: sqlite3.Connection, activity_id: int) -> str:
    row = conn.execute("SELECT comment FROM user_activity_comments WHERE activity_id = ?",
                       (activity_id,)).fetchone()
    return row[0] if row else ""


def comment_set(conn: sqlite3.Connection, activity_id: int, comment: str) -> None:
    if not comment:
        conn.execute("DELETE FROM user_activity_comments WHERE activity_id = ?", (activity_id,))
        return
    conn.execute("""
        INSERT INTO user_activity_comments (activity_id, comment, updated_at) VALUES (?,?,?)
        ON CONFLICT(activity_id) DO UPDATE SET comment = excluded.comment, updated_at = excluded.updated_at
    """, (activity_id, comment, _now_iso()))


def comments_all(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute(
        "SELECT activity_id, comment FROM user_activity_comments").fetchall()}


# ── Coaching report (singleton) ───────────────────────────────────────────────

def coaching_report_get(conn: sqlite3.Connection) -> dict | None:
    """Return {content, horizon, generated_at} or None if no report exists."""
    row = conn.execute(
        "SELECT content, horizon, generated_at FROM user_coaching_report WHERE id = 1"
    ).fetchone()
    if not row:
        return None
    return {"content": row[0], "horizon": row[1], "generated_at": row[2]}


def coaching_report_set(conn: sqlite3.Connection, content: str, horizon: str = "24h") -> None:
    """UPSERT — only the latest report is kept (singleton row id=1)."""
    conn.execute("""
        INSERT INTO user_coaching_report (id, content, horizon, generated_at)
        VALUES (1, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
          content = excluded.content,
          horizon = excluded.horizon,
          generated_at = excluded.generated_at
    """, (content, horizon, _now_iso()))


def coaching_report_clear(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM user_coaching_report WHERE id = 1")


# ── Overall coach chat ────────────────────────────────────────────────────────
# Summary state is kept in user_app_config under 3 dedicated keys:
#   chat_overall_summary, chat_overall_summary_through_idx, chat_overall_updated_at
# Singleton thread → no need for a separate meta table.

_OVERALL_SUMMARY_KEY     = "chat_overall_summary"
_OVERALL_THROUGH_KEY     = "chat_overall_summary_through_idx"
_OVERALL_UPDATED_AT_KEY  = "chat_overall_updated_at"


def overall_chat_load(conn: sqlite3.Connection) -> dict:
    """Return {messages, summary, summary_through_idx} — same shape as the
    legacy gd.load_overall_chat() to make the caller migration painless."""
    msgs = [
        {"role": r[0], "content": r[1], "model": r[2], "ts": r[3]}
        for r in conn.execute(
            "SELECT role, content, model, ts FROM chat_overall ORDER BY msg_index"
        ).fetchall()
    ]
    summary = config_get(conn, _OVERALL_SUMMARY_KEY, "") or ""
    through_str = config_get(conn, _OVERALL_THROUGH_KEY, "0") or "0"
    return {
        "messages":            msgs,
        "summary":             summary,
        "summary_through_idx": int(through_str),
    }


def overall_chat_append(conn: sqlite3.Connection, role: str, content: str,
                        model: str | None = None) -> int:
    """Append a single message; returns the assigned msg_index."""
    nxt = conn.execute(
        "SELECT COALESCE(MAX(msg_index) + 1, 0) FROM chat_overall"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO chat_overall (msg_index, role, content, model, ts) VALUES (?,?,?,?,?)",
        (nxt, role, content, model, _now_iso()),
    )
    config_set(conn, _OVERALL_UPDATED_AT_KEY, _now_iso())
    return nxt


def overall_chat_replace(conn: sqlite3.Connection, messages: list[dict],
                         summary: str = "", summary_through_idx: int = 0) -> None:
    """Bulk-rewrite the entire thread. Used by the migration script and any
    place that previously wrote the whole list (e.g. chat-summary refresh)."""
    conn.execute("DELETE FROM chat_overall")
    rows = [
        (i, m.get("role", ""), m.get("content", ""), m.get("model"),
         m.get("ts") or _now_iso())
        for i, m in enumerate(messages)
    ]
    if rows:
        conn.executemany(
            "INSERT INTO chat_overall (msg_index, role, content, model, ts) VALUES (?,?,?,?,?)",
            rows,
        )
    config_set(conn, _OVERALL_SUMMARY_KEY,    summary)
    config_set(conn, _OVERALL_THROUGH_KEY,    str(summary_through_idx))
    config_set(conn, _OVERALL_UPDATED_AT_KEY, _now_iso())


def overall_chat_clear(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM chat_overall")
    for k in (_OVERALL_SUMMARY_KEY, _OVERALL_THROUGH_KEY, _OVERALL_UPDATED_AT_KEY):
        config_set(conn, k, None)


# ── Per-activity review chat ──────────────────────────────────────────────────

def review_chat_load(conn: sqlite3.Connection, activity_id: int) -> dict:
    """Return {messages, summary, summary_through_idx} — same shape as
    legacy gd.load_review_chat(aid)."""
    msgs = [
        {"role": r[0], "content": r[1], "model": r[2], "ts": r[3]}
        for r in conn.execute("""
            SELECT role, content, model, ts FROM chat_review
             WHERE activity_id = ? ORDER BY msg_index
        """, (activity_id,)).fetchall()
    ]
    meta = conn.execute("""
        SELECT summary, summary_through_idx FROM chat_review_meta WHERE activity_id = ?
    """, (activity_id,)).fetchone()
    return {
        "messages":            msgs,
        "summary":             (meta[0] if meta else "") or "",
        "summary_through_idx": (meta[1] if meta else 0) or 0,
    }


def review_chat_append(conn: sqlite3.Connection, activity_id: int,
                       role: str, content: str, model: str | None = None) -> int:
    nxt = conn.execute("""
        SELECT COALESCE(MAX(msg_index) + 1, 0) FROM chat_review WHERE activity_id = ?
    """, (activity_id,)).fetchone()[0]
    conn.execute("""
        INSERT INTO chat_review (activity_id, msg_index, role, content, model, ts)
        VALUES (?,?,?,?,?,?)
    """, (activity_id, nxt, role, content, model, _now_iso()))
    # Touch updated_at so review-list views can sort by recent-conversation
    conn.execute("""
        INSERT INTO chat_review_meta (activity_id, updated_at)
        VALUES (?,?)
        ON CONFLICT(activity_id) DO UPDATE SET updated_at = excluded.updated_at
    """, (activity_id, _now_iso()))
    return nxt


def review_chat_replace(conn: sqlite3.Connection, activity_id: int,
                        messages: list[dict], summary: str = "",
                        summary_through_idx: int = 0) -> None:
    """Bulk-rewrite one activity's chat thread + summary."""
    conn.execute("DELETE FROM chat_review WHERE activity_id = ?", (activity_id,))
    rows = [
        (activity_id, i, m.get("role", ""), m.get("content", ""), m.get("model"),
         m.get("ts") or _now_iso())
        for i, m in enumerate(messages)
    ]
    if rows:
        conn.executemany("""
            INSERT INTO chat_review (activity_id, msg_index, role, content, model, ts)
            VALUES (?,?,?,?,?,?)
        """, rows)
    conn.execute("""
        INSERT INTO chat_review_meta (activity_id, summary, summary_through_idx, updated_at)
        VALUES (?,?,?,?)
        ON CONFLICT(activity_id) DO UPDATE SET
          summary             = excluded.summary,
          summary_through_idx = excluded.summary_through_idx,
          updated_at          = excluded.updated_at
    """, (activity_id, summary, summary_through_idx, _now_iso()))


def review_chat_clear(conn: sqlite3.Connection, activity_id: int) -> None:
    conn.execute("DELETE FROM chat_review WHERE activity_id = ?", (activity_id,))
    conn.execute("DELETE FROM chat_review_meta WHERE activity_id = ?", (activity_id,))
