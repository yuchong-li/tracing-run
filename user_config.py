"""User training config — constants + pure helpers.

Storage lives entirely in db.* (SQLite-backed). This module's surface is:

- Taxonomy constants: PHASES, ACTIVITY_TAG_KEYS, ACTIVITY_TAG_TO_BUILDER,
  ACTIVITY_TAG_TO_PROMPT, PHASE_COACH_HINT (used by UI dropdowns + builder
  dispatch + chat sys prompt assembly).
- load() — snapshot SQLite state into a dict in the legacy shape. Used by
  callers that need multi-field reads (build_context, chat sys-prompt
  builder, sidebar). Setters do NOT live here — write paths call db.* directly.
- Pure functions over the snapshot dict: is_onboarded, next_race,
  build_context, get_activity_tag, get_activity_comment, get_personal_note,
  list_insights. These are 1-line accessors / pure logic — kept for
  caller-side readability.

Write paths use db.* directly:
  - db.config_set(c, key, value) for phase / personal_note / onboarded
  - db.tag_set / db.comment_set for per-activity edits
  - db.insights_add / db.insights_update / db.insights_delete for insights
  - db.races_add / db.races_update / db.races_delete for races
"""

from datetime import date

import db

PHASES = {
    "race_prep":   "🏁 备赛",
    "maintenance": "💪 维持期",
    "recovery":    "😌 恢复中",
}

# ── Activity-tag taxonomy + Builder dispatch ────────────────────────────────
# Single source of truth for the tag selectbox in the review UI AND for the
# review_builders.dispatch() routing. Tags are STABLE STORAGE KEYS (English,
# snake_case) — language-neutral. The human-readable label is rendered via
# i18n (`t(f"tag.{key}")`) at display time. The empty string is the sentinel
# for "untagged" (rendered via `t("tag.empty")`).
#
# DB migration #8 converted historic Chinese-string tag values to these keys.

ACTIVITY_TAG_KEYS = [
    "",                  # sentinel for untagged (label via tag.empty)
    "aerobic",           # merged aerobic_recovery + aerobic_base
    "steady",            # High Z2 → mid/high Z3 cruise
    "long_run",
    "tempo",
    "threshold",
    "intervals",
    "hill",
    "trail",
    "race",
    "other",
]

# Tag key → Builder class name. The actual class lookup happens in
# review_builders.dispatch(); this maps stable keys to the builder names that
# dispatch() resolves. For unmapped keys (or "" / "other") the dispatcher
# falls back to DefaultBuilder.
ACTIVITY_TAG_TO_BUILDER: dict[str, str] = {
    "aerobic":          "AerobicBuilder",
    "steady":           "AerobicBuilder",    # same builder; steady prompt reframes the reading
    "long_run":         "LongRunBuilder",
    "tempo":            "TempoBuilder",
    "threshold":        "TempoBuilder",      # same builder; prompt distinguishes target
    "intervals":        "IntervalBuilder",
    "hill":             "HillBuilder",       # interval-style structure + grade overlay
    "trail":            "TrailBuilder",
    "race":             "RaceBuilder",
    # "" (untagged) and "other" → not in map → DefaultBuilder fallback in dispatch()
}

# Tag key → prompt file basename (without `.md` / lang suffix; looked up via
# load_prompt()). Decoupled from ACTIVITY_TAG_TO_BUILDER because one builder
# can serve multiple tags with different LLM framings (AerobicBuilder serves
# both aerobic + steady, each with its own prompt — same data, opposite reading
# of the HR-ceiling block). Tags not in this map fall back to the generic
# `review_report` prompt and are flagged beta in the UI.
ACTIVITY_TAG_TO_PROMPT: dict[str, str] = {
    "aerobic":          "review_report_aerobic",
    "steady":           "review_report_steady",
    "long_run":         "review_report_long_run",
    "tempo":            "review_report_tempo",
    "threshold":        "review_report_threshold",
    "intervals":        "review_report_intervals",
    "hill":             "review_report_hill",
    "race":             "review_report_race",
    "trail":            "review_report_trail",
}

PHASE_COACH_HINT = {
    "race_prep":   "用户当前处于备赛阶段，建议围绕赛事倒计时给出周期化训练安排。",
    "maintenance": "用户当前处于维持期，无近期目标赛事，保持有氧基础即可，不要推高训练量。",
    "recovery":    "用户当前处于主动恢复期，需降负荷，优先睡眠和恢复，避免高强度建议。",
}


# ── Snapshot loader ──────────────────────────────────────────────────────────

def load() -> dict:
    """Return a snapshot of user state from SQLite, shaped like the legacy
    JSON dict so build_context + chat sys-prompt builder + sidebar can keep
    consuming a single dict.

    activity_tags / activity_comments use string activity-id keys to match
    the legacy shape (the JSON dict was keyed by str(activity_id))."""
    with db.connect() as c:
        return {
            "phase":              db.config_get(c, "phase", "maintenance"),
            "personal_note":      db.config_get(c, "personal_note", "") or "",
            "onboarded":          db.config_get(c, "onboarded", "0") == "1",
            "races":              db.races_list(c),
            "coach_insights":     db.insights_list(c),
            "activity_tags":      {str(aid): tag for aid, tag in db.tags_all(c).items()},
            "activity_comments":  {str(aid): cmt for aid, cmt in db.comments_all(c).items()},
        }


# ── Pure cfg-dict accessors (1-line sugar; kept for readability) ────────────

def is_onboarded(cfg: dict) -> bool:
    return bool(cfg.get("onboarded", False))


def get_activity_tag(cfg: dict, activity_id) -> str:
    return cfg.get("activity_tags", {}).get(str(activity_id), "")


def get_activity_comment(cfg: dict, activity_id) -> str:
    return cfg.get("activity_comments", {}).get(str(activity_id), "")


def get_personal_note(cfg: dict) -> str:
    return cfg.get("personal_note", "") or ""


def list_insights(cfg: dict) -> list[dict]:
    return cfg.get("coach_insights", []) or []


# ── Race / coach context (pure functions over cfg dict) ──────────────────────

def next_race(cfg: dict) -> dict | None:
    """Return the nearest upcoming race, or None."""
    today = date.today()
    upcoming = sorted(
        [r for r in cfg.get("races", [])
         if r.get("date") and date.fromisoformat(r["date"]) >= today],
        key=lambda r: r["date"],
    )
    return upcoming[0] if upcoming else None


def build_context(cfg: dict) -> str:
    """Return a context string to append to the coaching prompt."""
    phase = cfg.get("phase", "maintenance")
    hint  = PHASE_COACH_HINT.get(phase, "")
    today = date.today()

    if phase == "race_prep":
        races = cfg.get("races", [])
        upcoming = sorted(
            [r for r in races if r.get("date")],
            key=lambda r: r["date"],
        )
        if not upcoming:
            # Distinct hint for the "race_prep but no race set" case so the
            # AI doesn't try to follow PHASE_COACH_HINT's "围绕赛事倒计时"
            # directive when there's no race to count down to.
            return (
                "【训练阶段】备赛（⚠️ 用户已选择备赛阶段但尚未指定目标赛事）\n"
                "教练提示：在用户添加具体赛事之前，无法给出真正针对性的周期化安排。"
                "推荐时请优先提醒用户在「📑 训练档案 → 🏁 目标比赛」中添加目标赛事；"
                "在用户添加之前，按通用有氧基础 + 渐进负荷的方式推荐，"
                "不要硬编一个倒计时或假装存在某场比赛。"
            )

        lines = [f"【训练阶段】备赛\n教练提示：{hint}", "【目标比赛】"]
        for r in upcoming:
            race_date   = date.fromisoformat(r["date"])
            days        = (race_date - today).days
            dist_str    = f"{r['distance_km']}km"    if r.get("distance_km") else ""
            terrain_str = r.get("terrain", "")
            goal_str    = f"目标 {r['goal_time']}"   if r.get("goal_time")   else ""
            day_str     = f"距今 {days} 天" if days >= 0 else f"已过去 {-days} 天"
            meta        = " · ".join(p for p in [dist_str, terrain_str, goal_str, day_str] if p)
            lines.append(f"- {r['name']}（{r['date']}）{' · ' + meta if meta else ''}")
            if r.get("notes"):
                lines.append(f"  备注：{r['notes']}")
        return "\n".join(lines)

    elif phase == "maintenance":
        return f"【训练阶段】维持期\n教练提示：{hint}"
    else:
        return f"【训练阶段】恢复期\n教练提示：{hint}"
