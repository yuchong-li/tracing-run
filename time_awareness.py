"""Time-awareness for LLM context.

Two responsibilities:
  1. now_block()           — ready-to-append system-prompt block telling the
                             LLM what "now" is (date, weekday, time, TZ).
  2. annotate_history(msgs)— prefix user messages with their send time so
                             the LLM can reason about gaps and deltas. Assistant
                             messages are left untouched (avoid teaching the
                             model to echo timestamps in its own replies).

Both rely on the container's wall clock (`datetime.now()`), which means TZ
correctness is upstream — see docker-compose.yml `TZ` env and .env.example.
"""

from datetime import datetime

import i18n


def now_block(now: datetime | None = None) -> str:
    """Return a markdown block ready to append to a system prompt.

    Caller appends verbatim — string already starts with two newlines so it
    visually separates from the preceding block."""
    n = now or datetime.now()
    weekday = i18n.t(f"time_awareness.weekday_{n.weekday()}")
    body = i18n.t(
        "time_awareness.now_format",
        date=n.strftime("%Y-%m-%d"),
        weekday=weekday,
        time=n.strftime("%H:%M"),
        tz=n.astimezone().strftime("%Z") or "local",
    )
    return i18n.t("time_awareness.now_header") + body


def annotate_history(msgs: list[dict]) -> list[dict]:
    """Return a copy of msgs with [time] prefixed onto user-role content.

    Only the role/content keys are emitted (matches what the LLM call expects).
    Messages without a parseable `ts` field pass through unchanged."""
    out: list[dict] = []
    for m in msgs:
        role = m.get("role", "")
        content = m.get("content", "")
        if role == "user":
            ts = _parse_ts(m.get("ts"))
            if ts is not None:
                content = f"[{ts.strftime('%Y-%m-%d %H:%M')}] {content}"
        out.append({"role": role, "content": content})
    return out


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
