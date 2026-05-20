"""tracing.run — FastHTML single-file app.

Backend modules (db / garmin_data / garmin_auth / ui/llm / review_builders /
review_tools / prompts) hold the data, builder, and LLM logic; this file is
the UI layer.

Layout:
  ┌──────────┬──────────────────────────┐
  │ 对话教练  │                          │
  │ ─────    │  current chat thread     │
  │ 同步     │  (overall OR per-activity│
  │ 设置     │   review chat)           │
  │ 复盘训练 │                          │
  │  · run 1 │                          │
  │  · run 2 │                          │
  │  ...     │                          │
  └──────────┴──────────────────────────┘
   280px        flex-1
   (mobile: drawer + hamburger)

Env:
  DATA_DIR    state root (default: ./data/local for local dev; docker sets /data)
  APP_PORT    listen port (default: 8507)
  APP_PASSWORD  optional gate (empty = no auth; required in docker-compose)

Local dev (using a writeable scratch dir):
  cd /path/to/repo
  DATA_DIR=$PWD/data/local venv/bin/python3 coach_app.py
"""

import asyncio
import json
import os
import queue
import random
import re
import shutil
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone

# ── DATA_DIR setup must happen BEFORE importing db / garmin_data ──
_DATA_DIR = os.environ.get("DATA_DIR",
                            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "data/local"))
os.makedirs(os.path.join(_DATA_DIR, "cache"), exist_ok=True)
# Re-export the resolved absolute path so sub-modules (db / garmin_data /
# garmin_auth) all see the same value even when DATA_DIR was unset.
os.environ["DATA_DIR"] = _DATA_DIR

# Local-dev convenience: if the chosen DATA_DIR is empty AND a sibling seed
# dir exists, seed from it so a fresh checkout has something to chat with.
# Inert in docker (no sibling dir present). Override with LOCAL_SEED_DIR env
# var if your local seed lives somewhere else.
_LOCAL_SEED_DIR = os.environ.get(
    "LOCAL_SEED_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data/seed"),
)
_DB_PATH = os.path.join(_DATA_DIR, "cache/garmin.db")

# App version — read from VERSION file at startup. prod.sh build writes
# this file before each build so the running container reports the tag it
# was shipped as. Local dev (no VERSION file) falls back to "dev".
def _read_version() -> str:
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "dev"
    except FileNotFoundError:
        return "dev"
_APP_VERSION = _read_version()
if not os.path.exists(_DB_PATH) and os.path.exists(os.path.join(_LOCAL_SEED_DIR, "cache/garmin.db")):
    print(f"[coach] seeding local dev DATA_DIR → {_DATA_DIR}")
    shutil.copy2(os.path.join(_LOCAL_SEED_DIR, "cache/garmin.db"), _DB_PATH)
    if os.path.exists(os.path.join(_LOCAL_SEED_DIR, ".garth_session")):
        shutil.copytree(os.path.join(_LOCAL_SEED_DIR, ".garth_session"),
                        os.path.join(_DATA_DIR, ".garth_session"),
                        dirs_exist_ok=True)

# Make existing modules importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fasthtml.common import (
    fast_app, serve, Title, Body, Div, P, Span, A, Button, Input, Textarea,
    Form, Label, Select, Option, H1, H2, H3, H4, Hr, Br, Style, Script, Link,
    Meta, NotStr, Ul, Li, Header, Main, Aside, Section, Nav, Details, Summary,
    Dialog,
)
from starlette.responses import StreamingResponse, RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

import db
import garmin_data as gd
import garmin_auth
from ui.llm import (
    DEFAULT_MODEL, LLM_BASE, LLM_API_KEY, llm_stream, llm_stream_with_tools,
    coach_sys, review_chat_sys, load_prompt, extract_chat_addendum,
)
# Chat compression + ✨ 提炼 button helper.
from chat_helpers import (
    _distill_with_llm, _refine_personal_note_with_llm, maybe_resummarize,
)
# Date-background builder (surrounding-activities context).
from coach_helpers import _build_date_background
import review_tools as rt
import user_config as uc
from review_builders import dispatch as _dispatch_builder
import report_jobs
import i18n
import time_awareness as ta

# ── In-process state for async background ops ─────────────────────────────────
_sync_state: dict = {"running": False, "msg": "", "frac": 0.0, "error": ""}
_sync_lock = threading.Lock()

# Stream tokens: maps short opaque token → (route_kind, activity_id|None)
# Lets the SSE GET endpoint find the right chat to stream from.
_pending_streams: dict[str, dict] = {}

ACTIVITY_TAG_KEYS = uc.ACTIVITY_TAG_KEYS

# Sidebar tag-chip colors — three tiers along recovery-cost (cool → warm),
# matching the universal Garmin / Polar / Strava HR-zone metaphor so a glance
# at a week's tags reflects accumulated training stress.
#   blue  = low (recovery / aerobic base)
#   amber = moderate (long / tempo / threshold / trail)
#   red   = high (intervals / hill / race)
_TAG_TIER_BLUE  = "bg-blue-900/50 text-blue-200"
_TAG_TIER_AMBER = "bg-amber-900/50 text-amber-200"
_TAG_TIER_RED   = "bg-red-900/50 text-red-200"
_TAG_TIER_GRAY  = "bg-gray-700/50 text-gray-300"
_TAG_COLOR: dict[str, str] = {
    "aerobic_recovery": _TAG_TIER_BLUE,
    "aerobic_base":     _TAG_TIER_BLUE,
    "long_run":         _TAG_TIER_RED,
    "tempo":            _TAG_TIER_AMBER,
    "threshold":        _TAG_TIER_AMBER,
    "trail":            _TAG_TIER_AMBER,
    "intervals":        _TAG_TIER_RED,
    "hill":             _TAG_TIER_RED,
    "race":             _TAG_TIER_RED,
    "other":            _TAG_TIER_GRAY,
}


def _tag_label(tag: str) -> str:
    """Render a stable tag key as the user-locale display label.
    Empty string → tag.empty ('— Untagged —' / '— 未标记 —')."""
    return i18n.t("tag.empty") if not tag else i18n.t(f"tag.{tag}")

# Follow-up suggestion chips. Appended to every report + chat-followup system
# prompt so the LLM emits 3 contextual follow-up questions at the very end of
# each response, wrapped in a sentinel block that the client can parse out.
# Streaming bubble hides everything from the open tag onwards so the user
# never visually sees the raw JSON; on `done`, JS extracts + renders chips.
def _follow_ups_instruction() -> str:
    """Locale-aware follow-ups suffix appended to typed-report user messages."""
    return i18n.t("prompt.follow_ups_instruction")


# ── Starter chips for empty /chat/overall ────────────────────────────────────
# When the user opens the overall coach chat with no message history yet, the
# biggest UX hurdle is "what do I even ask?". We populate 3 grounded
# inspiration chips: A=specific recent activity, B=trend/comparison, C=forward
# planning. Each must reference real dates / activity names / race names —
# never the generic "最近怎么样" baseline.
#
# Generation: blocking LLM call (~1-2s) gated behind an htmx auto-trigger on
# page load, with a pulsing skeleton placeholder while the call runs. Cached
# in user_app_config['overall_starter_chips'] for 3 hours OR until the
# activity row count changes (a new sync brought new activities → previous
# chip suggestions are stale).
_STARTER_CHIPS_CACHE_KEY    = "overall_starter_chips"
_STARTER_CHIPS_TTL_SECONDS  = 3 * 3600

def _starter_chip_system_prompt() -> str:
    """Locale-aware system prompt for the starter-chip generator."""
    return i18n.t("starter.system_prompt")


def _build_starter_chip_context(conn) -> str:
    """Compact context blob for the chip-generator LLM. ~500-1000 tokens —
    breadth, not depth: 7 days of activities + last week summary + upcoming
    races + pinned insights. No 1Hz time-series, no 6-month trends."""
    today = date.today()
    parts = [i18n.t("starter.context_header", date=today.isoformat())]

    parts.append(i18n.t("starter.recent_activities_header"))
    acts = db.get_recent_activities(conn, days=7)
    if not acts:
        parts.append(i18n.t("starter.empty"))
    else:
        tags     = db.tags_all(conn)
        comments = db.comments_all(conn)
        for a in acts:
            aid  = a.get("activityId")
            tag_key = tags.get(aid, "") or ""
            tag  = _tag_label(tag_key) if tag_key else i18n.t("starter.untagged")
            km   = (a.get("distance") or 0) / 1000
            mins = (a.get("duration") or 0) / 60
            hr   = a.get("averageHR")
            spd  = a.get("averageSpeed")
            pace = gd.format_pace(spd) if spd else "—"
            ds   = (a.get("startTimeLocal") or "")[:10]
            name = a.get("activityName", "") or ""
            parts.append(i18n.t(
                "starter.activity_line",
                date=ds, tag=tag, name=name,
                km=km, mins=mins, pace=pace,
                hr=int(hr) if hr else "—",
            ))
            c = comments.get(aid)
            if c:
                parts.append(i18n.t("starter.activity_comment", comment=c.strip()[:120]))

    parts.append(i18n.t("starter.weekly_header"))
    weeks = db.get_weekly_summary(conn, weeks=2)
    if weeks:
        last = weeks[-1]
        parts.append(i18n.t(
            "starter.weekly_line",
            week=last["week"],
            run_km=last["runKm"], ride_km=last["rideKm"],
            acts=last["activities"], load=last["weeklyLoad"],
        ))
    else:
        parts.append(i18n.t("starter.no_data"))

    parts.append(i18n.t("starter.upcoming_races_header"))
    upcoming = []
    for r in db.races_list(conn):
        rd = r.get("date")
        if not rd:
            continue
        try:
            d = date.fromisoformat(rd)
        except (ValueError, TypeError):
            continue
        days_away = (d - today).days
        if 0 <= days_away <= 84:
            upcoming.append((days_away, r))
    if upcoming:
        upcoming.sort()
        for days_away, r in upcoming[:3]:
            parts.append(i18n.t(
                "starter.race_line",
                date=r.get("date"),
                name=r.get("name", ""),
                km=r.get("distance_km", "—"),
                terrain=r.get("terrain", "—"),
                days=days_away,
            ))
    else:
        parts.append(i18n.t("starter.empty"))

    parts.append(i18n.t("starter.pinned_insights_header"))
    insights = db.insights_list(conn)
    if insights:
        for i in insights[:10]:
            t = (i.get("text") or "").strip()
            if t:
                parts.append(f"- {t}")
    else:
        parts.append(i18n.t("starter.empty"))

    return "\n".join(parts)


def _get_or_generate_starter_chips(conn) -> list[str]:
    """Returns 3 starter chip strings. Cached in user_app_config under
    `overall_starter_chips` as JSON `{generated_at, activity_count_at_gen,
    locale_at_gen, chips}`. Regenerates if no cache, age > 3h, activities
    row count changed since last gen, OR the user switched locale
    (otherwise we'd serve EN chips after a switch to ZH and vice versa)."""
    raw       = db.config_get(conn, _STARTER_CHIPS_CACHE_KEY)
    cur_count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    cur_locale = i18n.current_locale()
    if raw:
        try:
            cached = json.loads(raw)
            gen_at = datetime.fromisoformat(cached["generated_at"])
            age    = (datetime.now(timezone.utc) - gen_at).total_seconds()
            chips  = cached.get("chips") or []
            if (age < _STARTER_CHIPS_TTL_SECONDS
                    and cached.get("activity_count_at_gen") == cur_count
                    and cached.get("locale_at_gen") == cur_locale
                    and len(chips) == 3):
                return [str(c) for c in chips]
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass

    ctx_md = _build_starter_chip_context(conn)
    msgs = [
        {"role": "system", "content": _starter_chip_system_prompt()},
        {"role": "user",   "content": ctx_md},
    ]
    try:
        raw_resp = "".join(llm_stream(msgs, DEFAULT_MODEL))
    except Exception:
        raw_resp = ""

    chips: list[str] = []
    m = re.search(r"\[[\s\S]*?\]", raw_resp)
    if m:
        try:
            parsed = json.loads(m.group(0))
            if isinstance(parsed, list):
                chips = [str(x).strip() for x in parsed if str(x).strip()][:3]
        except (json.JSONDecodeError, TypeError):
            pass
    # Fallback to generic-but-actionable defaults if LLM mis-formats.
    if len(chips) != 3:
        chips = [
            i18n.t("starter.fallback_chip_1"),
            i18n.t("starter.fallback_chip_2"),
            i18n.t("starter.fallback_chip_3"),
        ]

    db.config_set(conn, _STARTER_CHIPS_CACHE_KEY, json.dumps({
        "generated_at":          datetime.now(timezone.utc).isoformat(),
        "activity_count_at_gen": cur_count,
        "locale_at_gen":         cur_locale,
        "chips":                 chips,
    }, ensure_ascii=False))
    return chips


def _starter_chip_skeleton():
    """3 pulsing chip-shaped placeholders + a small subtitle. Lives inside
    #follow-up-chips; replaced wholesale by the htmx swap once
    /chat/overall/starter-chips returns."""
    return Div(
        Div(
            Div(cls="h-7 w-28 bg-gray-800 rounded-full animate-pulse"),
            Div(cls="h-7 w-32 bg-gray-800 rounded-full animate-pulse"),
            Div(cls="h-7 w-24 bg-gray-800 rounded-full animate-pulse"),
            cls="flex gap-2 mb-1.5",
        ),
        Div(i18n.t("ui.starter_chips_loading"),
            cls="text-[11px] text-gray-500 italic"),
        cls="w-full",
    )


# Plotly for the activity stats charts. Pure-function chart helpers
# (_elapsed_minutes / _pace_ticks) live in coach_helpers.py.
import plotly.graph_objects as go
from coach_helpers import _elapsed_minutes, _pace_ticks

# Dark theme so charts read on the gray-950 background. Margins tightened
# for narrow mobile width.
_PLOTLY_LAYOUT_BASE = dict(
    template="plotly_dark",
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
    margin=dict(l=8, r=8, t=10, b=24),
    font=dict(size=10, color="#d1d5db"),
)
_PLOTLY_CFG = {"displayModeBar": False, "responsive": True}
# Static config — disables hover / zoom / pan / drag, so horizontal touch
# swipes pass cleanly through to the parent scroll-snap carousel instead of
# being captured by Plotly's gesture handler.
_PLOTLY_CFG_STATIC = {"displayModeBar": False, "staticPlot": True, "responsive": True}


def _summarize_tool_call(name: str, args: dict) -> str:
    """Domain-aware concise label for the tool note shown above the bubble.
    Replaces the raw JSON args dump (which wraps to 4-5 lines on mobile and
    eats vertical real estate). The full args still ride along in the event
    payload for the expand-on-click view."""
    if name == "get_raw_window_by_time":
        s = int(args.get("start_seconds") or 0)
        e = int(args.get("end_seconds") or 0)
        ts = lambda x: f"{x // 60}:{x % 60:02d}"
        chs = "/".join(args.get("channels") or []) or i18n.t("ui.tool_badge.default_channel")
        return i18n.t("ui.tool_badge.window_time", start=ts(s), end=ts(e), channels=chs)
    if name == "get_raw_window_by_distance":
        s = int(args.get("start_meters") or 0)
        e = int(args.get("end_meters") or 0)
        chs = "/".join(args.get("channels") or []) or i18n.t("ui.tool_badge.default_channel")
        return f"{s}m–{e}m · {chs}"
    if name == "get_window_stats":
        if args.get("start_seconds") is not None:
            return i18n.t("ui.tool_badge.stats_window", start=args["start_seconds"], end=args["end_seconds"])
        if args.get("start_meters") is not None:
            return f"stats · {args['start_meters']}m–{args['end_meters']}m"
        if args.get("lap_index") is not None:
            return f"stats · Lap {args['lap_index']}"
        return "stats"
    if name == "find_activities":
        bits = []
        if args.get("tag"):           bits.append(f"tag={args['tag']}")
        if args.get("name_contains"): bits.append(f"name~{args['name_contains']}")
        if args.get("date_from"):     bits.append(f"≥{args['date_from']}")
        if args.get("date_to"):       bits.append(f"≤{args['date_to']}")
        return " · ".join(bits) or i18n.t("ui.tool_badge.recent_default")
    if name == "get_activity_report":
        return f"aid {args.get('activity_id', '?')}"
    if name == "get_metric_trend":
        return i18n.t("ui.tool_badge.metric_window", metric=args.get("metric", "?"), days=args.get("days", 90))
    # Fallback: tiny snippet
    return json.dumps(args, ensure_ascii=False)[:40]


def _tool_call_sse_event(payload: dict) -> str:
    """Build the SSE `tool` event sent to the client. Structured payload
    lets the client render a compact summary + expandable args panel."""
    body = {
        "name":    payload["name"],
        "summary": _summarize_tool_call(payload["name"], payload.get("args") or {}),
        "args":    payload.get("args") or {},
    }
    return f"event: tool\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"


def _fig_to_html(fig: go.Figure, *, static: bool = True) -> str:
    """Embed Plotly figure as a self-contained HTML fragment. Relies on the
    page-head Plotly preload (see Layout headers) — `include_plotlyjs=False`
    avoids re-injecting the CDN script per chart, which races the inline
    `Plotly.newPlot()` calls on htmx swaps and leaves charts blank until a
    full refresh. `static=True` makes the chart inert — required for the
    swipeable carousel context."""
    return fig.to_html(
        include_plotlyjs=False,
        full_html=False,
        config=_PLOTLY_CFG_STATIC if static else _PLOTLY_CFG,
        default_height="260px",
    )


def _plotlyjs_cdn_url() -> str:
    """Derive the plotly.js CDN URL that matches the installed plotly-Python
    version. We can't hardcode a version because the Python package emits
    figure JSON keyed to its own bundled plotly.js version, and a mismatch
    breaks newer trace types (e.g. Scattermap with style="carto-darkmatter"
    needs plotly.js ≥ 2.31)."""
    fig = go.Figure()
    html = fig.to_html(include_plotlyjs="cdn", full_html=False)
    m = re.search(r'src="(https://cdn\.plot\.ly/plotly-[0-9.]+\.min\.js)"', html)
    return m.group(1) if m else "https://cdn.plot.ly/plotly-latest.min.js"


_PLOTLY_CDN_URL = _plotlyjs_cdn_url()

# ── App + headers ──────────────────────────────────────────────────────────────
app, rt_route = fast_app(
    pico=False,
    live=False,
    hdrs=(
        Meta(name="viewport", content="width=device-width, initial-scale=1, viewport-fit=cover"),
        Meta(name="theme-color", content="#111827"),
        # Inline SVG favicon — the 🛰️ glyph sits inside an SVG <text>, served
        # as a data: URI so we don't need a static file. Modern browsers
        # (Chrome/Safari/Firefox) all render emoji this way; falls back to a
        # square if the OS lacks the glyph.
        Link(rel="icon", href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🛰️</text></svg>"),
        # iOS "Add to Home Screen" — without this, the home-screen icon
        # falls back to a screenshot or the first letter of the title (the
        # white "T" some users currently see). 180x180 is iOS's preferred
        # apple-touch-icon size; SVG works on iOS 14+.
        Link(rel="apple-touch-icon", href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 180 180'><rect width='180' height='180' rx='40' fill='%23111827'/><text x='90' y='133' text-anchor='middle' font-size='110'>🛰️</text></svg>"),
        # Standalone-mode hints — make the launched-from-home-screen webapp
        # render without Safari chrome (full screen) and use a real title
        # under the icon (otherwise iOS would just show "T" from the page <title>).
        Meta(name="apple-mobile-web-app-capable", content="yes"),
        Meta(name="apple-mobile-web-app-status-bar-style", content="black-translucent"),
        Meta(name="apple-mobile-web-app-title", content="tracing.run"),
        # Web app manifest — declares scope="/" so iOS keeps every route
        # inside the standalone PWA window. Without this, navigating to a
        # path that doesn't match iOS's implicit scope (e.g. /settings when
        # added from /chat/overall) pops a Safari in-app browser modal.
        Link(rel="manifest", href="/manifest.json"),
        Script(src="https://cdn.tailwindcss.com"),
        Script(src="https://unpkg.com/htmx.org@1.9.10"),
        Script(src="https://cdn.jsdelivr.net/npm/marked@12/marked.min.js"),
        # Preload Plotly once at app boot so subsequent activity-page htmx
        # swaps don't race the per-chart CDN fetch (the inline Plotly.newPlot
        # scripts emitted by `_fig_to_html` would fire before Plotly was
        # defined, leaving charts blank until a full refresh). Each chart now
        # uses include_plotlyjs=False — relies on this single global load.
        # URL is derived from the installed plotly-Python version so the
        # script bundle matches the figure JSON the python lib emits.
        Script(src=_PLOTLY_CDN_URL),
        Style(NotStr("""
          html, body { background:#0b0f17; color:#e5e7eb; font-family: -apple-system,'PingFang SC','Helvetica Neue',sans-serif; }
          ::-webkit-scrollbar { width:6px; height:6px; }
          ::-webkit-scrollbar-thumb { background:#374151; border-radius:3px; }
          .prose-coach h1,.prose-coach h2,.prose-coach h3 { font-weight:600; margin:.6rem 0 .3rem; color:#f3f4f6; }
          .prose-coach h1 { font-size:1.1rem; }
          .prose-coach h2 { font-size:1.02rem; }
          .prose-coach h3 { font-size:.95rem; }
          .prose-coach p  { margin:.4rem 0; line-height:1.55; }
          .prose-coach ul,.prose-coach ol { margin:.4rem 0 .4rem 1.2rem; }
          .prose-coach li { margin:.2rem 0; }
          .prose-coach code { background:#1f2937; padding:0 .3em; border-radius:3px; font-size:.88em; }
          .prose-coach pre { background:#0f172a; padding:.6rem .8rem; border-radius:6px; overflow-x:auto; font-size:.85em; }
          .prose-coach table { border-collapse:collapse; margin:.5rem 0; font-size:.88em; }
          .prose-coach th,.prose-coach td { border:1px solid #374151; padding:.25rem .5rem; }
          .prose-coach th { background:#1f2937; }
          .prose-coach blockquote { border-left:3px solid #4b5563; padding-left:.6rem; color:#cbd5e1; margin:.4rem 0; }
          .prose-coach a { color:#60a5fa; }
          .typing::after { content:'▋'; animation: blink 1s infinite; color:#9ca3af; }
          @keyframes blink { 50% { opacity:0; } }
          /* Report card sections (Row 3) — reuse prose-coach typography but
             wrap each section in a thin top border so they read as discrete
             units (not one wall of text). */
          .report-section + .report-section { border-top:1px solid #1f2937; padding-top:.6rem; margin-top:.6rem; }
          .report-section { margin-top:.2rem; }
          .report-sections { font-size:.92em; }
          .report-sections h1, .report-sections h2, .report-sections h3 { color:#f3f4f6; }
          .report-chips-text::before { content:'•'; color:#6b7280; margin-right:.4rem; animation: pulse 1.4s ease-in-out infinite; }
          @keyframes pulse { 50% { opacity:0.3; } }
          /* Hide the horizontal scrollbar on the carousel — scroll-snap dots
             are the visual indicator, scrollbar is just noise on mobile */
          .no-scrollbar { scrollbar-width: none; -ms-overflow-style: none; }
          .no-scrollbar::-webkit-scrollbar { display: none; }
          /* iOS Safari auto-zooms into any input/textarea with font-size < 16px
             on focus, AND doesn't zoom back out on blur — leaving the page
             stuck zoomed. Force ≥16px on mobile to suppress the zoom entirely.
             Desktop keeps the smaller Tailwind sizing (text-sm = 14px). */
          @media (max-width: 768px) {
            input, textarea, select { font-size: 16px !important; }
          }
          /* htmx applies .htmx-request to the element that triggered the
             pending request. Fade clicked sidebar items so user gets visual
             feedback during slow first-fetch (5-30s) of activity detail. */
          .htmx-request { opacity: 0.5; pointer-events: none; transition: opacity .15s; }
          .htmx-request::after {
            content: '· 拉取中…';
            font-size: 10px;
            color: #6b7280;
            margin-left: 6px;
            font-style: italic;
          }
          /* Fullscreen loading overlay — defined as raw CSS (not Tailwind
             utilities) because Tailwind's CDN JIT can race with elements
             we create dynamically in JS, leaving classes uncompiled. */
          #main-loading {
            position: fixed;
            inset: 0;
            z-index: 40;
            background: rgba(11, 15, 23, 0.88);
            backdrop-filter: blur(4px);
            -webkit-backdrop-filter: blur(4px);
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            touch-action: none;
          }
          @media (min-width: 768px) { #main-loading { left: 18rem; } }
          .spinner {
            width: 44px; height: 44px;
            border: 2px solid #374151;
            border-top-color: #3b82f6;
            border-radius: 50%;
            animation: spin 0.9s linear infinite;
            margin-bottom: 1rem;
          }
          @keyframes spin { to { transform: rotate(360deg); } }
          body.loading { overflow: hidden; }
          /* Native <dialog> styling — Tailwind CDN doesn't reliably hit
             dialog elements (especially ::backdrop), so spell it out raw. */
          dialog.app-dialog {
            background: #111827;
            color: #e5e7eb;
            border: 1px solid #374151;
            border-radius: 12px;
            max-width: 28rem;
            width: 92vw;
            padding: 0;
            box-shadow: 0 10px 40px rgba(0,0,0,0.6);
          }
          dialog.app-dialog::backdrop {
            background: rgba(0,0,0,0.65);
            backdrop-filter: blur(3px);
          }
          /* ── Onboarding tour ─────────────────────────────────────────
             Spotlight = an absolutely-positioned div with massive box-shadow
             spread → darkens everything OUTSIDE its rect. Tooltip = separate
             card positioned next to the spotlight.  */
          .tour-overlay {
            position: fixed; inset: 0; z-index: 60;
            pointer-events: auto;
          }
          /* `position: fixed` (not absolute) — getBoundingClientRect returns
             viewport coords, so we can use those directly without adding
             window.scrollY. Important: prevents tooltip from rendering at
             document-top when the user has scrolled chat-scroll down. */
          .tour-spotlight {
            position: fixed;
            border-radius: 10px;
            border: 2px solid #60a5fa;
            box-shadow: 0 0 0 9999px rgba(0,0,0,0.78), 0 0 30px rgba(96,165,250,0.6);
            pointer-events: none;
            transition: all 0.32s cubic-bezier(.4,0,.2,1);
          }
          .tour-tooltip {
            position: fixed;
            background: #1f2937;
            color: #f3f4f6;
            padding: 16px 18px;
            border-radius: 10px;
            border: 1px solid #374151;
            max-width: 320px;
            min-width: 260px;
            z-index: 61;
            box-shadow: 0 12px 30px rgba(0,0,0,0.6);
            font-size: 13px;
            line-height: 1.5;
          }
          .tour-tooltip h4 {
            font-size: 15px;
            font-weight: 600;
            margin-bottom: 6px;
            color: #f9fafb;
          }
          .tour-tooltip-actions {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-top: 14px;
            font-size: 12px;
          }
          .tour-tooltip-actions button {
            padding: 6px 14px;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            border: none;
          }
          .tour-skip { color: #9ca3af; background: transparent; }
          .tour-skip:hover { color: #e5e7eb; }
          .tour-next { color: white; background: #2563eb; font-weight: 500; }
          .tour-next:hover { background: #3b82f6; }
          .tour-step-counter { color: #6b7280; font-size: 11px; }
        """)),
        Script(NotStr(r"""
          // Extract <follow_ups>JSON</follow_ups> block from raw text.
          // Returns [cleanedText, chipsArray|null]. Used both during streaming
          // (to hide the JSON visually as it arrives) and on done (to extract
          // chips for the suggestion bar).
          const FOLLOWUPS_RE = /<follow_ups>\s*(\[[\s\S]*?\])\s*<\/follow_ups>/;
          function extractFollowUps(text) {
            const m = text.match(FOLLOWUPS_RE);
            if (!m) return [text, null];
            let chips = null;
            try { chips = JSON.parse(m[1]); } catch(e) {}
            const cleaned = text.replace(FOLLOWUPS_RE, '').trim();
            return [cleaned, Array.isArray(chips) ? chips : null];
          }
          // Replace #follow-up-chips contents. Newest assistant response wins
          // (called from each renderMD; document-order = chronological).
          function updateChips(chips) {
            const container = document.getElementById('follow-up-chips');
            if (!container) return;
            if (!chips || chips.length === 0) {
              container.innerHTML = '';
              return;
            }
            container.innerHTML = chips.map(c => {
              const safe = String(c).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                                     .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
              return '<button type="button" data-msg="' + safe +
                     '" onclick="useChip(this.dataset.msg)" ' +
                     'class="text-xs bg-gray-800 hover:bg-gray-700 text-gray-200 ' +
                     'px-3 py-1.5 rounded-full border border-gray-700 ' +
                     'whitespace-nowrap">' + safe + '</button>';
            }).join(' ');
          }
          // Click a suggested-prompt chip → fill textarea + auto-submit.
          function useChip(text) {
            const form = document.querySelector('form[data-clear-on-send]');
            if (!form) return;
            const ta = form.querySelector('textarea[name="msg"]');
            if (ta) ta.value = text;
            form.requestSubmit();
          }
          // Render markdown into a bubble (idempotent). Strips <follow_ups>
          // block and feeds chips to the suggestion bar.
          function renderMD(el) {
            if (!el || el.dataset.rendered === '1') return;
            const raw = el.dataset.raw || el.textContent || '';
            const [body, chips] = extractFollowUps(raw);
            try { el.innerHTML = marked.parse(body); }
            catch(e) { /* leave as text */ }
            el.dataset.rendered = '1';
            if (chips) updateChips(chips);
          }
          // Body is the sole scroller (avoids nested-scroll trap on mobile
          // where you have to touch exactly the inner scroller to pan
          // history). The fixed input bar sits above content via padding.
          function scrollChat() {
            // Skip when the thread has no real messages — the empty-state
            // hint sits at the top of the viewport, and scrolling to the
            // bottom of the document would push it (and the page header)
            // above the fold until the user scrolls back up.
            if (!document.querySelector('.msg-row')) return;
            window.scrollTo({top: document.documentElement.scrollHeight, behavior: 'auto'});
          }
          // Vanilla EventSource client — htmx-ext-sse's sse-swap overwrites
          // innerHTML on every chunk, fighting our markdown rendering. We own
          // the swapping logic, so subscribe directly.
          //
          // Server sends data as JSON-encoded string (ensure_ascii=False) so
          // unicode + newlines + special chars round-trip cleanly.
          function startStream(el) {
            const url = el.dataset.streamUrl;
            if (!url || el.dataset.streaming === '1') return;
            el.dataset.streaming = '1';
            const es = new EventSource(url);
            const finish = () => {
              stopThinkingCycle(el);
              el.classList.remove('typing', 'italic', 'text-gray-400');
              renderMD(el);
              try { es.close(); } catch(e) {}
              scrollChat();
            };
            // Server-side status events narrate backend phases (拉取 / 构建 /
            // 撰写中). Display in italic gray; ALSO start the cycling timer
            // so the user sees additional verbs rotate every 2.5s during
            // long LLM waits. New status events reset cycle to that phrase.
            // First real text chunk → clear all status state, switch modes.
            es.addEventListener('status', (ev) => {
              if (el.dataset.streamingStarted === '1') return;
              try {
                const text = JSON.parse(ev.data);
                el.textContent = text;
                el.classList.add('typing', 'italic', 'text-gray-400');
                // Restart cycle from this phrase (or phrase 0 if not in list)
                stopThinkingCycle(el);
                startThinkingCycle(el, text);
                scrollChat();
              } catch(e) {}
            });
            es.addEventListener('chunk', (ev) => {
              try {
                const text = JSON.parse(ev.data);
                // First real chunk arriving: drop status + cycling.
                if (el.dataset.streamingStarted !== '1') {
                  el.dataset.streamingStarted = '1';
                  stopThinkingCycle(el);
                  el.dataset.raw = '';
                  el.textContent = '';
                  el.classList.remove('italic', 'text-gray-400');
                }
                el.dataset.raw = (el.dataset.raw || '') + text;
                // Visually hide the <follow_ups> JSON block as soon as its
                // start tag arrives (LLM emits it at the very end). Only
                // textContent is masked — dataset.raw keeps the full text
                // so renderMD on `done` can still extract chips.
                const cutIdx = el.dataset.raw.indexOf('<follow_ups>');
                el.textContent = cutIdx >= 0
                  ? el.dataset.raw.substring(0, cutIdx)
                  : el.dataset.raw;
                el.classList.add('typing');
                scrollChat();
              } catch(e) {}
            });
            es.addEventListener('done', finish);
            // Tool call note — citation-style footer placed AFTER the
            // assistant bubble (not above it). Multiple tool calls in the
            // same response accumulate in one shared footer container.
            // Each entry: compact one-line summary + expandable raw args.
            //
            // Layout (all inside #chat-scroll):
            //   <div flex justify-start>  ← the assistant bubble row
            //     <div assistant-bubble>...streamed content...</div>
            //   </div>
            //   <div tool-footer>          ← new sibling, appears below
            //     <details>🔧 tool 1 · summary ▶</details>
            //     <details>🔧 tool 2 · summary ▶</details>
            //   </div>
            function getOrCreateToolFooter(el) {
              if (el._toolFooter && el._toolFooter.isConnected) return el._toolFooter;
              const footer = document.createElement('div');
              footer.className = 'flex flex-col gap-0.5 mt-1 mb-3 ml-3 max-w-[85%] '
                               + 'text-[11px] text-gray-500';
              const flexRow = el.parentElement;        // <div flex justify-start mb-3>
              const scroller = flexRow.parentElement;  // <div id=chat-scroll>
              scroller.insertBefore(footer, flexRow.nextSibling);
              el._toolFooter = footer;
              return footer;
            }
            es.addEventListener('tool', (ev) => {
              try {
                const data = JSON.parse(ev.data);
                const esc = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                                             .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
                let summaryHTML, argsHTML = '';
                if (typeof data === 'string') {
                  // Legacy format (in case any branch still sends a string)
                  summaryHTML = '🔧 ' + esc(data);
                } else {
                  summaryHTML = '🔧 <span class="text-gray-400">'
                              + esc(data.name || '') + '</span>'
                              + (data.summary ? ' · ' + esc(data.summary) : '');
                  if (data.args && Object.keys(data.args).length) {
                    argsHTML = '<pre class="text-[10px] mt-1 ml-4 px-2 py-1 '
                             + 'bg-gray-900/60 rounded text-gray-500 '
                             + 'whitespace-pre-wrap overflow-x-auto">'
                             + esc(JSON.stringify(data.args, null, 2))
                             + '</pre>';
                  }
                }
                const det = document.createElement('details');
                det.className = 'italic';
                det.innerHTML =
                  '<summary class="cursor-pointer hover:text-gray-300 truncate '
                  + 'list-none select-none">' + summaryHTML + '</summary>'
                  + argsHTML;
                getOrCreateToolFooter(el).appendChild(det);
                scrollChat();
              } catch(e) { console.error('tool event parse fail', e); }
            });
            es.onerror = finish;  // network drop / server close
          }
          // Force Plotly charts to re-measure their container. The inline
          // Plotly.newPlot(...) scripts that Plotly emits run during parse,
          // when the parent containers may not yet have laid out (esp.
          // inside a freshly htmx-swapped panel) → charts size to 0×0.
          // Calling Plotly.Plots.resize() after the DOM settles fixes them.
          // Safe to call on charts that already rendered correctly.
          function resizeAllCharts() {
            if (typeof Plotly === 'undefined') return;
            document.querySelectorAll('.plotly-graph-div').forEach(d => {
              try { Plotly.Plots.resize(d); } catch(e) {}
            });
          }

          // Fullscreen loading overlay for slow #main swaps (notably the
          // 5-30s first-click detail fetch). Built from a raw CSS class
          // (#main-loading + .spinner above) to dodge Tailwind CDN's JIT
          // race with dynamically-created elements.
          //
          // 150ms delay before showing → fast cached swaps don't flash.
          // body.loading class locks scroll → user can't pan history while waiting.
          let _loadingTimer = null;
          function showMainLoading(msg) {
            let el = document.getElementById('main-loading');
            if (!el) {
              el = document.createElement('div');
              el.id = 'main-loading';
              el.innerHTML =
                '<div class="spinner"></div>'
                + '<div id="main-loading-msg" style="font-size:14px;color:#d1d5db">'
                + (msg || I18N['js.loading']) + '</div>';
              document.body.appendChild(el);
            } else {
              const m = document.getElementById('main-loading-msg');
              if (m && msg) m.textContent = msg;
              el.style.display = 'flex';
            }
            document.body.classList.add('loading');
          }
          function hideMainLoading() {
            if (_loadingTimer) { clearTimeout(_loadingTimer); _loadingTimer = null; }
            const el = document.getElementById('main-loading');
            if (el) el.style.display = 'none';
            document.body.classList.remove('loading');
          }
          // Detect "is this swap going to replace the main pane?". Check the
          // resolved target AND, as fallback, the triggering element's
          // hx-target attribute (some htmx code paths set detail.target lazily).
          function _isMainSwap(ev) {
            const t = ev.detail && ev.detail.target;
            if (t && t.id === 'main') return true;
            const trigger = ev.detail && ev.detail.elt;
            if (trigger && trigger.getAttribute &&
                trigger.getAttribute('hx-target') === '#main') return true;
            // Also check the closest ancestor with hx-target (htmx supports inheritance)
            if (trigger && trigger.closest &&
                trigger.closest('[hx-target="#main"]')) return true;
            return false;
          }
          document.addEventListener('htmx:beforeRequest', (ev) => {
            if (!_isMainSwap(ev)) return;
            // Delay show — quick cached swaps (e.g. activity already fetched)
            // finish before 150ms, no flash. Slow ones (first fetch) show.
            _loadingTimer = setTimeout(() => showMainLoading(I18N['js.loading']), 150);
          });
          document.addEventListener('htmx:afterRequest', hideMainLoading);
          document.addEventListener('htmx:responseError', hideMainLoading);
          document.addEventListener('htmx:sendError', hideMainLoading);
          document.addEventListener('htmx:swapError', hideMainLoading);

          // ── Onboarding walkthrough ──────────────────────────────────
          // 4-step spotlight tour for first-time users. Triggered on chat
          // pages when localStorage has no `coach_onboarded_v1` flag.
          // Resettable from settings → "重新走一遍引导".
          const TOUR_KEY = 'coach_onboarded_v1';
          // Helper: open mobile drawer if needed so sidebar items are
          // measurable / visible. No-op on desktop.
          function _openDrawerIfMobile() {
            if (window.innerWidth >= 768) return;
            const sb = document.getElementById('sidebar');
            const ov = document.getElementById('drawer-overlay');
            if (sb) sb.classList.remove('-translate-x-full');
            if (ov) ov.classList.remove('hidden');
          }
          // Tour steps are built lazily so window.I18N (injected per-request
          // by Layout) is read at function-call time, not at script init.
          function _tourSteps() {
            return [
              {
                title: I18N['js.tour.step1.title'],
                text: I18N['js.tour.step1.text'],
                target: null,  // intro screen, no spotlight
              },
              {
                title: I18N['js.tour.step2.title'],
                text: I18N['js.tour.step2.text'],
                targetFn: () => {
                  _openDrawerIfMobile();
                  return document.getElementById('sidebar-sync-btn');
                },
              },
              {
                title: I18N['js.tour.step3.title'],
                text: I18N['js.tour.step3.text'],
                targetFn: () => {
                  _openDrawerIfMobile();
                  return document.querySelector('a[href="/chat/overall"]');
                },
              },
              {
                title: I18N['js.tour.step4.title'],
                text: I18N['js.tour.step4.text'],
                targetFn: () => {
                  _openDrawerIfMobile();
                  return document.querySelector('#sidebar a[href^="/chat/activity/"]');
                },
              },
              {
                title: I18N['js.tour.step5.title'],
                text: I18N['js.tour.step5.text'],
                targetFn: () => {
                  _openDrawerIfMobile();
                  return document.getElementById('sidebar-settings-btn');
                },
              },
            ];
          }
          let TOUR_STEPS = null;
          let _tourIdx = 0;
          function _renderTourStep() {
            if (TOUR_STEPS === null) TOUR_STEPS = _tourSteps();
            _removeTourElements();
            const step = TOUR_STEPS[_tourIdx];
            const target = step.target || (step.targetFn && step.targetFn());

            // Always-present overlay
            const overlay = document.createElement('div');
            overlay.className = 'tour-overlay';
            overlay.id = 'tour-overlay';
            document.body.appendChild(overlay);

            // Tooltip — created upfront so we can measure its rect for layout
            const tip = document.createElement('div');
            tip.className = 'tour-tooltip';
            tip.id = 'tour-tooltip';
            tip.innerHTML =
              '<h4>' + step.title + '</h4>'
              + '<div>' + step.text + '</div>'
              + '<div class="tour-tooltip-actions">'
              + '<span class="tour-step-counter">' + (_tourIdx+1) + ' / ' + TOUR_STEPS.length + '</span>'
              + '<div>'
              + '<button class="tour-skip" onclick="_skipTour()">' + I18N['js.tour.skip'] + '</button>'
              + '<button class="tour-next" onclick="_nextTourStep()">'
              +   (_tourIdx === TOUR_STEPS.length - 1 ? I18N['js.tour.done'] : I18N['js.tour.next'])
              + '</button>'
              + '</div></div>';
            document.body.appendChild(tip);

            if (target) {
              // Scroll target into viewport before measuring. Instant (not
              // smooth) so getBoundingClientRect immediately returns the
              // post-scroll rect — no setTimeout dance required.
              try { target.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'auto' }); }
              catch (e) { /* older browsers */ }
              _placeSpotlightAndTip(target, tip);
            } else {
              // No target = intro screen. Fixed-positioned tooltip centered
              // in viewport (translate trick).
              tip.style.left = '50%';
              tip.style.top = '50%';
              tip.style.transform = 'translate(-50%, -50%)';
            }
          }
          function _placeSpotlightAndTip(target, tip) {
            const r = target.getBoundingClientRect();  // viewport-relative
            const sp = document.createElement('div');
            sp.className = 'tour-spotlight';
            sp.id = 'tour-spotlight';
            const pad = 6;
            // position: fixed in CSS → use viewport coords directly.
            sp.style.left = (r.left - pad) + 'px';
            sp.style.top = (r.top - pad) + 'px';
            sp.style.width = (r.width + pad*2) + 'px';
            sp.style.height = (r.height + pad*2) + 'px';
            document.body.appendChild(sp);

            // Position tooltip — prefer right of spotlight; fall back to
            // below; clamp inside viewport so it never overflows.
            const tipRect = tip.getBoundingClientRect();
            let tipL = r.right + 16;
            let tipT = r.top;
            if (tipL + tipRect.width > window.innerWidth - 16) {
              tipL = Math.max(8, Math.min(r.left, window.innerWidth - tipRect.width - 16));
              tipT = r.bottom + 16;
            }
            if (tipT + tipRect.height > window.innerHeight - 16) {
              tipT = Math.max(8, window.innerHeight - tipRect.height - 16);
            }
            tipL = Math.max(8, tipL);
            tipT = Math.max(8, tipT);
            tip.style.left = tipL + 'px';
            tip.style.top = tipT + 'px';
          }
          function _removeTourElements() {
            ['tour-overlay', 'tour-spotlight', 'tour-tooltip'].forEach(id => {
              const el = document.getElementById(id);
              if (el) el.remove();
            });
          }
          function _nextTourStep() {
            if (_tourIdx >= TOUR_STEPS.length - 1) {
              _finishTour();
            } else {
              _tourIdx++;
              _renderTourStep();
            }
          }
          function _skipTour() { _finishTour(); }
          function _finishTour() {
            localStorage.setItem(TOUR_KEY, '1');
            _removeTourElements();
            // Close any drawer the tour opened
            if (typeof closeDrawer === 'function') closeDrawer();
          }
          function startTour() {
            _tourIdx = 0;
            _renderTourStep();
          }
          // ESC to skip
          document.addEventListener('keydown', (ev) => {
            if (ev.key === 'Escape' && document.getElementById('tour-overlay')) {
              _skipTour();
            }
          });
          // Auto-start on chat pages when not yet onboarded.
          window.addEventListener('load', () => {
            if (localStorage.getItem(TOUR_KEY) === '1') return;
            const path = window.location.pathname;
            if (path.startsWith('/chat')) {
              setTimeout(startTour, 800);
            }
          });

          // ── Pin / 长期记忆 ──────────────────────────────────────────
          // Open the pin dialog with the assistant bubble's full text
          // pre-filled. The btn lives next to the bubble inside .msg-row,
          // so closest('.msg-row') → find .assistant-bubble inside.
          let _pinSourceURL = '';
          function openPinDialog(btn) {
            const row = btn.closest('.msg-row');
            const bubble = row && row.querySelector('.assistant-bubble');
            if (!bubble) return;
            // dataset.raw still has the <follow_ups> JSON tail — strip it
            const raw = bubble.dataset.raw || bubble.textContent || '';
            const cleaned = raw.replace(FOLLOWUPS_RE, '').trim();
            const ta = document.getElementById('pin-text');
            ta.value = cleaned;
            ta.disabled = false;
            document.getElementById('pin-status').textContent = '';
            document.getElementById('pin-distill-btn').disabled = false;
            document.getElementById('pin-save-btn').disabled = false;
            document.getElementById('pin-save-btn').textContent = I18N['js.pin.save_btn'];
            _pinSourceURL = window.location.pathname;
            document.getElementById('pin-dlg').showModal();
          }
          async function distillPin() {
            const ta = document.getElementById('pin-text');
            const orig = ta.value.trim();
            if (!orig) return;
            const btn = document.getElementById('pin-distill-btn');
            const status = document.getElementById('pin-status');
            ta.disabled = true; btn.disabled = true;
            status.textContent = I18N['js.pin.distilling'];
            try {
              const resp = await fetch('/pin/distill', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'text=' + encodeURIComponent(orig),
              });
              if (!resp.ok) throw new Error('HTTP ' + resp.status);
              const distilled = (await resp.text()).trim();
              if (distilled) {
                ta.value = distilled;
                status.textContent = I18N['js.pin.distilled'];
              } else {
                status.textContent = I18N['js.pin.distill_empty'];
              }
            } catch(e) {
              status.textContent = I18N['js.pin.distill_fail'].replace('{e}', e.message);
            } finally {
              ta.disabled = false; btn.disabled = false;
            }
          }
          // ── 关于我 ✨ 整理 ──────────────────────────────────────────
          // Read the personal_note textarea, send to /settings/note/distill,
          // replace the textarea content with the LLM-organized version. User
          // still has to click 保存 to persist (so they can edit / undo).
          async function distillNote(btn) {
            const ta = document.querySelector('textarea[name="personal_note"]');
            if (!ta) return;
            const orig = ta.value.trim();
            const status = document.getElementById('note-status');
            if (!orig) {
              status.textContent = I18N['js.note.empty'];
              status.className = 'text-xs text-amber-400 mt-1';
              return;
            }
            ta.disabled = true; btn.disabled = true;
            status.textContent = I18N['js.note.organizing'];
            status.className = 'text-xs text-blue-400 mt-1';
            try {
              const resp = await fetch('/settings/note/distill', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'personal_note=' + encodeURIComponent(orig),
              });
              if (!resp.ok) throw new Error('HTTP ' + resp.status);
              const refined = (await resp.text()).trim();
              if (refined) {
                ta.value = refined;
                status.textContent = I18N['js.note.organized'];
                status.className = 'text-xs text-emerald-400 mt-1';
              } else {
                status.textContent = I18N['js.note.organize_empty'];
                status.className = 'text-xs text-amber-400 mt-1';
              }
            } catch(e) {
              status.textContent = I18N['js.note.organize_fail'].replace('{e}', e.message);
              status.className = 'text-xs text-red-400 mt-1';
            } finally {
              ta.disabled = false; btn.disabled = false;
            }
          }

          // ── Row 3 复盘报告 card ──────────────────────────────────────
          // Manages the dedicated report card on activity chat pages:
          //   • collapse/expand toggle (state persisted to localStorage per aid)
          //   • SSE subscription when status="running" (replay + tail)
          //   • renders each completed section as a finished markdown block
          //   • shows cycling chips placeholder for the in-progress section
          //   • header pill transitions blue → green on done

          // Built lazily so I18N (per-request payload) is read at call time,
          // not at script init.
          function _reportChips() {
            return [
              I18N['js.report.chip0'],
              I18N['js.report.chip1'],
              I18N['js.report.chip2'],
              I18N['js.report.chip3'],
              I18N['js.report.chip4'],
            ];
          }
          let REPORT_CHIPS = null;
          function startReportChipsCycle(el) {
            if (!el || el._chipsTimer) return;
            if (REPORT_CHIPS === null) REPORT_CHIPS = _reportChips();
            let idx = 0;
            const tick = () => {
              idx = (idx + 1) % REPORT_CHIPS.length;
              const t = el.querySelector('.report-chips-text');
              if (t) t.textContent = REPORT_CHIPS[idx];
            };
            el._chipsTimer = setInterval(tick, 2200);
          }
          function stopReportChipsCycle(el) {
            if (el && el._chipsTimer) {
              clearInterval(el._chipsTimer);
              el._chipsTimer = null;
            }
          }

          function _renderReportSection(card, mdText) {
            const sectionsEl = card.querySelector('.report-sections');
            if (!sectionsEl) return;
            // Strip <follow_ups>JSON</follow_ups> tail (typically arrives in
            // the last section) and feed it to the chips bar — same as the
            // chat-bubble renderMD flow does. Without this the JSON renders
            // as raw text inside the report.
            const [body, chips] = extractFollowUps(mdText);
            if (chips) updateChips(chips);
            if (!body.trim()) return;
            const div = document.createElement('div');
            div.className = 'report-section';
            try {
              div.innerHTML = window.marked
                ? marked.parse(body)
                : body.replace(/</g, '&lt;');
            } catch (e) {
              div.textContent = body;
            }
            sectionsEl.appendChild(div);
          }

          function toggleReportCard(headerEl) {
            const card = headerEl.closest('.report-card');
            if (!card) return;
            const body  = card.querySelector('.report-body');
            const arrow = card.querySelector('.collapse-arrow');
            const aid   = card.dataset.aid;
            const collapsed = body.classList.toggle('hidden');
            if (arrow) {
              arrow.textContent = collapsed ? '▶' : '▼';
            }
            try {
              localStorage.setItem('report-collapse-' + aid, collapsed ? '1' : '0');
            } catch (e) {}
          }

          function regenerateReport(aid) {
            htmx.ajax('POST', '/chat/activity/' + aid + '/regenerate', {
              target: '#main', swap: 'innerHTML',
            });
          }

          function subscribeToReport(aid) {
            const card = document.getElementById('report-card');
            if (!card) return;
            const es = new EventSource('/chat/activity/' + aid + '/report/stream');
            card._eventSource = es;
            es.addEventListener('section', (ev) => {
              try {
                const md = JSON.parse(ev.data);
                _renderReportSection(card, md);
              } catch (e) { console.warn('section parse failed', e); }
            });
            es.addEventListener('current_started', () => {});
            es.addEventListener('done', (ev) => {
              card.dataset.status = 'done';
              const pill = card.querySelector('.status-pill');
              if (pill) {
                pill.textContent = I18N['js.report.pill.done'];
                pill.className = 'status-pill text-xs text-emerald-300 '
                               + 'bg-emerald-900/40 px-2 py-0.5 rounded-full';
              }
              const chips = card.querySelector('.report-chips');
              if (chips) {
                stopReportChipsCycle(chips);
                chips.remove();
              }
              es.close();
              card._eventSource = null;
            });
            es.addEventListener('error', (ev) => {
              let msg = I18N['js.report.fail_msg'];
              try { msg = JSON.parse(ev.data) || msg; } catch (e) {}
              card.dataset.status = 'error';
              const pill = card.querySelector('.status-pill');
              if (pill) {
                pill.textContent = I18N['js.report.pill.fail'];
                pill.className = 'status-pill text-xs text-red-300 '
                               + 'bg-red-900/40 px-2 py-0.5 rounded-full';
              }
              const sectionsEl = card.querySelector('.report-sections');
              if (sectionsEl) {
                const div = document.createElement('div');
                div.className = 'text-red-400 text-sm py-2';
                div.textContent = '❌ ' + msg;
                sectionsEl.appendChild(div);
              }
              const chips = card.querySelector('.report-chips');
              if (chips) {
                stopReportChipsCycle(chips);
                chips.remove();
              }
              es.close();
              card._eventSource = null;
            });
          }

          function initReportCard() {
            const card = document.getElementById('report-card');
            if (!card || card.dataset.init === '1') return;
            card.dataset.init = '1';

            const aid     = card.dataset.aid;
            const status  = card.dataset.status;
            const body    = card.querySelector('.report-body');
            const arrow   = card.querySelector('.collapse-arrow');

            let collapsed = true;
            try {
              const stored = localStorage.getItem('report-collapse-' + aid);
              if (stored === '0') collapsed = false;
            } catch (e) {}
            if (!collapsed) {
              body.classList.remove('hidden');
              if (arrow) arrow.textContent = '▼';
            }

            const saved = card.dataset.reportText || '';
            if (saved) {
              const parts = saved.split(/\n(?=## )/);
              for (const p of parts) {
                if (p.trim()) _renderReportSection(card, p);
              }
            }

            if (status === 'running') {
              const chips = card.querySelector('.report-chips');
              if (chips) startReportChipsCycle(chips);
              subscribeToReport(aid);
            }
          }

          async function savePin() {
            const ta = document.getElementById('pin-text');
            const txt = ta.value.trim();
            if (!txt) return;
            const btn = document.getElementById('pin-save-btn');
            const status = document.getElementById('pin-status');
            btn.disabled = true; btn.textContent = I18N['js.pin.saving'];
            try {
              const resp = await fetch('/pin/save', {
                method: 'POST',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: 'text=' + encodeURIComponent(txt) +
                      '&source=' + encodeURIComponent(_pinSourceURL),
              });
              if (!resp.ok) throw new Error('HTTP ' + resp.status);
              status.textContent = I18N['js.pin.saved'];
              setTimeout(() => document.getElementById('pin-dlg').close(), 800);
            } catch(e) {
              status.textContent = I18N['js.pin.save_fail'].replace('{e}', e.message);
              btn.disabled = false; btn.textContent = I18N['js.pin.save_btn'];
            }
          }

          // Cycling status phrases — server emits a `status` event marking
          // each phase (fetch / build / write); client cycles through these
          // sub-phrases every 2.5s while in the "writing" phase so the user
          // sees motion instead of a static placeholder during 5-30s LLM waits.
          // Server-emitted status events take precedence (reset cycle to first phrase).
          // Built lazily so I18N (per-request payload) is read at first use.
          function _thinkingPhrases() {
            return [
              I18N['js.stream.chip0'],
              I18N['js.stream.chip1'],
              I18N['js.stream.chip2'],
              I18N['js.stream.chip3'],
              I18N['js.stream.chip4'],
            ];
          }
          let THINKING_PHRASES = null;
          function startThinkingCycle(el, startPhrase) {
            if (el._thinkingTimer) return;  // already cycling
            if (THINKING_PHRASES === null) THINKING_PHRASES = _thinkingPhrases();
            // Find a sensible starting index based on the current phrase
            let idx = 0;
            if (startPhrase) {
              const found = THINKING_PHRASES.indexOf(startPhrase);
              if (found >= 0) idx = found;
            }
            el._thinkingTimer = setInterval(() => {
              if (el.dataset.streamingStarted === '1') {
                stopThinkingCycle(el);
                return;
              }
              idx = (idx + 1) % THINKING_PHRASES.length;
              el.textContent = THINKING_PHRASES[idx];
            }, 2500);
          }
          function stopThinkingCycle(el) {
            if (el._thinkingTimer) {
              clearInterval(el._thinkingTimer);
              el._thinkingTimer = null;
            }
          }

          // Activity stats carousel — update pagination dots as user scrolls
          // through the horizontal scroll-snap cards. Idempotent: dataset.init
          // flag prevents double-attaching listeners after htmx swaps.
          function initCarousels() {
            document.querySelectorAll('[id^="carousel-"]').forEach(c => {
              if (c.dataset.init === '1') return;
              c.dataset.init = '1';
              const aid = c.id.replace('carousel-', '');
              const update = () => {
                const w = c.clientWidth;
                if (!w) return;
                const idx = Math.round(c.scrollLeft / w);
                const dots = document.querySelectorAll(`[id^="dot-${aid}-"]`);
                dots.forEach((d, i) => {
                  d.classList.remove(i === idx ? 'bg-gray-700' : 'bg-gray-200');
                  d.classList.add(i === idx ? 'bg-gray-200' : 'bg-gray-700');
                });
              };
              c.addEventListener('scroll', update, { passive: true });
              update();  // initial sync (e.g. after htmx swap with non-zero scrollLeft)
            });
          }
          // Wire up on initial load and after every htmx swap.
          function scanBubbles(ev) {
            // Close the mobile drawer ONLY on navigation swaps (target = #main).
            // Without the gate, in-sidebar polls (e.g. /sync/status every 1.5s)
            // fire htmx:afterSwap → close the drawer mid-sync, so the user has
            // to keep re-opening it just to see the percent tick up.
            const t = ev && ev.detail && ev.detail.target;
            if (t && t.id === 'main') closeDrawer();
            document.querySelectorAll('.assistant-bubble[data-stream-url]').forEach(startStream);
            document.querySelectorAll('.assistant-bubble[data-rendered="0"]:not([data-stream-url])').forEach(renderMD);
            initCarousels();
            initReportCard();
            // Resize Plotly charts — they were initialized during HTML parse
            // when their containers were 0×0; force a re-measure now that
            // layout has settled. Two passes: immediate (catches most) +
            // 100ms delayed (catches stragglers where layout took longer).
            resizeAllCharts();
            setTimeout(resizeAllCharts, 100);
            // Also resize whenever the user manually opens a <details> —
            // catches any chart that was inside a closed details on parse.
            document.querySelectorAll('details').forEach(d => {
              if (d.dataset.resizeWired === '1') return;
              d.dataset.resizeWired = '1';
              d.addEventListener('toggle', () => {
                if (d.open) setTimeout(resizeAllCharts, 50);
              });
            });
            scrollChat();
          }
          // Use document (not document.body — script runs in <head> before
          // body exists). htmx events bubble all the way up.
          document.addEventListener('htmx:afterSwap', scanBubbles);
          window.addEventListener('load', scanBubbles);
          window.addEventListener('DOMContentLoaded', scanBubbles);

          // Mobile drawer toggle (open/close in tandem)
          function toggleDrawer() {
            const d = document.getElementById('sidebar');
            d.classList.toggle('-translate-x-full');
            document.getElementById('drawer-overlay').classList.toggle('hidden');
          }
          // Force drawer closed (used after htmx navigation — sidebar gets
          // OOB-swapped back to default closed state, but the overlay would
          // otherwise linger and trap clicks, dimming the new chat panel).
          function closeDrawer() {
            const d = document.getElementById('sidebar');
            const ov = document.getElementById('drawer-overlay');
            if (d) d.classList.add('-translate-x-full');
            if (ov) ov.classList.add('hidden');
          }

          // ── iOS "Add to Home Screen" hint ───────────────────────────
          // Shows a small dismissible tag in the mobile header when:
          //   • on iOS Safari (the only browser that supports the iOS PWA flow)
          //   • not already running standalone (window.navigator.standalone)
          //   • user hasn't dismissed in the last 30 days
          // Tap the tag → instructions overlay; tap ✕ → dismiss for 30d.
          const PWA_HINT_KEY = 'pwa-hint-dismissed-at';
          const PWA_HINT_REPROMPT_DAYS = 30;
          function _isIOSSafari() {
            const ua = navigator.userAgent || '';
            const isIOS = /iPhone|iPad|iPod/.test(ua);
            // Must be Safari proper — Chrome (CriOS), Firefox (FxiOS) and
            // Edge (EdgiOS) on iOS each have a different UA but none can
            // trigger the iOS Add-to-Home flow from a webpage.
            const isSafari = /Safari/.test(ua) && !/CriOS|FxiOS|EdgiOS/.test(ua);
            return isIOS && isSafari;
          }
          function _isStandalone() {
            return window.navigator.standalone === true ||
                   (window.matchMedia &&
                    window.matchMedia('(display-mode: standalone)').matches);
          }
          function _shouldShowInstallHint() {
            if (!_isIOSSafari()) return false;
            if (_isStandalone()) return false;
            try {
              const v = localStorage.getItem(PWA_HINT_KEY);
              if (v) {
                const days = (Date.now() - parseInt(v, 10)) / 86400000;
                if (days < PWA_HINT_REPROMPT_DAYS) return false;
              }
            } catch (e) {}
            return true;
          }
          function maybeShowInstallButton() {
            const tag = document.getElementById('install-hint-tag');
            if (!tag) return;
            if (_shouldShowInstallHint()) tag.classList.remove('hidden');
          }
          function showInstallHint() {
            const ov = document.getElementById('install-hint-overlay');
            if (!ov) return;
            ov.classList.remove('hidden');
            ov.classList.add('flex');
          }
          function closeInstallHint() {
            const ov = document.getElementById('install-hint-overlay');
            if (!ov) return;
            ov.classList.add('hidden');
            ov.classList.remove('flex');
          }
          function dismissInstallHint(ev) {
            if (ev) { ev.stopPropagation(); ev.preventDefault(); }
            try { localStorage.setItem(PWA_HINT_KEY, Date.now().toString()); } catch (e) {}
            closeInstallHint();
            const tag = document.getElementById('install-hint-tag');
            if (tag) tag.classList.add('hidden');
          }
          window.addEventListener('load', maybeShowInstallButton);
          // Send on Enter (Shift+Enter for newline)
          function sendOnEnter(ev) {
            if (ev.key === 'Enter' && !ev.shiftKey) {
              ev.preventDefault();
              ev.target.closest('form').requestSubmit();
            }
          }
          // Clear textarea after submit
          document.addEventListener('htmx:afterRequest', (ev) => {
            if (ev.detail.elt.tagName === 'FORM' && ev.detail.elt.dataset.clearOnSend === '1') {
              const ta = ev.detail.elt.querySelector('textarea');
              if (ta) ta.value = '';
            }
          });
        """)),
    ),
)


# ── Helpers ────────────────────────────────────────────────────────────────────
# ── Password gate (APP_PASSWORD env) ───────────────────────────────────────
# Optional — empty env means no gate (local dev). Container deploys set
# APP_PASSWORD via docker-compose mapping from .env's LOGIN_PASSWORD.
_APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()


def _auth_cookie_value() -> str:
    """Stable cookie value derived from the password — survives server
    restarts (no in-memory session table) but rotates if password changes.
    Truncated to 40 chars (collision-resistant for our scale, smaller cookie)."""
    import hashlib
    return hashlib.sha256(("coach-auth:" + _APP_PASSWORD).encode()).hexdigest()[:40]


# Paths the middleware always lets through (no auth check). Anything else
# triggers a redirect to /lock when APP_PASSWORD is set.
_AUTH_OPEN_PATHS = {"/lock", "/favicon.ico"}


class PasswordMiddleware(BaseHTTPMiddleware):
    """Gate all requests behind APP_PASSWORD if it's set. Cookie-based —
    user enters password once at /lock, gets a 30-day cookie, then has
    free access until cookie expires or password changes."""
    async def dispatch(self, request, call_next):
        if not _APP_PASSWORD:
            return await call_next(request)
        path = request.url.path
        if path in _AUTH_OPEN_PATHS:
            return await call_next(request)
        if request.cookies.get("coach_authed") == _auth_cookie_value():
            return await call_next(request)
        # Unauthenticated — redirect to /lock for browser nav, 401 for fetch
        # (so JS-driven calls fail loud rather than getting redirected HTML).
        if request.headers.get("hx-request") or "json" in (request.headers.get("accept") or ""):
            return Response("", status_code=401, headers={"HX-Redirect": "/lock"})
        return RedirectResponse("/lock", status_code=303)


# Wire middleware. add_middleware appends to the chain — earlier-added
# middlewares wrap LATER ones (closer to the route). PasswordMiddleware
# should be outermost (first to inspect every request).
app.add_middleware(PasswordMiddleware)


# ── Locale resolution per request ─────────────────────────────────────────────
# Resolution order: db.locale → cookie 'locale' (transient hint while
# /locale POST is in flight) → Accept-Language → DEFAULT_LOCALE env →
# db.LOCALE_DEFAULT. Sets a contextvar that i18n.t() reads.

class LocaleMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        try:
            with db.connect() as c:
                stored = db.config_get(c, "locale", None)
        except Exception:
            stored = None
        if stored:
            chosen = stored
        elif (cookie := request.cookies.get("locale")):
            chosen = cookie
        else:
            chosen = i18n.pick_lang_from_accept(request.headers.get("accept-language"))
        i18n.set_request_locale(chosen)
        response = await call_next(request)
        # HTML content is locale-dependent — without no-store, browsers
        # cache `/` and show stale-language pages after a /locale switch.
        ctype = response.headers.get("content-type", "")
        if "text/html" in ctype:
            response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response


app.add_middleware(LocaleMiddleware)


@rt_route("/lock", methods=["GET"])
def get_lock():
    """Password entry page. Shown when middleware redirects unauthenticated
    requests here. Bare layout (no sidebar) — user can't see anything until
    they enter the right password."""
    return (
        Title(i18n.t("lock.title")),
        Body(
            Div(
                Div(
                    Div("🔐", cls="text-5xl text-center mb-3"),
                    H2(i18n.t("lock.brand"), cls="text-xl font-semibold text-gray-100 text-center mb-1"),
                    P(i18n.t("lock.prompt"), cls="text-sm text-gray-500 text-center mb-6"),
                    Form(
                        Input(type="password", name="password",
                              placeholder=i18n.t("lock.password_ph"),
                              required=True,
                              autofocus=True,
                              cls="w-full bg-gray-800 text-gray-100 rounded-lg "
                                  "px-4 py-2.5 border border-gray-700 "
                                  "focus:border-blue-500 focus:outline-none mb-3"),
                        Button(i18n.t("lock.submit"), type="submit",
                               cls="w-full bg-blue-600 hover:bg-blue-500 text-white "
                                   "py-2.5 rounded-lg font-medium"),
                        method="post",
                        action="/lock",
                    ),
                    cls="max-w-xs mx-auto pt-24 px-6",
                ),
                cls="min-h-screen bg-gray-950",
            ),
        ),
    )


@rt_route("/lock", methods=["POST"])
def post_lock(password: str = ""):
    """Verify password → set cookie + redirect home. Wrong password →
    re-render the form with an inline error (no DB hit, no rate limit —
    single-user scale)."""
    if password == _APP_PASSWORD:
        # secure=False so it works on http://LAN-IP. If you front this with
        # HTTPS later, flip to secure=True (or set via env var).
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            "coach_authed",
            _auth_cookie_value(),
            max_age=86400 * 30,        # 30 days
            httponly=True,             # no JS access
            samesite="lax",            # CSRF defence
        )
        return resp
    # Wrong — re-render form with red error above input
    return (
        Title(i18n.t("lock.title")),
        Body(
            Div(
                Div(
                    Div("🔐", cls="text-5xl text-center mb-3"),
                    H2(i18n.t("lock.brand"), cls="text-xl font-semibold text-gray-100 text-center mb-1"),
                    P(i18n.t("lock.wrong"), cls="text-sm text-red-400 text-center mb-6"),
                    Form(
                        Input(type="password", name="password",
                              placeholder=i18n.t("lock.password_ph"),
                              required=True, autofocus=True,
                              cls="w-full bg-gray-800 text-gray-100 rounded-lg "
                                  "px-4 py-2.5 border border-red-500 "
                                  "focus:border-blue-500 focus:outline-none mb-3"),
                        Button(i18n.t("lock.submit"), type="submit",
                               cls="w-full bg-blue-600 hover:bg-blue-500 text-white "
                                   "py-2.5 rounded-lg font-medium"),
                        method="post",
                        action="/lock",
                    ),
                    cls="max-w-xs mx-auto pt-24 px-6",
                ),
                cls="min-h-screen bg-gray-950",
            ),
        )
    )


def _have_session() -> bool:
    return os.path.exists(os.path.join(_DATA_DIR, ".garth_session", "oauth2_token.json"))


def _have_data() -> bool:
    if not os.path.exists(_DB_PATH):
        return False
    try:
        with db.connect() as conn:
            return conn.execute("SELECT count(*) FROM activities").fetchone()[0] > 0
    except Exception:
        return False


def _fmt_relative(ts_str: str) -> str:
    if not ts_str:
        return i18n.t("rel.dash")
    try:
        dt = datetime.fromisoformat(ts_str)
    except Exception:
        return ts_str[:16]
    now = datetime.now()
    diff = now - dt
    if diff.total_seconds() < 60:
        return i18n.t("rel.just_now")
    if diff.total_seconds() < 3600:
        return i18n.t("rel.minutes_ago", n=int(diff.total_seconds() / 60))
    if diff.days < 1:
        return i18n.t("rel.hours_ago", n=int(diff.total_seconds() / 3600))
    if diff.days < 7:
        return i18n.t("rel.days_ago", n=diff.days)
    return dt.strftime(i18n.t("rel.older_fmt"))


def _is_runlike_activity(tk: str) -> bool:
    # Sidebar only surfaces activity types this app actually analyses
    # (running + cycling variants). Strength / swim / walk / hike / yoga
    # land in SQLite so the cross-activity chat sees full training context,
    # but they'd be noise in the sidebar's per-activity drill-down list.
    return any(k in (tk or "") for k in ("run", "cycl", "bik", "ride"))


def _activity_label(a: dict) -> tuple[str, str]:
    """Return (primary_line, secondary_line) for an activity sidebar row."""
    tk = a.get("activityTypeKey") or ""
    type_label = gd.display_type(tk)
    dist_km = (a.get("distance") or 0) / 1000
    dur = gd.format_duration(a.get("duration"))
    primary = f"{type_label} {dist_km:.1f}km"
    dt_iso = (a.get("startTimeLocal") or "")[:16]
    try:
        dt = datetime.fromisoformat(dt_iso)
        date_part = dt.strftime(i18n.t("act.row_date_fmt"))
    except Exception:
        date_part = dt_iso[:16]
    secondary = f"{date_part} · {dur}"
    return primary, secondary


# ── Layout components ──────────────────────────────────────────────────────────
def Sidebar(active_route: str = "/chat/overall"):
    # Sidebar is `fixed inset-y-0` so on iOS standalone PWA it overlaps the
    # status bar at top — same pattern as the mobile header. `pt-[env(safe-
    # area-inset-top)]` is 0 in Safari and the status-bar height in PWA mode.
    _SIDEBAR_CLS = (
        "fixed inset-y-0 left-0 w-72 bg-gray-900 border-r border-gray-800 z-30 "
        "transform md:translate-x-0 -translate-x-full transition-transform "
        "overflow-y-auto pt-[env(safe-area-inset-top)]"
    )
    if not _have_data():
        return Aside(
            Div(i18n.t("sidebar.no_data"), cls="text-gray-500 text-sm p-4"),
            id="sidebar",
            cls=_SIDEBAR_CLS,
        )

    with db.connect() as conn:
        meta = db.get_app_metadata(conn)
        acts = db.get_recent_activities(conn, days=90)
        acts = [a for a in acts if _is_runlike_activity(a.get("activityTypeKey"))]
        tags_map = db.tags_all(conn)

    sync_text = i18n.t("sidebar.last_sync", when=_fmt_relative(meta.get('fetched_at', '')))
    name = meta.get("display_name", "")[:8]

    # Greeting + race countdown — visual delight on first eye contact.
    # Time-of-day buckets: early / midday / eve / night.
    cfg_for_sidebar = uc.load()
    user_name = (os.environ.get("DISPLAY_NAME") or "").strip()
    if not user_name:
        # Fall back: Garmin display_name is often a UUID, just use empty
        prof_name = meta.get("display_name", "")
        if prof_name and not ("-" in prof_name and len(prof_name) > 20):
            user_name = prof_name.split()[0]
    _hour = datetime.now().hour
    _period = i18n.t("greeting.morning"   if 5  <= _hour < 12 else
                     "greeting.afternoon" if 12 <= _hour < 18 else
                     "greeting.evening"   if 18 <= _hour < 22 else
                     "greeting.night")
    _emoji = ("🌅" if 5 <= _hour < 12 else
              "☀️" if 12 <= _hour < 18 else
              "🌆" if 18 <= _hour < 22 else "🌙")
    _sep = i18n.t("greeting.sep") if user_name else ""
    greeting_line = f"{_period}{_sep}{user_name} {_emoji}".rstrip(" ") if user_name else f"{_period} {_emoji}"

    # Race countdown — only when phase=race_prep AND there's an upcoming race.
    # Tuple shape: (icon, name, sub) — sub is the small gray second line
    # (e.g. "21.1km · 还有 36 天"). Two-line layout keeps the countdown visible
    # on narrow mobile drawers (single-line truncate would clip 还有N天).
    race_line = None
    phase = cfg_for_sidebar.get("phase", "maintenance")
    nr = uc.next_race(cfg_for_sidebar)
    if phase == "race_prep" and nr and nr.get("date"):
        try:
            days = (date.fromisoformat(nr["date"]) - date.today()).days
            dist_part = f"{nr['distance_km']}km · " if nr.get("distance_km") else ""
            race_line = ("🏁", nr["name"], dist_part + i18n.t("race.days_left", days=days))
        except Exception:
            pass
    elif phase == "race_prep":
        race_line = ("🏁", i18n.t("phase.race_prep_short"), i18n.t("race.no_target"))
    elif phase == "recovery":
        race_line = ("😌", i18n.t("phase.recovery_short"), i18n.t("phase.recovery_hint"))

    def _act_row(a):
        aid = a.get("activityId")
        is_active = active_route == f"/chat/activity/{aid}"
        primary, secondary = _activity_label(a)
        tag = tags_map.get(aid, "") if tags_map else ""
        bg = "bg-gray-800" if is_active else "hover:bg-gray-800/60"
        tag_color = _TAG_COLOR.get(tag, _TAG_TIER_GRAY)
        tag_chip = (Span(_tag_label(tag), cls=f"text-[10px] {tag_color} px-1.5 py-0.5 rounded ml-1")
                    if tag else None)
        return A(
            Div(
                Div(Span(primary, cls="text-sm text-gray-100 truncate"), tag_chip,
                    cls="flex items-center"),
                Div(secondary, cls="text-xs text-gray-500 mt-0.5"),
                cls="flex flex-col px-3 py-2",
            ),
            href=f"/chat/activity/{aid}",
            hx_get=f"/chat/activity/{aid}",
            hx_target="#main",
            hx_push_url="true",
            hx_swap="innerHTML",
            cls=f"block rounded-md mx-2 my-0.5 {bg} text-gray-200 cursor-pointer no-underline",
        )

    # Group by date
    grouped: dict[str, list] = {}
    for a in acts:
        d = (a.get("startTimeLocal") or "")[:10]
        if not d:
            continue
        try:
            d_obj = date.fromisoformat(d)
        except Exception:
            continue
        today = date.today()
        # Group by stable key (locale-independent); render via i18n at display.
        if d_obj == today:
            key = "today"
        elif d_obj == today - timedelta(days=1):
            key = "yesterday"
        elif (today - d_obj).days <= 7:
            key = "last_7"
        elif (today - d_obj).days <= 30:
            key = "last_30"
        else:
            key = "older"
        grouped.setdefault(key, []).append(a)

    # Recency-tier coloring: brighter / cooler color = fresher. The dot +
    # label color combo gives a one-glance sense of fresh vs stale without
    # reading the label text.
    _DOT_COLOR = {
        "today":     "bg-emerald-400",
        "yesterday": "bg-sky-400",
        "last_7":    "bg-gray-400",
        "last_30":   "bg-gray-500",
        "older":     "bg-gray-600",
    }
    _LABEL_COLOR = {
        "today":     "text-emerald-300",
        "yesterday": "text-sky-300",
        "last_7":    "text-gray-300",
        "last_30":   "text-gray-400",
        "older":     "text-gray-500",
    }

    activity_blocks = []
    first_group = True
    for key in ("today", "yesterday", "last_7", "last_30", "older"):
        if key not in grouped:
            continue
        # Hairline separator before each group (skip the very first — it sits
        # right under the "Recent" section title which has its own border).
        sep_cls = "" if first_group else "border-t border-gray-800/60 mt-3 pt-2"
        activity_blocks.append(
            Div(
                Span(cls=f"inline-block w-1.5 h-1.5 rounded-full {_DOT_COLOR[key]} mr-2 shrink-0"),
                Span(i18n.t(f"sidebar.day.{key}"),
                     cls=f"text-[11px] font-semibold {_LABEL_COLOR[key]} uppercase tracking-wider"),
                cls=f"flex items-center px-4 pb-1.5 {sep_cls}",
            )
        )
        activity_blocks.extend(_act_row(a) for a in grouped[key])
        first_group = False

    overall_active = active_route == "/chat/overall"
    overall_bg = "bg-gray-800" if overall_active else "hover:bg-gray-800/60"

    return Aside(
        # Header section — greeting + race countdown for first-eye-contact
        # delight.
        Div(
            Div(
                Span(greeting_line,
                     cls="text-base font-semibold text-gray-100 truncate"),
                Button("✕", onclick="toggleDrawer()",
                       cls="md:hidden text-gray-400 text-lg ml-auto px-2 shrink-0"),
                cls="flex items-center px-4 pt-3 pb-1"
            ),
            # Race countdown — two lines so the countdown is always visible
            # even when the race name is long / sidebar is mobile-narrow.
            # Line 1 (truncate fallback for very long race names):
            #     🏁 Brighton Half Mara
            # Line 2 (always full width on its own row):
            #     21.1km · 还有 36 天
            (Div(
                Div(
                    Span(race_line[0] + " ", cls="text-sm shrink-0"),
                    Span(race_line[1],
                         cls="text-sm text-gray-100 font-medium truncate"),
                    cls="flex items-baseline px-4",
                ),
                Div(race_line[2],
                    cls="text-xs text-gray-400 px-4 pb-1 truncate"),
             ) if race_line else None),
            Div(sync_text, cls="text-[11px] text-gray-500 px-4 pb-3"),
            cls="border-b border-gray-800",
        ),
        # Action buttons — incremental sync (default fast) / settings ⚙.
        # No manual full-sync button: the empty-data panel forces a full
        # sync on first connect, and disconnect → reconnect re-runs it.
        # An extra manual trigger here only confused users.
        Div(
            Button(i18n.t("sidebar.sync_incremental"),
                   id="sidebar-sync-btn",
                   hx_post="/sync?mode=incremental",
                   hx_target="#sync-toast",
                   hx_swap="innerHTML",
                   cls="flex-[2] text-xs bg-gray-800 hover:bg-gray-700 text-gray-200 py-1.5 rounded"),
            Button(i18n.t("sidebar.settings"),
                   id="sidebar-settings-btn",
                   hx_get="/settings",
                   hx_target="#main",
                   hx_swap="innerHTML show:top",
                   hx_push_url="true",
                   cls="flex-1 text-xs bg-gray-800 hover:bg-gray-700 text-gray-200 py-1.5 rounded"),
            cls="flex px-3 py-2 gap-1",
        ),
        Div(id="sync-toast", cls="px-3 text-[11px] text-gray-400"),
        # Pinned: overall coach chat (cross-activity / trends / planning)
        A(
            Div(
                Span("💬 ", cls="text-base"),
                Span(i18n.t("sidebar.coach_chat"), cls="text-sm text-gray-100"),
                Span(i18n.t("sidebar.coach_chat_sub"),
                     cls="block text-[11px] text-gray-500 mt-0.5"),
                cls="px-3 py-2",
            ),
            href="/chat/overall",
            hx_get="/chat/overall",
            hx_target="#main",
            hx_push_url="true",
            cls=f"block rounded-md mx-2 mt-2 {overall_bg} text-gray-200 cursor-pointer no-underline",
        ),
        # Section header — bigger + bolder + top border so it visually
        # separates from the coach-chat link above and reads as a real
        # section heading (not just another sub-group label).
        Div(i18n.t("sidebar.recent"),
            cls="px-4 pt-5 pb-2 text-sm font-semibold text-gray-200 "
                "border-t border-gray-800 mt-4"),
        *activity_blocks,
        # Note: 断开 Garmin moved to settings page (Danger Zone) — was here
        # as a tiny gray link but got accidentally tapped by users on mobile.
        # Now lives behind ⚙ → bottom of page → red button → confirm dialog.
        id="sidebar",
        cls=_SIDEBAR_CLS,
    )


def Layout(content, active_route: str = "/chat/overall"):
    """Full-page shell — returned only on direct URL loads (no HX-Request).
    Subsequent htmx navigation swaps just the inner panel via _respond()."""
    return (
        Title("tracing.run"),
        Body(
            # Per-request i18n payload for inline JS. Sits at body root so it
            # runs before any DOM-ready handlers (and before the deferred
            # TOUR_STEPS / REPORT_CHIPS / STREAM_STATUS_CHIPS function calls).
            _inject_i18n_script(),
            # Mobile header — always visible at viewport top. Fully opaque
            # (no backdrop-blur translucency) so chat content scrolling beneath
            # doesn't ghost through. Hamburger ☰ stays reachable everywhere.
            # Right side: optional "📱 添加到桌面" hint (only iOS Safari, not
            # already standalone, not dismissed in last 30d). JS reveals it
            # on load via maybeShowInstallButton() — see init below.
            Div(
                Button("☰", onclick="toggleDrawer()",
                       cls="text-2xl text-gray-300 px-3 py-2"),
                Span("tracing.run", cls="text-base font-semibold text-gray-100"),
                Div(
                    Button(i18n.t("mobile.add_to_home"),
                           onclick="showInstallHint()",
                           cls="text-[11px] text-gray-300 bg-gray-800 "
                               "hover:bg-gray-700 px-2.5 py-1 rounded-l-full "
                               "border-r border-gray-900"),
                    Button("✕",
                           onclick="dismissInstallHint(event)",
                           title=i18n.t("mobile.dismiss_hint"),
                           cls="text-[11px] text-gray-400 bg-gray-800 "
                               "hover:bg-gray-700 px-2 py-1 rounded-r-full"),
                    id="install-hint-tag",
                    cls="hidden ml-auto mr-2 flex items-center",
                ),
                id="mobile-header",
                # `pt-[env(safe-area-inset-top)]` is 0 in Safari (its own
                # chrome already pads the viewport) but expands to the iOS
                # status-bar height (~47-59px) when launched as a standalone
                # PWA from the home screen — otherwise the time/signal/
                # battery glyphs overlap the hamburger + title row.
                cls="md:hidden flex items-center bg-gray-900 "
                    "border-b border-gray-800 fixed top-0 left-0 right-0 z-20 "
                    "pt-[env(safe-area-inset-top)]",
            ),
            # Spacer so first message doesn't hide under the fixed mobile
            # header. Grows alongside the header in standalone PWA mode so
            # content doesn't get pushed under the extra safe-area padding.
            Div(cls="md:hidden h-[calc(3rem+env(safe-area-inset-top))]"),
            # Drawer overlay (mobile)
            Div(onclick="toggleDrawer()",
                id="drawer-overlay",
                cls="hidden fixed inset-0 bg-black/50 z-20 md:hidden"),
            # Sidebar
            Sidebar(active_route),
            # Main
            Main(content, id="main",
                 cls="md:ml-72 min-h-screen flex flex-col"),
            # Global reconnect dialog — opens when SessionExpiredError or
            # EmptyDetailFetchError is caught (sync / first-click fetch).
            # The reconnect path wipes ONLY .garth_session/ → forces fresh
            # OAuth login; ALL data (SQLite + chats + insights) is preserved.
            Dialog(
                Div(
                    H3(i18n.t("reconnect.title"),
                       cls="text-base font-semibold text-yellow-400 mb-3"),
                    P(i18n.t("reconnect.body"),
                      cls="text-sm text-gray-200 mb-2"),
                    P(Span(i18n.t("reconnect.hint_lead"), cls="font-semibold text-gray-100"),
                      i18n.t("reconnect.hint_body"),
                      Span(i18n.t("reconnect.hint_kept"), cls="font-semibold text-emerald-300"),
                      i18n.t("reconnect.hint_tail"),
                      cls="text-xs text-gray-400 mb-5"),
                    Div(
                        Button(
                            i18n.t("reconnect.dismiss"),
                            type="button",
                            onclick="document.getElementById('reconnect-dlg').close()",
                            cls="flex-1 bg-gray-700 hover:bg-gray-600 text-gray-100 "
                                "px-4 py-2 rounded text-sm",
                        ),
                        # Native form POST (not htmx) so the 303 redirect
                        # back to / fully reloads the page → routes through
                        # login flow naturally.
                        Form(
                            Button(
                                i18n.t("reconnect.cta"),
                                type="submit",
                                cls="w-full bg-yellow-600 hover:bg-yellow-500 text-white "
                                    "px-4 py-2 rounded text-sm font-medium",
                            ),
                            method="post",
                            action="/auth/reconnect",
                            cls="flex-1",
                        ),
                        cls="flex gap-2",
                    ),
                    cls="p-5",
                ),
                id="reconnect-dlg",
                cls="app-dialog",
            ),
            # iOS "Add to Home Screen" instructions overlay — opens when the
            # 📱 tag in the mobile header is tapped. iOS Safari has no
            # programmatic install API, so the best we can do is a clear
            # 3-step illustration. Plain absolute-positioned div (not <dialog>)
            # so it works inside iOS Safari's inert handling quirks.
            Div(
                Div(
                    Span("✕", onclick="closeInstallHint()",
                         cls="absolute top-3 right-3 text-gray-400 text-xl "
                             "cursor-pointer leading-none px-2"),
                    Div("📱", cls="text-5xl text-center mb-2"),
                    H3(i18n.t("install.title"),
                       cls="text-base font-semibold text-gray-100 text-center mb-1"),
                    P(i18n.t("install.subtitle"),
                      cls="text-xs text-gray-400 text-center mb-5"),
                    Div(
                        P(Span("①", cls="font-bold text-blue-400 mr-2 text-base"),
                          i18n.t("install.step1_a"),
                          Span("⬆", cls="text-base mx-0.5"),
                          i18n.t("install.step1_b"),
                          cls="text-sm text-gray-100 mb-3"),
                        P(Span("②", cls="font-bold text-blue-400 mr-2 text-base"),
                          i18n.t("install.step2_a"),
                          Span(i18n.t("install.step2_pill"),
                               cls="bg-gray-800 text-gray-100 px-1.5 py-0.5 rounded text-xs"),
                          cls="text-sm text-gray-100 mb-3"),
                        P(Span("③", cls="font-bold text-blue-400 mr-2 text-base"),
                          i18n.t("install.step3_a"),
                          Span(i18n.t("install.step3_pill"),
                               cls="bg-blue-600 text-white px-1.5 py-0.5 rounded text-xs"),
                          cls="text-sm text-gray-100 mb-1"),
                        cls="mb-5",
                    ),
                    Div(
                        Button(i18n.t("install.dismiss"),
                               type="button",
                               onclick="dismissInstallHint(event)",
                               cls="flex-1 bg-gray-700 hover:bg-gray-600 "
                                   "text-gray-100 px-4 py-2 rounded text-sm"),
                        Button(i18n.t("install.ack"),
                               type="button",
                               onclick="closeInstallHint()",
                               cls="flex-1 bg-blue-600 hover:bg-blue-500 "
                                   "text-white px-4 py-2 rounded text-sm ml-2"),
                        cls="flex",
                    ),
                    cls="relative bg-gray-900 border border-gray-800 "
                        "rounded-2xl p-6 max-w-sm w-full",
                ),
                id="install-hint-overlay",
                onclick="(event.target===this) && closeInstallHint()",
                cls="hidden fixed inset-0 bg-black/70 z-30 "
                    "items-center justify-center p-6",
            ),
            # Global pin dialog — opens via openPinDialog(btn). Lives at
            # body root so showModal() backdrop covers full viewport.
            Dialog(
                Div(
                    H3(i18n.t("pin.dlg.title"),
                       cls="text-base font-semibold text-gray-100 mb-2"),
                    P(i18n.t("pin.dlg.body"),
                      cls="text-xs text-gray-400 mb-3"),
                    Textarea(
                        "",
                        id="pin-text",
                        rows="6",
                        cls="w-full bg-gray-800 text-gray-100 rounded "
                            "px-3 py-2 border border-gray-700 text-sm "
                            "focus:border-blue-500 focus:outline-none",
                    ),
                    Div(id="pin-status",
                        cls="text-xs text-gray-500 mt-1 min-h-[1em]"),
                    Div(
                        Button(i18n.t("pin.dlg.distill"),
                               type="button",
                               id="pin-distill-btn",
                               onclick="distillPin()",
                               cls="text-xs bg-purple-700 hover:bg-purple-600 "
                                   "text-white px-3 py-1.5 rounded shrink-0"),
                        Span("", cls="flex-1"),
                        Button(i18n.t("pin.dlg.cancel"), type="button",
                               onclick="document.getElementById('pin-dlg').close()",
                               cls="text-sm bg-gray-700 hover:bg-gray-600 "
                                   "text-gray-100 px-4 py-1.5 rounded shrink-0"),
                        Button(i18n.t("pin.dlg.save"),
                               type="button",
                               id="pin-save-btn",
                               onclick="savePin()",
                               cls="text-sm bg-blue-600 hover:bg-blue-500 "
                                   "text-white px-4 py-1.5 rounded shrink-0"),
                        cls="flex items-center gap-2 mt-3",
                    ),
                    cls="p-5",
                ),
                id="pin-dlg",
                cls="app-dialog",
            ),
            cls="bg-gray-950 text-gray-100",
        ),
    )


_JS_I18N_KEYS = (
    "js.loading",
    "js.tour.skip", "js.tour.next", "js.tour.done",
    "js.tour.step1.title", "js.tour.step1.text",
    "js.tour.step2.title", "js.tour.step2.text",
    "js.tour.step3.title", "js.tour.step3.text",
    "js.tour.step4.title", "js.tour.step4.text",
    "js.tour.step5.title", "js.tour.step5.text",
    "js.pin.distilling", "js.pin.distilled", "js.pin.distill_empty",
    "js.pin.distill_fail", "js.pin.saving", "js.pin.saved",
    "js.pin.save_fail", "js.pin.save_btn",
    "js.note.empty", "js.note.organizing", "js.note.organized",
    "js.note.organize_empty", "js.note.organize_fail",
    "js.report.chip0", "js.report.chip1", "js.report.chip2",
    "js.report.chip3", "js.report.chip4",
    "js.report.pill.done", "js.report.pill.fail", "js.report.fail_msg",
    "js.stream.chip0", "js.stream.chip1", "js.stream.chip2",
    "js.stream.chip3", "js.stream.chip4",
)


def _inject_i18n_script():
    """Build a tiny <script> that exposes the per-request locale's JS-side
    strings as `window.I18N`. Injected by Layout before any inline JS reads
    these (the static head JS defers TOUR_STEPS / REPORT_CHIPS / etc. to
    function form so they look up I18N lazily at call time)."""
    payload = json.dumps({k: i18n.t(k) for k in _JS_I18N_KEYS}, ensure_ascii=False)
    return Script(NotStr(f"window.I18N = {payload};"))


def _respond(panel, active_route: str, req=None):
    """Return panel-only for htmx swaps, full Layout for direct URL loads.

    Also includes an out-of-band sidebar refresh on htmx requests so the
    'active' highlight on the new chat tracks correctly. The OOB element
    uses the same id="sidebar" → htmx replaces the existing sidebar in place
    without a full page reload."""
    if req and req.headers.get("hx-request"):
        sidebar = Sidebar(active_route)
        # Mark sidebar for out-of-band swap so it replaces the existing one
        sidebar.attrs["hx-swap-oob"] = "outerHTML"
        return panel, sidebar
    return Layout(panel, active_route)


# ── Chat rendering ─────────────────────────────────────────────────────────────
def _pin_button():
    """Tiny gray '📌 add to memory' link under each assistant bubble. Click
    opens the global #pin-dlg with the bubble's text pre-filled."""
    return Button(
        i18n.t("pin.btn"),
        type="button",
        onclick="openPinDialog(this)",
        cls="pin-btn text-[10px] text-gray-600 hover:text-gray-300 "
            "mt-1 ml-1 self-start",
    )


def MsgBubble(role: str, content: str, *, raw: bool = True):
    if role == "user":
        return Div(
            Div(content, cls="bg-blue-600 text-white px-4 py-2 rounded-2xl rounded-br-sm max-w-[85%] whitespace-pre-wrap break-words"),
            cls="flex justify-end mb-3",
        )
    # assistant — render markdown via JS, content stored in data-raw.
    # Layout switched to flex-col so the 📌 pin link can stack below the
    # bubble (vs in a flex row to its right where it'd misalign on mobile).
    return Div(
        Div(content,
            cls="assistant-bubble prose-coach text-gray-100 px-4 py-2 max-w-[85%]",
            **{"data-raw": content if raw else "", "data-rendered": "0"}),
        _pin_button(),
        cls="msg-row flex flex-col items-start mb-3",
    )


def StreamingBubble(token: str):
    """Empty assistant bubble. JS opens an EventSource to /sse/<token>,
    appends each 'chunk' event to dataset.raw, then renders markdown on
    'done'. See the inline scanBubbles/startStream functions in the page hdr."""
    return Div(
        Div("",
            id=f"stream-{token}",
            cls="assistant-bubble prose-coach text-gray-100 px-4 py-2 max-w-[85%] typing",
            **{"data-raw": "", "data-rendered": "0",
               "data-stream-url": f"/sse/{token}"}),
        _pin_button(),
        cls="msg-row flex flex-col items-start mb-3",
    )


def _random_placeholder(key: str) -> str:
    """Pick a random placeholder from a pipe-separated pool in the i18n
    catalog. Re-rolls per render so the chat input shows variety across
    page loads — no LLM call, just a static rotation."""
    pool = i18n.t(key)
    options = [s.strip() for s in pool.split("|") if s.strip()]
    return random.choice(options) if options else pool


def ChatPanel(*, kind: str, activity_id: int | None, header, messages: list, model: str):
    """Render the main chat pane.
    kind = "overall" | "activity"

    Layout: body is the sole scroller. Per-chat header flows naturally (scrolls
    away with content). Input bar is position:fixed at viewport bottom, offset
    by sidebar width on desktop (md:left-72). Messages container has bottom
    padding so the last message isn't hidden behind the input bar."""
    if kind == "overall":
        send_url = "/chat/overall/send"
        clear_url = "/chat/overall/clear"
        placeholder = _random_placeholder("chat.overall.placeholder")
    else:
        send_url = f"/chat/activity/{activity_id}/send"
        clear_url = f"/chat/activity/{activity_id}/clear"
        placeholder = _random_placeholder("chat.activity.placeholder")

    bubbles = [MsgBubble(m["role"], m["content"]) for m in messages
               if m["role"] in ("user", "assistant") and m.get("content")]
    is_empty_overall = (kind == "overall" and not bubbles)
    if not bubbles:
        bubbles = [Div(i18n.t("chat.empty"), cls="text-center text-gray-500 text-sm py-12")]

    # Suggestion chips container. Two modes:
    #  - empty /chat/overall: skeleton + htmx auto-trigger to LLM-generate
    #    3 inspiration starter chips, swapped into innerHTML on completion.
    #  - everything else: empty container; JS updateChips() populates after
    #    each assistant `done` event from the <follow_ups> sentinel block.
    if is_empty_overall:
        chips_div = Div(
            _starter_chip_skeleton(),
            id="follow-up-chips",
            hx_get="/chat/overall/starter-chips",
            hx_trigger="load",
            hx_swap="innerHTML",
            cls="max-w-3xl mx-auto flex gap-2 overflow-x-auto no-scrollbar pb-2",
        )
    else:
        chips_div = Div(
            id="follow-up-chips",
            cls="max-w-3xl mx-auto flex gap-2 overflow-x-auto no-scrollbar "
                "pb-2 empty:pb-0 empty:hidden",
        )

    return Div(
        # Per-chat header — natural flow, scrolls away with content
        header,
        # Messages — natural flow inside body's scroll. Bottom padding
        # equals input bar height + iOS safe-area, so last message isn't
        # eaten by the fixed input bar.
        Div(*bubbles,
            id="chat-scroll",
            cls="px-4 py-4 max-w-3xl mx-auto w-full "
                "pb-[calc(7rem+env(safe-area-inset-bottom))]"),
        # Input bar — fixed at viewport bottom, sidebar-aware on desktop.
        # Includes the dynamic follow-up chips bar (filled by JS extract +
        # updateChips on each assistant `done` event, OR by the htmx-loaded
        # starter chips for empty overall chats).
        Div(
            chips_div,
            Form(
                Textarea("",
                         name="msg",
                         placeholder=placeholder,
                         onkeydown="sendOnEnter(event)",
                         rows="1",
                         cls="flex-1 bg-gray-800 text-gray-100 rounded-xl px-4 py-2.5 resize-none border border-gray-700 focus:border-blue-500 focus:outline-none text-sm leading-snug"),
                Button(i18n.t("chat.send"),
                       type="submit",
                       cls="bg-blue-600 hover:bg-blue-500 text-white px-4 py-2 rounded-xl text-sm font-medium ml-2"),
                hx_post=send_url,
                hx_target="#chat-scroll",
                hx_swap="beforeend",
                hx_include="[name=msg]",
                **{"data-clear-on-send": "1"},
                cls="flex items-end max-w-3xl mx-auto w-full",
            ),
            Div(
                Span(i18n.t("chat.model_pfx", model=model), cls="text-[10px] text-gray-500"),
                A(i18n.t("chat.clear"), href="#",
                  hx_post=clear_url,
                  hx_target="#main",
                  hx_swap="innerHTML",
                  hx_confirm=i18n.t("chat.clear_confirm"),
                  cls="text-[10px] text-gray-500 hover:text-gray-300 ml-auto cursor-pointer"),
                cls="flex max-w-3xl mx-auto w-full mt-1",
            ),
            cls="fixed bottom-0 left-0 right-0 md:left-72 z-10 "
                "border-t border-gray-800 bg-gray-950 "
                "px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))]",
        ),
    )


def ActivityChatPanel(*, aid: int, header, report_card, messages: list, model: str):
    """Activity review page: header (title + comment + charts) → report card →
    follow-up chat history → input bar.

    The report (msg_index=0 in chat_review) is rendered separately in
    `report_card`; the chat list shows ONLY follow-up Q&A (msg_index >= 1)."""
    bubbles = [MsgBubble(m["role"], m["content"]) for m in messages
               if m["role"] in ("user", "assistant") and m.get("content")]
    if not bubbles:
        bubbles = [Div(i18n.t("chat.activity.empty_pre_report"),
                       cls="text-center text-gray-500 text-sm py-12")]

    chips_div = Div(
        id="follow-up-chips",
        cls="max-w-3xl mx-auto flex gap-2 overflow-x-auto no-scrollbar "
            "pb-2 empty:pb-0 empty:hidden",
    )
    return Div(
        header,
        report_card,
        Div(*bubbles,
            id="chat-scroll",
            cls="px-4 py-4 max-w-3xl mx-auto w-full "
                "pb-[calc(7rem+env(safe-area-inset-bottom))]"),
        Div(
            chips_div,
            Form(
                Textarea("",
                         name="msg",
                         placeholder=_random_placeholder("chat.activity.placeholder"),
                         onkeydown="sendOnEnter(event)",
                         rows="1",
                         cls="flex-1 bg-gray-800 text-gray-100 rounded-xl px-4 py-2.5 resize-none border border-gray-700 focus:border-blue-500 focus:outline-none text-sm leading-snug"),
                Button(i18n.t("chat.send"),
                       type="submit",
                       cls="bg-blue-600 hover:bg-blue-500 text-white px-4 py-2 rounded-xl text-sm font-medium ml-2"),
                hx_post=f"/chat/activity/{aid}/send",
                hx_target="#chat-scroll",
                hx_swap="beforeend",
                hx_include="[name=msg]",
                **{"data-clear-on-send": "1"},
                cls="flex items-end max-w-3xl mx-auto w-full",
            ),
            Div(
                Span(i18n.t("chat.model_pfx", model=model), cls="text-[10px] text-gray-500"),
                A(i18n.t("chat.clear"), href="#",
                  hx_post=f"/chat/activity/{aid}/clear",
                  hx_target="#main",
                  hx_swap="innerHTML",
                  hx_confirm=i18n.t("chat.clear_confirm_full"),
                  cls="text-[10px] text-gray-500 hover:text-gray-300 ml-auto cursor-pointer"),
                cls="flex max-w-3xl mx-auto w-full mt-1",
            ),
            cls="fixed bottom-0 left-0 right-0 md:left-72 z-10 "
                "border-t border-gray-800 bg-gray-950 "
                "px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))]",
        ),
    )


# ── Activity stats panel (collapsible — like 课表/备注) ─────────────────────────
def _chart_hr_pace_elev_html(metrics: dict, is_run: bool) -> str:
    ts_raw = metrics.get("ts", [])
    x_min  = _elapsed_minutes(ts_raw)
    hr     = metrics.get("hr", [])
    speed  = metrics.get("speed", [])
    elev   = metrics.get("elev", [])
    pace_s    = [1000 / s if (s and s > 0) else None for s in speed]
    speed_kmh = [s * 3.6  if s              else None for s in speed]

    fig = go.Figure()
    if any(v is not None for v in hr):
        fig.add_trace(go.Scatter(
            x=x_min, y=hr, mode="lines", name=i18n.t("chart.hr_bpm"),
            line=dict(color="#f87171", width=1.5),
            hovertemplate="%{x:.1f} min<br>%{y:.0f} bpm<extra></extra>",
        ))
    if is_run and any(v is not None for v in pace_s):
        tickvals, ticktext = _pace_ticks(pace_s)
        fig.add_trace(go.Scatter(
            x=x_min, y=pace_s, mode="lines", name=i18n.t("chart.pace_min_km"),
            yaxis="y2", line=dict(color="#60a5fa", width=1.5),
            customdata=[f"{int(p)//60}:{int(p)%60:02d}/km" if p else "—"
                        for p in pace_s],
            hovertemplate="%{x:.1f} min<br>%{customdata}<extra></extra>",
        ))
        fig.update_layout(yaxis2=dict(
            overlaying="y", side="right", autorange="reversed",
            tickfont=dict(size=9, color="#60a5fa"),
            tickvals=tickvals, ticktext=ticktext,
        ))
    elif any(v is not None for v in speed_kmh):
        fig.add_trace(go.Scatter(
            x=x_min, y=speed_kmh, mode="lines", name=i18n.t("chart.speed_kmh"),
            yaxis="y2", line=dict(color="#60a5fa", width=1.5),
            hovertemplate="%{x:.1f} min<br>%{y:.1f} km/h<extra></extra>",
        ))
        fig.update_layout(yaxis2=dict(overlaying="y", side="right",
                                       tickfont=dict(size=9, color="#60a5fa")))
    if any(v is not None for v in elev):
        fig.add_trace(go.Scatter(
            x=x_min, y=elev, mode="lines", name=i18n.t("chart.elev_m"),
            fill="tozeroy", fillcolor="rgba(150,150,150,0.18)",
            line=dict(color="gray", width=1), yaxis="y3",
            hovertemplate="%{x:.1f} min<br>%{y:.0f} m<extra></extra>",
        ))
        fig.update_layout(yaxis3=dict(overlaying="y", side="right", position=0.97,
                                       anchor="free", showticklabels=False))
    fig.update_layout(
        **_PLOTLY_LAYOUT_BASE,
        yaxis=dict(tickfont=dict(size=9, color="#f87171")),
        xaxis=dict(title=i18n.t("chart.duration_min"), title_font=dict(size=10), tickfont=dict(size=9)),
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.0,
                    xanchor="right", x=1, font=dict(size=10)),
    )
    return _fig_to_html(fig)


def _chart_dynamics_html(metrics: dict) -> str:
    cad = metrics.get("cadence", [])
    gct = metrics.get("gct", [])
    x_min = _elapsed_minutes(metrics.get("ts", []))
    if not any(v for v in cad if v):
        return ""

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=x_min, y=[c if c else None for c in cad], mode="lines",
        name=i18n.t("chart.cadence_spm"), line=dict(color="#c084fc", width=1.5),
        hovertemplate="%{x:.1f} min<br>%{y:.0f} spm<extra></extra>",
    ))
    if any(v for v in gct if v):
        fig.add_trace(go.Scatter(
            x=x_min, y=[g if g else None for g in gct], mode="lines",
            name=i18n.t("chart.gct_ms"), yaxis="y2", line=dict(color="#fb923c", width=1.5),
            hovertemplate="%{x:.1f} min<br>%{y:.0f} ms<extra></extra>",
        ))
        fig.update_layout(yaxis2=dict(overlaying="y", side="right",
                                       tickfont=dict(size=9, color="#fb923c")))
    fig.update_layout(
        **{**_PLOTLY_LAYOUT_BASE, "margin": dict(l=8, r=8, t=10, b=24)},
        yaxis=dict(tickfont=dict(size=9, color="#c084fc")),
        xaxis=dict(title=i18n.t("chart.duration_min"), title_font=dict(size=10), tickfont=dict(size=9)),
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.0,
                    xanchor="right", x=1, font=dict(size=10)),
    )
    return _fig_to_html(fig)


def _chart_map_html(gps: list) -> str:
    """Route map with start/end markers. Uses Plotly's Scattermap (MapLibre,
    OSM tiles — no token needed). carto-darkmatter style matches the dark
    theme. staticPlot=True so the map doesn't capture horizontal swipe in
    the carousel (matches the other charts)."""
    if not gps:
        return ""
    lats = [p["lat"] for p in gps if p.get("lat")]
    lons = [p["lon"] for p in gps if p.get("lon")]
    if not lats or not lons:
        return ""

    fig = go.Figure()
    # Route polyline
    fig.add_trace(go.Scattermap(
        lat=lats, lon=lons, mode="lines",
        line=dict(color="#60a5fa", width=3),
        hoverinfo="skip", name=i18n.t("chart.route"),
    ))
    # Start (green) + end (red) markers
    fig.add_trace(go.Scattermap(
        lat=[lats[0]], lon=[lons[0]],
        mode="markers", marker=dict(size=12, color="#22c55e"),
        hoverinfo="skip", name=i18n.t("chart.start"),
    ))
    fig.add_trace(go.Scattermap(
        lat=[lats[-1]], lon=[lons[-1]],
        mode="markers", marker=dict(size=12, color="#ef4444"),
        hoverinfo="skip", name=i18n.t("chart.end"),
    ))

    # Auto-fit center + zoom — Plotly's Scattermap doesn't auto-fit, so we
    # compute the bounding box and pick a zoom level for it.
    lat_min, lat_max = min(lats), max(lats)
    lon_min, lon_max = min(lons), max(lons)
    center_lat = (lat_min + lat_max) / 2
    center_lon = (lon_min + lon_max) / 2
    # Rough zoom heuristic — picks something sensible for typical run distances
    span = max(lat_max - lat_min, lon_max - lon_min) or 0.01
    if   span < 0.005: zoom = 14.5
    elif span < 0.01:  zoom = 13.5
    elif span < 0.02:  zoom = 13
    elif span < 0.05:  zoom = 12
    elif span < 0.1:   zoom = 11
    elif span < 0.2:   zoom = 10
    else:              zoom = 9

    fig.update_layout(
        map=dict(
            style="carto-darkmatter",
            center=dict(lat=center_lat, lon=center_lon),
            zoom=zoom,
        ),
        showlegend=False,
        margin=dict(l=0, r=0, t=0, b=0),
        paper_bgcolor="rgba(0,0,0,0)",
    )
    return _fig_to_html(fig)


def _chart_hr_zones_html(hr_zones: list) -> str:
    if not hr_zones:
        return ""
    labels = [f"Z{z.get('zoneNumber', i+1)}" for i, z in enumerate(hr_zones)]
    mins   = [round(z.get("secsInZone", 0) / 60, 1) for z in hr_zones]
    colors = ["#5cb85c", "#5bc0de", "#f0ad4e", "#d9534f", "#c0392b"]
    fig = go.Figure(go.Bar(
        x=labels, y=mins, marker_color=colors[:len(labels)],
        text=[f"{m:.0f}min" for m in mins], textposition="outside",
    ))
    fig.update_layout(
        **{**_PLOTLY_LAYOUT_BASE, "margin": dict(l=0, r=0, t=20, b=0)},
        yaxis=dict(showticklabels=False), showlegend=False,
    )
    return _fig_to_html(fig)


def _laps_table(laps: list, is_run: bool):
    """Per-lap table — pure HTML, no chart lib needed."""
    headers = [
        i18n.t("lap.col_index"),
        i18n.t("lap.col_distance"),
        i18n.t("lap.col_duration"),
        i18n.t("lap.col_pace") if is_run else i18n.t("lap.col_speed"),
        i18n.t("lap.col_hr"),
        i18n.t("lap.col_cadence"),
        i18n.t("lap.col_gct"),
    ]
    rows = []
    for i, lap in enumerate(laps, 1):
        dist = (lap.get("distance") or 0) / 1000
        dur  = gd.format_duration(lap.get("duration"))
        spd  = lap.get("averageSpeed") or 0
        pace = gd.format_pace(spd) if is_run else (f"{spd*3.6:.1f}km/h" if spd else "—")
        hr   = lap.get("averageHR")
        cad  = lap.get("averageRunCadence")
        gct  = lap.get("groundContactTime")
        rows.append((i, f"{dist:.2f}km", dur, pace,
                     f"{int(hr)}" if hr else "—",
                     f"{int(cad)}" if cad else "—",
                     f"{int(gct)}" if gct else "—"))

    def _th(s):
        return Div(s, cls="text-xs font-medium text-gray-400 px-2 py-1.5 text-left border-b border-gray-800")

    def _td(s):
        return Div(str(s), cls="text-xs text-gray-200 px-2 py-1.5 text-left border-b border-gray-900")

    return Div(
        Div(*[_th(h) for h in headers],
            cls="grid grid-cols-[2rem_5rem_4rem_5rem_3rem_3rem_3rem] bg-gray-900"),
        *[Div(*[_td(c) for c in r],
              cls="grid grid-cols-[2rem_5rem_4rem_5rem_3rem_3rem_3rem] hover:bg-gray-900/50")
          for r in rows],
        cls="rounded border border-gray-800 overflow-x-auto",
    )


def _stats_grid(summary: dict, type_key: str):
    """Compact key-stats grid (distance / duration / pace / HR / TE / etc.)."""
    is_run = "run" in (type_key or "")
    cells = []
    def _cell(label, value):
        if not value or value == "—":
            return None
        return Div(
            Div(label, cls="text-[10px] text-gray-500 uppercase tracking-wide"),
            Div(value, cls="text-sm text-gray-100 font-medium mt-0.5"),
            cls="px-2 py-2",
        )

    dist = (summary.get("distance") or 0) / 1000
    cells.append(_cell(i18n.t("stats.distance"), f"{dist:.2f} km" if dist else "—"))
    cells.append(_cell(i18n.t("stats.duration"), gd.format_duration(summary.get("duration"))))
    spd = summary.get("averageSpeed")
    cells.append(_cell(i18n.t("stats.avg_pace") if is_run else i18n.t("stats.avg_speed"),
                        gd.format_pace(spd) if (is_run and spd)
                        else (f"{spd*3.6:.1f} km/h" if spd else "—")))
    avg_hr = summary.get("averageHR")
    max_hr = summary.get("maxHR")
    if avg_hr or max_hr:
        cells.append(_cell(i18n.t("stats.hr"), f"{int(avg_hr) if avg_hr else '—'} / {int(max_hr) if max_hr else '—'} bpm"))
    eg = summary.get("elevationGain")
    if eg and eg > 1:
        cells.append(_cell(i18n.t("stats.elev_gain"), f"{int(eg)} m"))
    cal = summary.get("calories")
    if cal:
        cells.append(_cell(i18n.t("stats.calories"), f"{int(cal)} kcal"))
    tl = summary.get("activityTrainingLoad")
    if tl:
        cells.append(_cell(i18n.t("stats.training_load"), f"{int(tl)}"))
    te = summary.get("aerobicTrainingEffect")
    if te:
        te_key = summary.get("trainingEffectLabel", "")
        label = i18n.t(f"te_label.{te_key}") if te_key in gd.TE_LABEL_MAP else ""
        cells.append(_cell(i18n.t("stats.te"), f"{te:.1f}" + (f" ({label})" if label else "")))
    vo2 = summary.get("vO2MaxValue")
    if vo2:
        cells.append(_cell(i18n.t("stats.vo2max"), f"{int(vo2)}"))
    # Running dynamics summary (from summary, not metrics)
    cad = summary.get("averageRunCadence")
    if cad:
        cells.append(_cell(i18n.t("stats.avg_cadence"), f"{int(cad)} spm"))
    gctv = summary.get("groundContactTime")
    if gctv:
        cells.append(_cell(i18n.t("stats.avg_gct"), f"{int(gctv)} ms"))
    stride = summary.get("strideLength")
    if stride:
        cells.append(_cell(i18n.t("stats.avg_stride"), f"{int(stride)} cm"))
    vr = summary.get("verticalRatio")
    if vr:
        cells.append(_cell(i18n.t("stats.vert_ratio"), f"{vr:.1f}%"))

    return Div(
        *[c for c in cells if c is not None],
        cls="grid grid-cols-2 md:grid-cols-4 gap-1 bg-gray-900/40 rounded border border-gray-800",
    )


def _activity_stats_panel(act_id: int, *, open_default: bool = False):
    """Collapsible 🔍 训练数据详情 — horizontal swipeable cards.

    open_default=True is used in the pre-report nudge state — user just
    waited for fetch, surface the data immediately so they can recall what
    they ran while filling the type/comment form. Once a chat exists,
    panel defaults to collapsed (less clutter while reading the report).

    Card 1: stats grid (distance / duration / HR / TE / calories etc.)
    Card 2: HR + pace + elevation chart
    Card 3: running dynamics (cadence + GCT)
    Card 4: HR zones bar chart
    Card 5: per-lap table

    Implementation: CSS scroll-snap on the row (native, no JS for scrolling).
    Plotly charts use staticPlot=True so horizontal touch gestures aren't
    captured by the chart's pan/zoom — they pass through to the carousel.
    No explicit touch-action on inner wrappers: previously the cards had
    `touch-action: pan-y` as belt-and-suspenders, but it caused the first
    horizontal swipe inside a card to be reinterpreted as vertical-only and
    fail (had to swipe on the margin to escape). Default `auto` lets the
    browser route horizontal swipes to the parent carousel correctly.
    Dots indicator updates current page via a tiny scroll listener (in scanBubbles)."""
    with db.connect() as conn:
        detail = db.get_activity_detail(conn, act_id)
        row = conn.execute(
            "SELECT activity_type_key FROM activities WHERE activity_id=?",
            (act_id,)
        ).fetchone()
    if not detail:
        return Details(
            Summary(i18n.t("carousel.title"), cls="text-xs text-gray-400 cursor-pointer max-w-3xl mx-auto"),
            Div(i18n.t("carousel.no_data"), cls="text-xs text-gray-500 max-w-3xl mx-auto mt-2"),
            cls="mt-2",
        )

    type_key = row[0] if row else ""
    is_run   = "run" in (type_key or "")
    metrics  = detail.get("metrics") or {}
    laps     = detail.get("laps") or []
    hr_zones = detail.get("hr_zones") or []
    summary  = detail.get("summary") or {}
    has_ts   = bool(metrics.get("ts")) and any(v is not None for v in metrics.get("ts", []))
    has_cad  = any(v for v in metrics.get("cadence", []) if v)

    gps      = detail.get("gps") or []
    cards: list[tuple[str, object]] = [(i18n.t("carousel.card_stats"), _stats_grid(summary, type_key))]
    if gps:
        cards.append((i18n.t("carousel.card_route"), NotStr(_chart_map_html(gps))))
    if has_ts:
        cards.append((i18n.t("carousel.card_hr_pace_elev"),
                      NotStr(_chart_hr_pace_elev_html(metrics, is_run))))
    if is_run and has_cad:
        cards.append((i18n.t("carousel.card_dynamics"),
                      NotStr(_chart_dynamics_html(metrics))))
    if hr_zones:
        cards.append((i18n.t("carousel.card_hr_zones"),
                      NotStr(_chart_hr_zones_html(hr_zones))))
    if laps:
        # Lap table can be tall (20+ rows for intervals). Cap height +
        # internal vertical scroll. Default touch-action (auto) means
        # horizontal swipes bubble to the parent carousel.
        cards.append((i18n.t("carousel.card_laps"),
                      Div(_laps_table(laps, is_run),
                          cls="max-h-72 overflow-y-auto")))

    # Build card divs. Each is full-width and snaps to center.
    card_divs = [
        Div(
            Div(label, cls="text-xs text-gray-400 mb-2 text-center"),
            Div(content),
            cls="snap-center shrink-0 w-full px-1",
        )
        for label, content in cards
    ]

    # Pagination dots — first one highlighted by default, JS updates on scroll
    dots = [
        Span(id=f"dot-{act_id}-{i}",
             cls=("inline-block w-1.5 h-1.5 rounded-full mx-1 transition-colors "
                  + ("bg-gray-200" if i == 0 else "bg-gray-700")))
        for i in range(len(cards))
    ]

    # `open` attr: HTML <details> opens by default when present (any value).
    # When open_default=True (nudge state), pass open=True; FastHTML emits
    # the bare attribute. When False, omit the kwarg entirely so the panel
    # stays collapsed.
    details_kwargs = {"open": True} if open_default else {}
    return Details(
        Summary(i18n.t("carousel.title_swipe", n=len(cards)),
                cls="text-xs text-gray-400 cursor-pointer max-w-3xl mx-auto py-1"),
        Div(
            # The carousel row
            Div(*card_divs,
                id=f"carousel-{act_id}",
                cls="flex overflow-x-auto snap-x snap-mandatory gap-2 "
                    "pb-2 min-h-[300px] no-scrollbar"),
            # Dots indicator
            Div(*dots, cls="flex justify-center mt-1"),
            cls="max-w-3xl mx-auto mt-3",
        ),
        cls="mt-2",
        **details_kwargs,
    )


# ── Routes: home + login + sync ────────────────────────────────────────────────
@rt_route("/")
def home():
    if not _have_session():
        return Layout(LoginPanel(), active_route="/login")
    if not _have_data():
        return Layout(EmptyDataPanel(), active_route="/")
    return RedirectResponse("/chat/overall", status_code=303)


# Web app manifest — without this, iOS Safari uses heuristics to decide the
# PWA "scope" (often the directory of the URL that was added to home screen).
# Result: launching the PWA from /chat/overall would treat /settings as
# "out of scope" and pop a Safari in-app browser modal (X / URL / refresh
# chrome) instead of staying in standalone. Declaring `scope: "/"` puts
# every route inside the app. Existing installations need to be removed +
# re-added from home screen to pick this up.
_MANIFEST_JSON = json.dumps({
    "name": "tracing.run",
    "short_name": "tracing.run",
    "start_url": "/chat/overall",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0b0f17",
    "theme_color": "#111827",
    "icons": [{
        "src": ("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' "
                "viewBox='0 0 180 180'><rect width='180' height='180' rx='40' "
                "fill='%23111827'/><text x='90' y='133' text-anchor='middle' "
                "font-size='110'>🛰️</text></svg>"),
        "sizes": "180x180",
        "type": "image/svg+xml",
    }],
})

@rt_route("/manifest.json")
def get_manifest():
    return Response(_MANIFEST_JSON, media_type="application/manifest+json")


# ── Language switcher ────────────────────────────────────────────────────────
# Earlier attempts (Form + onchange="this.form.requestSubmit()", and twin
# button forms) all had the same observed failure: the wrong locale was
# POSTed (browser timing / user confusion). This version reads the picked
# value at click time in JS and submits a freshly-built form — no chance
# for cached form state or event-timing quirks to leak the wrong value.

def _lang_switcher_script():
    # Locale is in the URL path, NOT a form field — Chrome's autofill kept
    # rewriting `name="locale"` form values to the browser's language.
    return Script("""
window.__switchLocale = function(value, nextUrl) {
  if (value !== 'en-US' && value !== 'zh-CN') return;
  var next = nextUrl || (window.location.pathname + window.location.search);
  var f = document.createElement('form');
  f.method = 'POST';
  f.action = '/locale/' + value + '?next=' + encodeURIComponent(next);
  f.style.display = 'none';
  document.body.appendChild(f);
  f.submit();
};
""")


def _lang_dropdown(cur_locale: str, next_url: str = "/", small: bool = True):
    """Native <details>/<summary> dropdown. <select> elements were
    unusable here — Chrome 148 desktop kept submitting the previously-
    selected option's value regardless of what the user picked. Buttons
    with hardcoded values bypass that entirely."""
    cur_label = "English" if cur_locale == "en-US" else "简体中文"
    summary_size = "text-xs px-2 py-1" if small else "text-sm px-3 py-2"
    btn_size = "text-xs px-3 py-1.5" if small else "text-sm px-3 py-2"
    summary_cls = (f"{summary_size} bg-gray-800 text-gray-100 rounded "
                   "border border-gray-700 hover:bg-gray-700 cursor-pointer "
                   "list-none select-none inline-block")
    item_cls = (f"{btn_size} block w-full text-left text-gray-100 "
                "hover:bg-gray-700 rounded")
    nu = (next_url or "/").replace("'", "%27")
    return Details(
        Summary(f"🌐 {cur_label} ▾", cls=summary_cls),
        Div(
            Button("English",
                   type="button",
                   onclick=f"window.__switchLocale('en-US', '{nu}')",
                   cls=item_cls),
            Button("简体中文",
                   type="button",
                   onclick=f"window.__switchLocale('zh-CN', '{nu}')",
                   cls=item_cls),
            cls=("absolute mt-1 right-0 bg-gray-800 border border-gray-700 "
                 "rounded shadow-lg p-1 min-w-[8rem] z-50"),
        ),
        cls="relative inline-block",
    )


def LoginPanel(error: str = ""):
    cur_locale = i18n.current_locale()
    return Div(
        _lang_switcher_script(),
        Div(
            Div(
                Span(i18n.t("auth.lang.label"),
                     cls="text-xs text-gray-500 mr-2 align-middle"),
                _lang_dropdown(cur_locale, next_url="/"),
                cls="flex items-center justify-end mb-6",
            ),
            H1(i18n.t("login.title"), cls="text-2xl font-semibold text-gray-100 mb-2"),
            P(i18n.t("login.subtitle"),
              cls="text-sm text-gray-400 mb-6"),
            (Div(error, cls="text-sm text-red-400 mb-4 p-3 bg-red-900/20 rounded") if error else None),
            Form(
                Label(i18n.t("login.email_label"), cls="text-xs text-gray-400"),
                Input(type="email", name="email", required=True,
                      cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 mb-3 border border-gray-700"),
                Label(i18n.t("login.password_label"), cls="text-xs text-gray-400"),
                Input(type="password", name="password", required=True,
                      cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 mb-4 border border-gray-700"),
                Button(i18n.t("login.submit"), type="submit",
                       cls="w-full bg-blue-600 hover:bg-blue-500 text-white py-2.5 rounded font-medium"),
                hx_post="/login",
                hx_target="#login-status",
                hx_swap="innerHTML",
            ),
            Div(id="login-status", cls="mt-4"),
            cls="max-w-sm mx-auto pt-20 px-6",
        ),
    )


@rt_route("/locale/{lang}", methods=["POST"])
def post_locale_path(lang: str, next: str = "/"):
    """Set the user's language. The lang code is in the URL PATH so no form
    body is needed — earlier versions used a form field, but Chrome's autofill
    engine rewrote the value to the user's browser language. With the value
    in the path, the browser can't tamper with it."""
    if lang not in db.LOCALES_SUPPORTED:
        return Response("invalid locale", status_code=400)
    with db.connect() as c:
        db.locale_set(c, lang)
    target = next if next.startswith("/") else "/"
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie("locale", lang, max_age=86400 * 365, samesite="lax")
    return resp


def EmptyDataPanel():
    return Div(
        Div(
            H1(i18n.t("empty.title"), cls="text-2xl font-semibold text-gray-100 mb-3"),
            P(i18n.t("empty.subtitle"),
              cls="text-sm text-gray-400 mb-6"),
            Button(i18n.t("empty.full_sync"),
                   hx_post="/sync?mode=full",
                   hx_target="#sync-progress",
                   hx_swap="innerHTML",
                   cls="bg-blue-600 hover:bg-blue-500 text-white px-6 py-2.5 rounded font-medium"),
            Div(id="sync-progress", cls="mt-4 text-sm text-gray-400"),
            cls="max-w-md mx-auto pt-20 px-6 text-center",
        ),
    )


@rt_route("/login")
def post_login(email: str, password: str):
    garmin_auth.start_auth(email, password,
                           os.path.join(_DATA_DIR, ".garth_session"))
    return Div(
        Div(i18n.t("login.connecting"), cls="text-sm text-gray-300 mb-2"),
        Div(id="login-poll",
            hx_get="/login/status",
            hx_trigger="load delay:1s",
            hx_swap="outerHTML"),
    )


@rt_route("/login/status")
def login_status():
    s = garmin_auth.get_status()
    st = s.get("status")
    if st == "running":
        return Div(
            Div(i18n.t("login.opening"), cls="text-sm text-gray-400"),
            id="login-poll",
            hx_get="/login/status",
            hx_trigger="load delay:1.5s",
            hx_swap="outerHTML",
        )
    if st == "mfa_needed":
        # If user already submitted, status stays "mfa_needed" for several
        # seconds while Playwright fills the code + waits for CAS ticket.
        # Show "verifying" and keep polling — re-rendering the form here
        # would clear the user's input and make them think the submit
        # silently failed (the original bug: had to submit twice to log in).
        if s.get("mfa_code"):
            return Div(
                Div(i18n.t("login.mfa_submitted"), cls="text-sm text-gray-400"),
                id="login-poll",
                hx_get="/login/status",
                hx_trigger="load delay:1.5s",
                hx_swap="outerHTML",
            )
        return Div(
            P(i18n.t("login.mfa_prompt"), cls="text-sm text-gray-300 mb-2"),
            Form(
                Input(type="text", name="code", placeholder=i18n.t("login.mfa_ph"),
                      autocomplete="one-time-code",
                      cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 mb-2 border border-gray-700"),
                Button(i18n.t("login.mfa_submit"), type="submit",
                       cls="w-full bg-blue-600 hover:bg-blue-500 text-white py-2 rounded text-sm"),
                hx_post="/login/mfa",
                hx_target="#login-poll",
                hx_swap="outerHTML",
            ),
            id="login-poll",
        )
    if st == "success":
        return Div(NotStr("<script>window.location='/'</script>"),
                   i18n.t("login.success"), cls="text-sm text-green-400")
    if st == "error":
        garmin_auth.reset()
        return Div(
            Div(s.get("error", i18n.t("login.failed")), cls="text-sm text-red-400 mb-3"),
            A(i18n.t("login.retry"), href="/", cls="text-sm text-blue-400"),
        )
    return Div(i18n.t("login.idle"), cls="text-xs text-gray-500")


@rt_route("/login/mfa")
def login_mfa(code: str):
    garmin_auth.submit_mfa(code)
    return Div(
        Div(i18n.t("login.mfa_submitted"), cls="text-sm text-gray-400"),
        id="login-poll",
        hx_get="/login/status",
        hx_trigger="load delay:1.5s",
        hx_swap="outerHTML",
    )


@rt_route("/logout", methods=["POST"])
def post_logout():
    """Full disconnect — matches the Danger Zone dialog's explicit promise:
    wipes activity data + AI chat history + training-context cache + garth
    session, but PRESERVES user-typed state (races / personal_note / coach
    insights / activity tags / comments / app config).

    Why selective: all data lives in one SQLite file (cache/garmin.db).
    A naive `rmtree(cache/)` would also wipe user_races / user_coach_insights
    / etc. that the dialog promises to keep. So we DELETE FROM specific
    tables instead.

    POST-only — same lock-down as /auth/reconnect (GET prefetch can't
    silently nuke data)."""
    # 1) Wipe garth session (forces fresh OAuth on next launch)
    sd = os.path.join(_DATA_DIR, ".garth_session")
    if os.path.exists(sd):
        shutil.rmtree(sd, ignore_errors=True)
    garmin_auth.reset()

    # 2) Wipe activity / wellness / chat tables — keep user_* tables
    _WIPE_TABLES = (
        # Activity tier (regenerable from Garmin)
        "activity_metrics", "activity_laps", "activity_splits",
        "activity_hr_zones", "activity_gps", "activity_weather",
        "activity_power_zones", "activity_unknown_channels",
        "activity_review_context",
        "activities",
        # Chat history (per dialog: explicitly NOT preserved)
        "chat_overall", "chat_review", "chat_review_meta",
        # AI-generated daily plan (currently unused — kept for forward-compat)
        "user_coaching_report",
    )
    try:
        with db.connect() as conn:
            for t in _WIPE_TABLES:
                try:
                    conn.execute(f"DELETE FROM {t}")
                except Exception:
                    pass  # Table may not exist on older schemas; tolerate
    except Exception as e:
        print(f"[disconnect] wipe failed: {e}")

    # 3) Bust app-metadata sync timestamp so the next launch shows
    # "尚无活动数据" cleanly instead of "上次同步：1 分钟前" with no acts.
    try:
        with db.connect() as conn:
            db.set_app_metadata(conn, fetched_at="", display_name="")
    except Exception:
        pass

    return RedirectResponse("/", status_code=303)


@rt_route("/auth/reconnect", methods=["POST"])
def post_auth_reconnect():
    """POST-only — refresh-only Garmin auth. Wipes ONLY .garth_session/
    to force a fresh OAuth flow. Preserves SQLite, chats, insights,
    everything. POST-only so a HEAD probe / browser prefetch / accidental
    URL hit can't silently wipe the session (FastHTML's rt_route accepts
    GET by default; we override with explicit methods=)."""
    sd = os.path.join(_DATA_DIR, ".garth_session")
    if os.path.exists(sd):
        shutil.rmtree(sd, ignore_errors=True)
    garmin_auth.reset()
    return RedirectResponse("/", status_code=303)


def _stale_session_panel(err: str, active_route: str, req=None):
    """Common error panel returned when SessionExpiredError /
    EmptyDetailFetchError surface in a route. Auto-opens the global
    reconnect-dlg via inline script so the user sees the recovery prompt
    immediately, not just a red error line."""
    return _respond(
        Div(
            P(i18n.t("stale_session.error", err=err),
              cls="p-6 text-red-400 text-sm max-w-3xl mx-auto"),
            NotStr(
                "<script>"
                "if (document.getElementById('reconnect-dlg')) "
                "document.getElementById('reconnect-dlg').showModal();"
                "</script>"
            ),
        ),
        active_route=active_route,
        req=req,
    )


# ── Sync (background thread + polling) ─────────────────────────────────────────
def _run_sync(force_full: bool):
    def _cb(frac, msg):
        with _sync_lock:
            _sync_state["frac"] = frac
            _sync_state["msg"] = msg or ""

    with _sync_lock:
        if _sync_state["running"]:
            return
        _sync_state.update({"running": True, "frac": 0.0, "msg": i18n.t("ui.sync.starting"), "error": ""})

    # Propagate request-thread locale into the worker — see _kickoff_report_job.
    request_locale = i18n.current_locale()

    def _worker():
        i18n.set_request_locale(request_locale)
        try:
            gd.sync_all(force_full=force_full, progress=_cb)
            with _sync_lock:
                _sync_state.update({"running": False, "frac": 1.0, "msg": i18n.t("sync.done_msg")})
        except gd.SessionExpiredError as e:
            with _sync_lock:
                _sync_state.update({"running": False,
                                    "error": i18n.t("sync.session_expired_pfx", e=e),
                                    "session_expired": True})
        except Exception as e:
            with _sync_lock:
                _sync_state.update({"running": False, "error": str(e),
                                    "session_expired": False})

    threading.Thread(target=_worker, daemon=True).start()


@rt_route("/sync")
def post_sync(mode: str = "incremental"):
    _run_sync(force_full=(mode == "full"))
    return Div(
        Div(i18n.t("sync.in_progress"), cls="text-yellow-400"),
        id="sync-poll",
        hx_get="/sync/status",
        hx_trigger="load delay:1.5s",
        hx_swap="outerHTML",
    )


@rt_route("/sync/status")
def sync_status():
    with _sync_lock:
        s = dict(_sync_state)
    if s["running"]:
        pct = int(s["frac"] * 100)
        return Div(
            Div(i18n.t("sync.in_progress_pct", msg=s['msg'], pct=pct),
                cls="text-yellow-400"),
            id="sync-poll",
            hx_get="/sync/status",
            hx_trigger="load delay:1.5s",
            hx_swap="outerHTML",
        )
    if s.get("error"):
        # If garth token expired, surface the global reconnect dialog so
        # user has a 1-click recovery path (vs just seeing the red error).
        extras = []
        if s.get("session_expired"):
            extras.append(NotStr(
                "<script>"
                "if (document.getElementById('reconnect-dlg')) "
                "document.getElementById('reconnect-dlg').showModal();"
                "</script>"
            ))
        return Div(i18n.t("sync.error_pfx", msg=s['error']), *extras,
                   cls="text-red-400", id="sync-poll")
    # done — refresh sidebar
    return Div(
        i18n.t("sync.done_toast"),
        Script("setTimeout(()=>location.reload(), 800)"),
        cls="text-green-400",
        id="sync-poll",
    )


# ── Chat: overall ──────────────────────────────────────────────────────────────
def _overall_chat_header():
    return Div(
        Div(
            H2(i18n.t("chat.overall.title"), cls="text-lg font-semibold text-gray-100"),
            P(i18n.t("chat.overall.subtitle"),
              cls="text-xs text-gray-500 mt-0.5"),
            cls="max-w-3xl mx-auto",
        ),
        cls="border-b border-gray-800 px-4 py-3",
    )


@rt_route("/chat/overall")
def get_overall(req=None):
    if not _have_data():
        return _respond(EmptyDataPanel(), active_route="/", req=req)
    with db.connect() as conn:
        chat = db.overall_chat_load(conn)
    return _respond(
        ChatPanel(
            kind="overall",
            activity_id=None,
            header=_overall_chat_header(),
            messages=chat.get("messages", []),
            model=DEFAULT_MODEL,
        ),
        active_route="/chat/overall",
        req=req,
    )


@rt_route("/chat/overall/send")
def post_overall_send(msg: str):
    if not msg or not msg.strip():
        return Div()
    msg = msg.strip()
    with db.connect() as conn:
        db.overall_chat_append(conn, "user", msg)
    token = uuid.uuid4().hex[:12]
    _pending_streams[token] = {"kind": "overall", "aid": None}
    return Div(
        MsgBubble("user", msg),
        StreamingBubble(token),
    )


@rt_route("/chat/overall/clear")
def post_overall_clear(req=None):
    with db.connect() as conn:
        db.overall_chat_clear(conn)
    return get_overall(req=req)


@rt_route("/chat/overall/starter-chips")
def get_overall_starter_chips():
    """Lazy-loaded inspiration chips for empty /chat/overall state. Returns 3
    button elements that swap into #follow-up-chips innerHTML. LLM-generated,
    cached for 3h OR until new activity synced (whichever first)."""
    with db.connect() as conn:
        chips = _get_or_generate_starter_chips(conn)
    return tuple(
        Button(
            c,
            type="button",
            onclick="useChip(this.dataset.msg)",
            cls="text-xs bg-gray-800 hover:bg-gray-700 text-gray-200 "
                "px-3 py-1.5 rounded-full border border-gray-700 whitespace-nowrap",
            **{"data-msg": c},
        )
        for c in chips
    )


# ── Chat: per-activity ─────────────────────────────────────────────────────────
def _report_card(aid: int, *, report_text: str = "", status: str = "running",
                 builder_name: str = "", stale: bool = False,
                 generated_at: str = ""):
    """Row 3 — the dedicated review-report card.

    States (driven by the data attrs the JS reads):
      * status="running"         → blue dot, chips animation, SSE auto-subscribe
      * status="done", stale=False → green dot
      * status="done", stale=True  → orange "stale" banner + [重新生成] button
      * status="empty"           → "尚未生成" placeholder (rare — pre-tag path
                                     normally goes through the nudge panel)

    Default collapsed; user toggles via the header arrow. State persisted to
    localStorage `report-collapse-<aid>` so re-visits stay collapsed."""
    pill_base = ("status-pill text-xs px-2 py-0.5 rounded-full whitespace-nowrap "
                 "shrink-0")
    if status == "running":
        pill = Span(i18n.t("report.pill.running"),
                    cls=f"{pill_base} text-blue-300 bg-blue-900/40")
        title_extra = ""
    elif status == "done" and stale:
        pill = Span(i18n.t("report.pill.stale"),
                    cls=f"{pill_base} text-orange-300 bg-orange-900/40")
        title_extra = ""
    elif status == "done":
        pill = Span(i18n.t("report.pill.done"),
                    cls=f"{pill_base} text-emerald-300 bg-emerald-900/40")
        # Subtitle goes on its own line so the header doesn't wrap awkwardly
        # on narrow phones.
        bits = []
        if builder_name: bits.append(builder_name)
        if generated_at: bits.append(generated_at)
        title_extra = " · ".join(bits)
    else:
        pill = Span(i18n.t("report.pill.empty"),
                    cls=f"{pill_base} text-gray-400 bg-gray-800")
        title_extra = ""

    # Header row: title (+ optional subtitle stacked under it) takes the
    # left flex-1 column; pill + arrow stay on the right and never wrap.
    title_block_children = [
        Div(i18n.t("report.card.title"),
            cls="text-sm font-medium text-gray-100 whitespace-nowrap"),
    ]
    if title_extra:
        title_block_children.append(
            Div(title_extra, cls="text-[11px] text-gray-500 truncate mt-0.5")
        )
    header_row = Div(
        Div(*title_block_children, cls="flex-1 min-w-0"),
        pill,
        Span("▶", cls="collapse-arrow text-gray-400 text-xs ml-2 shrink-0 "
                      "transition-transform"),
        cls="report-header flex items-center gap-2 px-4 py-2.5 cursor-pointer "
            "hover:bg-gray-800/40 select-none",
        onclick="toggleReportCard(this)",
    )

    stale_banner = (
        Div(
            Span(i18n.t("report.stale.banner"),
                 cls="text-xs text-orange-200"),
            Button(i18n.t("report.stale.regenerate"),
                   type="button",
                   onclick=f"regenerateReport({aid})",
                   cls="text-xs bg-orange-600 hover:bg-orange-500 text-white "
                       "px-2.5 py-1 rounded ml-2"),
            cls="report-stale-banner flex items-center "
                "border-t border-orange-900/40 bg-orange-950/30 px-4 py-2",
        ) if stale else None
    )

    body_children = [
        Div(cls="report-sections px-4 py-3 prose-coach text-gray-100"),
    ]
    if status == "running":
        body_children.append(
            Div(
                Span(i18n.t("report.chips_text"),
                     cls="report-chips-text text-xs text-gray-400"),
                cls="report-chips px-4 py-2 text-gray-500",
            )
        )
    body = Div(
        *([stale_banner] if stale_banner else []),
        *body_children,
        cls="report-body border-t border-gray-800 hidden",
    )

    return Div(
        header_row,
        body,
        id="report-card",
        **{
            "data-aid": str(aid),
            "data-status": status,
            "data-stale": "1" if stale else "0",
            "data-report-text": report_text or "",
        },
        cls="report-card border border-gray-800 rounded-lg bg-gray-900/40 "
            "max-w-3xl mx-auto mt-3",
    )


def _activity_chat_header(act: dict, tag: str, comment: str, *, stats_open: bool = False):
    aid = act.get("activityId")
    primary, secondary = _activity_label(act)
    pace = (gd.format_pace(act.get("averageSpeed"))
            if act.get("averageSpeed") else "—")
    hr = act.get("averageHR")
    eg = act.get("elevationGain")

    chips = [
        Span(f"@{pace}", cls="text-xs bg-gray-800 px-2 py-0.5 rounded"),
        Span(f"HR {int(hr)}" if hr else "HR —", cls="text-xs bg-gray-800 px-2 py-0.5 rounded"),
    ]
    if eg and eg > 5:
        chips.append(Span(f"+{int(eg)}m", cls="text-xs bg-gray-800 px-2 py-0.5 rounded"))

    tag_options = [Option(_tag_label(k), value=k, selected=(k == tag))
                   for k in ACTIVITY_TAG_KEYS]

    return Div(
        Div(
            Div(
                H2(primary, cls="text-lg font-semibold text-gray-100"),
                Div(secondary, cls="text-xs text-gray-500 mt-0.5"),
                Div(*chips, cls="flex gap-1.5 mt-2 flex-wrap"),
                cls="flex-1",
            ),
            Div(
                Form(
                    Select(*tag_options, name="tag",
                           cls="bg-gray-800 text-gray-100 text-xs rounded px-2 py-1 border border-gray-700"),
                    hx_post=f"/chat/activity/{aid}/tag",
                    hx_target="#main",
                    hx_swap="innerHTML",
                    hx_trigger="change",
                ),
                cls="ml-2",
            ),
            cls="flex items-start max-w-3xl mx-auto",
        ),
        # Comment editor (collapsible)
        Details(
            Summary(i18n.t("activity.comment.summary"),
                    cls="text-xs text-gray-400 cursor-pointer max-w-3xl mx-auto py-1"),
            Form(
                Textarea(comment or "", name="comment", rows="3",
                         placeholder=i18n.t("activity.comment.placeholder"),
                         cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 mt-1 border border-gray-700 text-sm"),
                Button(i18n.t("activity.comment.save"), type="submit",
                       cls="text-xs bg-gray-700 hover:bg-gray-600 text-gray-100 px-3 py-1 rounded mt-1"),
                hx_post=f"/chat/activity/{aid}/comment",
                hx_target="#comment-status",
                hx_swap="innerHTML",
                cls="max-w-3xl mx-auto",
            ),
            Div(id="comment-status", cls="text-xs text-gray-500 max-w-3xl mx-auto mt-1"),
            cls="mt-2",
        ),
        # Activity stats + charts (collapsible — same UX as 课表/备注 above)
        _activity_stats_panel(aid, open_default=stats_open),
        cls="border-b border-gray-800 px-4 py-3",
    )


# ── Background report-generation orchestration ─────────────────────────────
# The full pipeline (fetch detail → typed-builder context → LLM stream) runs
# in a worker thread via report_jobs.start(). The worker is independent of
# any HTTP connection — clients subscribe to the job's section-event stream
# via /chat/activity/{aid}/report/stream and reconnect transparently if they
# navigate away and come back.

def _build_report_messages(aid: int) -> tuple[list[dict], str]:
    """Assemble the LLM messages for an activity's typed report. Returns
    (messages, builder_name). Used by both the background worker and (in
    principle) any future replay/inspect path."""
    with db.connect() as conn:
        tag = db.tag_get(conn, aid) or ""
        row = conn.execute(
            "SELECT activity_type_key FROM activities WHERE activity_id=?",
            (aid,)
        ).fetchone()
        type_key = row[0] if row else ""
        comment = db.comment_get(conn, aid) or ""

    builder = _dispatch_builder(tag, type_key)
    ctx_md = _get_or_build_review_ctx(aid, tag, type_key)

    cfg = uc.load()
    detailed = gd.load_detailed() or {}
    sel_act = next(
        (a for a in detailed.get("activities", []) if a.get("activityId") == aid),
        None,
    )
    date_background = _build_date_background(sel_act, detailed) if sel_act else ""
    tag_instruction = (
        i18n.t("prompt.tag_instruction.tagged", tag=_tag_label(tag))
        if tag else
        i18n.t("prompt.tag_instruction.untagged")
    )
    comment_instruction = (
        i18n.t("prompt.comment_instruction.has_comment")
        if comment else i18n.t("prompt.comment_instruction.no_comment")
    )
    prompt_name = uc.ACTIVITY_TAG_TO_PROMPT.get(tag, "review_report")
    user_prompt = load_prompt(prompt_name).format(
        activity_context=ctx_md,
        date_background=date_background,
        tag_instruction=tag_instruction,
        comment_instruction=comment_instruction,
    ) + _follow_ups_instruction()
    messages = [
        {"role": "system", "content": coach_sys(cfg)},
        {"role": "user",   "content": user_prompt},
    ]
    return messages, builder.name


def _save_report_to_db(aid: int, full_text: str) -> None:
    """Replace msg #0 in chat_review with the new report (preserves any
    follow-up Q&A that was below it). Idempotent — safe to call multiple
    times for the same generation."""
    if not full_text:
        return
    with db.connect() as conn:
        state = db.review_chat_load(conn, aid)
        msgs = list(state.get("messages", []))
        new_msg = {
            "role": "assistant",
            "content": full_text,
            "model": DEFAULT_MODEL,
            "ts": None,
        }
        if msgs:
            msgs[0] = new_msg
        else:
            msgs = [new_msg]
        db.review_chat_replace(
            conn, aid, msgs,
            state.get("summary", ""),
            state.get("summary_through_idx", 0),
        )


def _kickoff_report_job(aid: int) -> None:
    """Start a background report-generation job for `aid` if one isn't
    already running. Idempotent."""
    existing = report_jobs.get(aid)
    if existing and existing.status == "running":
        return

    # Capture locale in the request thread — ContextVars don't propagate
    # into threading.Thread, so the worker would otherwise fall back to
    # env_default_locale() and ignore the user's setting.
    request_locale = i18n.current_locale()

    def _worker(job):
        i18n.set_request_locale(request_locale)
        messages, _builder = _build_report_messages(aid)
        for chunk in llm_stream(messages, DEFAULT_MODEL):
            job.feed_chunk(chunk)

    with db.connect() as conn:
        tag = db.tag_get(conn, aid) or ""
        row = conn.execute(
            "SELECT activity_type_key FROM activities WHERE activity_id=?",
            (aid,)
        ).fetchone()
        type_key = row[0] if row else ""
    builder_name = _dispatch_builder(tag, type_key).name

    report_jobs.start(
        aid, builder_name,
        worker=_worker,
        on_complete=lambda full: _save_report_to_db(aid, full),
    )


def _is_report_stale(aid: int) -> bool:
    """True iff there's a report in chat_review msg #0 whose builder doesn't
    match the current tag's builder (i.e. user re-tagged since gen). The
    activity_review_context row is dropped on tag change, so a missing/stale
    row alongside a present msg #0 is the "stale" signature."""
    with db.connect() as conn:
        tag = db.tag_get(conn, aid) or ""
        row = conn.execute(
            "SELECT activity_type_key FROM activities WHERE activity_id=?",
            (aid,)
        ).fetchone()
        type_key = row[0] if row else ""
        cached = db.load_review_context(conn, aid)
    if not cached or not cached.get("builder_version_hash"):
        return True
    builder = _dispatch_builder(tag, type_key)
    if cached.get("builder_version_hash") != builder.builder_hash():
        return True
    if cached.get("tag_at_generation") != tag:
        return True
    return False


def _get_or_build_review_ctx(aid: int, tag: str, type_key: str) -> str:
    """Resolve the typed-builder context_md for an activity, using the cache
    when valid. The gate is on builder_hash + tag_at_generation, with an
    empty-context skip-save guard.

    Without this cache, EVERY chat turn re-runs builder.build() (≈500ms-2s)
    to recompute Pa:HR drift / per-lap stats / decoupling / etc., adding
    cumulative seconds of dead latency to long conversations. The
    activity_review_context table is the single source of truth; this
    function reads / writes it. Invalidation is already wired:
    post_activity_tag calls db.clear_review_context on tag change."""
    builder = _dispatch_builder(tag, type_key)
    with db.connect() as conn:
        cached = db.load_review_context(conn, aid)
        # Skip cache if empty (would otherwise become stale-cache poison for
        # the next render).
        if (cached and cached.get("context_md")
                and cached.get("builder_version_hash") == builder.builder_hash()
                and cached.get("tag_at_generation") == tag):
            return cached["context_md"]
        result = builder.build(aid, conn)
        if result.context_md:
            db.save_review_context(
                conn, aid,
                tag_at_generation    = tag,
                builder_name         = builder.name,
                builder_version_hash = result.builder_hash,
                context_md           = result.context_md,
                highlight_windows    = result.highlight_windows,
            )
        return result.context_md


def _ensure_activity_detail_and_report(activity_id: int) -> tuple[dict, str, str]:
    """Lazy-load: ensure detail is in SQLite, ensure review_chat msg 0 exists.
    Returns (act_dict, tag, comment)."""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT activity_id, activity_name, activity_type_key, start_time_local, "
            "  distance_m, duration_s, average_hr, max_hr, average_speed_mps, "
            "  elevation_gain_m, training_load, aerobic_te, te_label "
            "FROM activities WHERE activity_id = ?", (activity_id,)
        ).fetchone()
        if not row:
            return None, "", ""
        act = {
            "activityId": row[0], "activityName": row[1] or "",
            "activityTypeKey": row[2] or "", "startTimeLocal": row[3] or "",
            "distance": row[4], "duration": row[5],
            "averageHR": row[6], "maxHR": row[7], "averageSpeed": row[8],
            "elevationGain": row[9], "activityTrainingLoad": row[10],
            "aerobicTrainingEffect": row[11], "trainingEffectLabel": row[12],
        }
        tag = db.tag_get(conn, activity_id) or ""
        comment = db.comment_get(conn, activity_id) or ""
    return act, tag, comment


@rt_route("/chat/activity/{aid}")
def get_activity_chat(aid: int, req=None):
    if not _have_data():
        return _respond(EmptyDataPanel(), active_route="/", req=req)
    act, tag, comment = _ensure_activity_detail_and_report(aid)
    if act is None:
        return _respond(Div(i18n.t("ui.activity_not_found"), cls="p-8 text-red-400"),
                        active_route="/", req=req)

    # Lazy detail fetch — user can't reasonably tag / comment / recall what
    # this run was without seeing map + charts + laps. Sync fetch on first
    # click; subsequent clicks hit cache. Wait time (5-30s) is shown via the
    # .htmx-request opacity fade on the sidebar item that triggered the load.
    with db.connect() as _conn:
        needs_fetch = not db.has_full_detail(_conn, aid)
    if needs_fetch:
        try:
            gd.fetch_activity_detail(aid)
        except gd.SessionExpiredError as e:
            return _stale_session_panel(str(e),
                                         active_route=f"/chat/activity/{aid}",
                                         req=req)
        except gd.EmptyDetailFetchError as e:
            # Empty detail = garth returned None / no metric descriptors,
            # which is also a stale-session signature in practice.
            return _stale_session_panel(str(e),
                                         active_route=f"/chat/activity/{aid}",
                                         req=req)
        except Exception as e:
            return _respond(
                Div(i18n.t("sse.fetch_detail_failed", e=e),
                    cls="p-6 text-red-400 text-sm max-w-3xl mx-auto"),
                active_route=f"/chat/activity/{aid}", req=req,
            )

    with db.connect() as conn:
        chat = db.review_chat_load(conn, aid)

    messages = chat.get("messages", [])
    has_report_in_db = bool(messages
                            and messages[0].get("role") == "assistant"
                            and messages[0].get("content"))
    job = report_jobs.get(aid)

    # No report ever AND no tag → nudge panel (must pick a tag first).
    if not has_report_in_db and job is None and not tag:
        return _pre_report_nudge_panel(aid, act, tag, comment, req)

    # Has tag but no report ever AND no in-flight job → kick off background
    # generation. Job persists across the request boundary; client SSE will
    # subscribe on page load.
    if not has_report_in_db and job is None and tag:
        _kickoff_report_job(aid)
        job = report_jobs.get(aid)

    # ── Build the Row 3 report card from the strongest signal we have ────
    if job is not None and job.status == "running":
        report_card = _report_card(aid, status="running",
                                   builder_name=job.builder_name)
    elif has_report_in_db:
        stale = _is_report_stale(aid)
        ts = (messages[0].get("ts") or "")[:16].replace("T", " ")
        with db.connect() as conn:
            cached = db.load_review_context(conn, aid)
        builder_name = (cached or {}).get("builder_name") or ""
        report_card = _report_card(
            aid, status="done",
            report_text=messages[0].get("content", ""),
            builder_name=builder_name,
            stale=stale,
            generated_at=ts,
        )
    elif job is not None and job.status == "error":
        report_card = _report_card(
            aid, status="empty", builder_name=job.builder_name,
        )
    else:
        report_card = _report_card(aid, status="empty")

    chat_messages = messages[1:] if has_report_in_db else messages

    return _respond(
        ActivityChatPanel(
            aid=aid,
            header=_activity_chat_header(act, tag, comment),
            report_card=report_card,
            messages=chat_messages,
            model=DEFAULT_MODEL,
        ),
        active_route=f"/chat/activity/{aid}",
        req=req,
    )


def _pre_report_nudge_panel(aid: int, act: dict, tag: str, comment: str, req=None):
    """Hard-gate input form shown when user opens an unreported activity
    without a tag. The form's submit button stays DISABLED until the tag
    dropdown has a non-empty value (small JS onchange handler enables it).
    Comment textarea is optional. POSTs to /chat/activity/{aid}/start-report
    which persists tag + comment then dispatches the typed builder + LLM.

    Why hard gate: without a tag, dispatch falls through to DefaultBuilder +
    generic prompt, which produces a noticeably weaker report and wastes a
    full LLM call. Better to nag once than waste tokens repeatedly."""
    # Build dropdown options. First option is the empty "select me" placeholder
    # which is `disabled+selected` so it never submits with an empty value.
    tag_options = [
        Option(i18n.t("nudge.tag.placeholder"), value="", disabled=True, selected=(not tag))
    ] + [
        Option(_tag_label(k), value=k, selected=(k == tag))
        for k in ACTIVITY_TAG_KEYS if k
    ]

    nudge_form = Form(
        Div("👋", cls="text-5xl text-center mb-3"),
        H2(i18n.t("nudge.title"),
           cls="text-lg font-semibold text-gray-100 text-center mb-1"),
        P(i18n.t("nudge.subtitle"),
          cls="text-xs text-gray-500 text-center mb-6"),

        # Tag — REQUIRED (gates the submit button)
        Div(
            Span("①", cls="text-emerald-400 text-base font-bold mr-2"),
            Span(i18n.t("nudge.tag.required"), cls="text-gray-100 text-sm font-medium"),
            Span(i18n.t("nudge.tag.required_hint"), cls="text-rose-400 text-xs ml-1"),
            cls="flex items-baseline mb-1.5",
        ),
        Select(
            *tag_options,
            name="tag",
            required=True,
            # Enable the submit button as soon as a non-empty tag is chosen.
            onchange="document.getElementById('start-report-btn').disabled = !this.value;",
            cls="w-full bg-gray-800 text-gray-100 rounded-lg px-3 py-2.5 "
                "border border-gray-700 focus:border-emerald-500 focus:outline-none "
                "text-sm mb-1",
        ),
        P(i18n.t("nudge.tag.help"),
          cls="text-xs text-gray-500 mb-5"),

        # Comment — OPTIONAL
        Div(
            Span("②", cls="text-sky-400 text-base font-bold mr-2"),
            Span(i18n.t("nudge.comment.label"), cls="text-gray-100 text-sm font-medium"),
            Span(i18n.t("nudge.comment.optional"), cls="text-gray-500 text-xs ml-1"),
            cls="flex items-baseline mb-1.5",
        ),
        Textarea(
            comment or "",
            name="comment",
            rows="3",
            placeholder=i18n.t("nudge.comment.placeholder"),
            cls="w-full bg-gray-800 text-gray-100 rounded-lg px-3 py-2 "
                "border border-gray-700 focus:border-sky-500 focus:outline-none "
                "text-sm mb-1 resize-none leading-relaxed",
        ),
        P(i18n.t("nudge.comment.help"),
          cls="text-xs text-gray-500 mb-6"),

        Button(
            i18n.t("nudge.cta"),
            id="start-report-btn",
            type="submit",
            disabled=(not tag),  # if user already had a tag (edge), enable
            cls="w-full bg-blue-600 hover:bg-blue-500 disabled:bg-gray-700 "
                "disabled:text-gray-500 disabled:cursor-not-allowed "
                "text-white px-4 py-2.5 rounded-lg text-sm font-medium "
                "shadow-lg transition-colors",
        ),
        hx_post=f"/chat/activity/{aid}/start-report",
        hx_target="#main",
        hx_swap="innerHTML",
        cls="max-w-sm mx-auto pt-4 px-4",
    )
    nudge = Div(nudge_form)
    panel = Div(
        # stats_open=True so the carousel is expanded by default — user just
        # waited for the fetch, surface the data so they can recall what
        # they ran while filling in the type/comment.
        _activity_chat_header(act, tag, comment, stats_open=True),
        Div(nudge, id="chat-scroll",
            cls="px-4 py-4 max-w-3xl mx-auto w-full "
                "pb-[calc(8rem+env(safe-area-inset-bottom))]"),
        # Disabled input bar — pre-report state, no chat to send to yet
        Div(
            Div(id="follow-up-chips",
                cls="max-w-3xl mx-auto flex gap-2 overflow-x-auto no-scrollbar "
                    "pb-2 empty:pb-0 empty:hidden"),
            Form(
                Textarea("", name="msg", rows="1",
                         placeholder=i18n.t("chat.activity.placeholder_locked"),
                         disabled=True,
                         cls="flex-1 bg-gray-900 text-gray-500 rounded-xl "
                             "px-4 py-2.5 resize-none border border-gray-800 text-sm"),
                cls="flex items-end max-w-3xl mx-auto w-full",
            ),
            cls="fixed bottom-0 left-0 right-0 md:left-72 z-10 "
                "border-t border-gray-800 bg-gray-950 "
                "px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))]",
        ),
    )
    return _respond(panel, active_route=f"/chat/activity/{aid}", req=req)


def _seed_report_panel(aid: int, req=None):
    """Build a fresh-report seed panel. ALL heavy lifting (fetch_activity_detail,
    builder.build, prompt assembly, LLM stream) happens inside the SSE handler
    so the panel renders INSTANTLY and the user sees progressive status events
    ("🛰️ 拉取活动 1Hz 数据…" → "🔬 构建复盘分析…" → "✍️ 教练撰写中…") as
    each phase progresses, instead of a blank cursor blinking for 30s.

    Used by both the auto-trigger path in get_activity_chat (no chat yet) AND
    the explicit /regenerate route. Both stash a lazy-seed token and rely on
    the SSE generator's `activity_seed_lazy` branch to do the work."""
    # No sync work — just register a lazy-seed token.
    token = uuid.uuid4().hex[:12]
    _pending_streams[token] = {
        "kind": "activity_seed_lazy",
        "aid": aid,
    }
    # The header needs the activity row from `activities` table (cheap SQLite
    # read, NOT the slow detail fetch). _ensure_activity_detail_and_report
    # just queries metadata — safe to call before full-detail fetch happens.
    act, tag, comment = _ensure_activity_detail_and_report(aid)
    panel = (
        Div(
            _activity_chat_header(act, tag, comment),
            Div(StreamingBubble(token),
                id="chat-scroll",
                cls="px-4 py-4 max-w-3xl mx-auto w-full "
                    "pb-[calc(8rem+env(safe-area-inset-bottom))]"),
            Div(
                Div(id="follow-up-chips",
                    cls="max-w-3xl mx-auto flex gap-2 overflow-x-auto no-scrollbar "
                        "pb-2 empty:pb-0 empty:hidden"),
                Form(
                    Textarea("", name="msg", rows="1",
                             placeholder=i18n.t("chat.activity.placeholder_generating"),
                             onkeydown="sendOnEnter(event)",
                             cls="flex-1 bg-gray-800 text-gray-100 rounded-xl px-4 py-2.5 resize-none border border-gray-700 text-sm"),
                    Button(i18n.t("chat.send"), type="submit",
                           cls="bg-blue-600 hover:bg-blue-500 text-white px-4 py-2 rounded-xl text-sm font-medium ml-2"),
                    hx_post=f"/chat/activity/{aid}/send",
                    hx_target="#chat-scroll",
                    hx_swap="beforeend",
                    **{"data-clear-on-send": "1"},
                    cls="flex items-end max-w-3xl mx-auto w-full",
                ),
                cls="fixed bottom-0 left-0 right-0 md:left-72 z-10 "
                    "border-t border-gray-800 bg-gray-950 "
                    "px-4 pt-3 pb-[calc(0.75rem+env(safe-area-inset-bottom))]",
            ),
        )
    )
    return _respond(panel, active_route=f"/chat/activity/{aid}", req=req)


@rt_route("/chat/activity/{aid}/regenerate")
def post_regenerate(aid: int, req=None):
    """Manual regenerate trigger (stale banner / explicit button). Replaces
    msg #0 in chat_review with a fresh report when the background job
    completes; follow-up Q&A (msg #1+) is preserved."""
    existing = report_jobs.get(aid)
    if existing and existing.status != "running":
        report_jobs.reset(aid)
    with db.connect() as conn:
        db.clear_review_context(conn, aid)
    _kickoff_report_job(aid)
    return get_activity_chat(aid, req)


@rt_route("/chat/activity/{aid}/start-report")
def post_start_report(aid: int, tag: str = "", comment: str = "", req=None):
    """Submitted from the nudge form (_pre_report_nudge_panel). Persists the
    tag (required) + comment (optional), then kicks off background generation.
    User lands on the chat page with Row 3 in the running state."""
    tag = (tag or "").strip()
    comment = (comment or "").strip()
    if not tag:
        return get_activity_chat(aid, req)
    with db.connect() as conn:
        db.tag_set(conn, aid, tag)
        db.clear_review_context(conn, aid)
        if comment:
            db.comment_set(conn, aid, comment)
    _kickoff_report_job(aid)
    return get_activity_chat(aid, req)


@rt_route("/chat/activity/{aid}/send")
def post_activity_send(aid: int, msg: str):
    if not msg or not msg.strip():
        return Div()
    msg = msg.strip()
    with db.connect() as conn:
        db.review_chat_append(conn, aid, "user", msg)
    token = uuid.uuid4().hex[:12]
    _pending_streams[token] = {"kind": "activity", "aid": aid}
    return Div(
        MsgBubble("user", msg),
        StreamingBubble(token),
    )


@rt_route("/chat/activity/{aid}/clear")
def post_activity_clear(aid: int, req=None):
    with db.connect() as conn:
        db.review_chat_clear(conn, aid)
    report_jobs.reset(aid)
    return get_activity_chat(aid, req=req)


@rt_route("/chat/activity/{aid}/tag")
def post_activity_tag(aid: int, tag: str = "", req=None):
    with db.connect() as conn:
        db.tag_set(conn, aid, tag.strip())
        # Tag change → invalidate cached review context
        db.clear_review_context(conn, aid)
    # Pass req so get_activity_chat returns the panel-only fragment for
    # the htmx swap. Without req, _respond returned the full Layout and
    # htmx swapped that into #main, producing a nested layout that
    # visually shifted the page right.
    return get_activity_chat(aid, req=req)


@rt_route("/chat/activity/{aid}/comment")
def post_activity_comment(aid: int, comment: str = ""):
    with db.connect() as conn:
        db.comment_set(conn, aid, comment.strip())
    return Div(i18n.t("ui.saved_indicator"), cls="text-green-400")


# ── Report-generation SSE endpoint ─────────────────────────────────────────────
# Subscribes the client to the in-memory job for `aid`. Replays all completed
# sections, then tails new ones until done/error. Heartbeat every 15s keeps
# the connection alive through proxies.
@rt_route("/chat/activity/{aid}/report/stream")
def get_report_stream(aid: int):
    job = report_jobs.get(aid)
    if job is None:
        return Response("no job", status_code=404)
    q = job.subscribe()

    def _gen():
        try:
            while True:
                try:
                    ev_type, data = q.get(timeout=15)
                except queue.Empty:
                    yield ": heartbeat\n\n"
                    continue
                if data is None:
                    yield f"event: {ev_type}\ndata: \n\n"
                else:
                    yield (f"event: {ev_type}\n"
                           f"data: {json.dumps(data, ensure_ascii=False)}\n\n")
                if ev_type in ("done", "error"):
                    break
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        _gen(), media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


# ── SSE streaming endpoint ─────────────────────────────────────────────────────
@rt_route("/sse/{token}")
def sse_stream(token: str):
    info = _pending_streams.pop(token, None)
    if not info:
        return Response("invalid token", status_code=404)

    kind = info["kind"]
    aid = info.get("aid")

    def _build_messages():
        """Build LLM messages with rolling-summary semantics. If history exceeds
        RECENT_N (50), older messages are compressed into a 【之前对话摘要】
        block appended to the system prompt; only msgs[new_idx:] are sent
        verbatim. Summary state is persisted back via *_replace so the next
        request reuses it."""
        cfg = uc.load()
        if kind == "activity_seed":
            return info["messages"]

        # Load full thread state (messages + summary + summary_through_idx)
        with db.connect() as conn:
            if kind == "overall":
                state = db.overall_chat_load(conn)
            else:  # activity
                state = db.review_chat_load(conn, aid)
        msgs = state.get("messages", [])
        summary = state.get("summary", "")
        sum_idx = state.get("summary_through_idx", 0)

        # Maybe extend the summary (no-op if thread short enough)
        new_sum, new_idx = maybe_resummarize(msgs, summary, sum_idx, DEFAULT_MODEL)
        if new_sum != summary or new_idx != sum_idx:
            with db.connect() as conn:
                if kind == "overall":
                    db.overall_chat_replace(conn, msgs, new_sum, new_idx)
                else:
                    db.review_chat_replace(conn, aid, msgs, new_sum, new_idx)

        # Build system prompt — 3-block framework (coach_sys + 【用户 Garmin 数据】
        # + 【教练分析】) AND append a hard word-count cap to control verbosity.
        # 训练推荐 isn't a feature here, so 【教练分析】 stays as a minimal
        # "no plan" placeholder.
        if kind == "overall":
            ctx = gd.build_coaching_context(gd.load_detailed(), gd.load_longterm(), cfg)
            sys_prompt = (
                coach_sys(cfg)
                + i18n.t("overall_sys.user_data_header") + ctx
                + i18n.t("overall_sys.coach_analysis_empty")
                + i18n.t("overall_sys.length_cap")
                + _follow_ups_instruction()
            )
        else:
            # Activity follow-up chat: review_chat_sys + typed addendum (so
            # follow-ups keep the same voice as the initial report) + the
            # full activity data context + date background.
            with db.connect() as conn:
                tag = db.tag_get(conn, aid) or ""

            # Re-derive activity context. Builder dispatch is cheap (just reads
            # SQLite) and gives us the same context_md the report saw.
            try:
                row = None
                with db.connect() as conn:
                    row = conn.execute(
                        "SELECT activity_type_key FROM activities WHERE activity_id=?",
                        (aid,)
                    ).fetchone()
                type_key = row[0] if row else ""
                review_ctx = _get_or_build_review_ctx(aid, tag, type_key)
            except Exception:
                review_ctx = ""

            # Date background — find the activity in detailed and pass to helper
            detailed = gd.load_detailed() or {}
            sel_act = next(
                (a for a in detailed.get("activities", []) if a.get("activityId") == aid),
                None,
            )
            date_bg = _build_date_background(sel_act, detailed) if sel_act else ""

            # Chat-style addendum from the typed prompt (role + voice; same
            # one the report used so chat doesn't tonally diverge from report)
            prompt_name = uc.ACTIVITY_TAG_TO_PROMPT.get(tag, "")
            chat_addendum = extract_chat_addendum(prompt_name) if prompt_name else ""

            sys_prompt = (
                review_chat_sys(cfg)
                + (("\n\n" + chat_addendum) if chat_addendum else "")
                + ((i18n.t("overall_sys.activity_data_header") + review_ctx) if review_ctx else "")
                + ((i18n.t("overall_sys.training_background_header") + date_bg) if date_bg else "")
                + _follow_ups_instruction()
            )
        if new_sum:
            sys_prompt = sys_prompt + i18n.t("overall_sys.prior_summary_header") + new_sum

        # Time-awareness: tell the LLM what "now" is, and stamp user msgs with
        # send-time so it can reason about gaps (e.g. "you asked about Thu's
        # intervals on Wed evening; they're now in the past").
        sys_prompt = sys_prompt + ta.now_block()
        recent = ta.annotate_history(msgs[new_idx:])
        return [{"role": "system", "content": sys_prompt}] + recent

    # ── Lazy seed branch: do fetch + builder + prompt assembly INSIDE the
    # SSE generator so the panel renders instantly and the user sees
    # progressive status updates (Claude-Code-style "rotating verbs") instead
    # of a blank cursor for 5-30s. Each phase yields a `status` event the
    # client renders in the bubble until first real `chunk` arrives.
    if kind == "activity_seed_lazy":
        def _sse(ev_name: str, payload: str) -> str:
            return f"event: {ev_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

        def _gen_lazy():
            try:
                with db.connect() as conn:
                    tag = db.tag_get(conn, aid) or ""
                    need_fetch = not db.has_full_detail(conn, aid)
                if need_fetch:
                    yield _sse("status", i18n.t("sse.fetch_first"))
                    try:
                        gd.fetch_activity_detail(aid)
                    except Exception as e:
                        yield _sse("chunk", i18n.t("sse.fetch_failed", e=e))
                        yield "event: done\ndata: \n\n"
                        return
                else:
                    yield _sse("status", i18n.t("sse.fetch_cached"))

                with db.connect() as conn:
                    row = conn.execute(
                        "SELECT activity_type_key FROM activities WHERE activity_id=?",
                        (aid,)
                    ).fetchone()
                    type_key = row[0] if row else ""

                yield _sse("status", i18n.t("sse.build_review"))
                try:
                    # Cache-aware: cached context_md is returned if builder hash
                    # + tag still match. Otherwise rebuilt + saved.
                    ctx_md = _get_or_build_review_ctx(aid, tag, type_key)
                except Exception as e:
                    yield _sse("chunk", i18n.t("sse.build_failed", e=e))
                    yield "event: done\ndata: \n\n"
                    return

                # Build the user_prompt for an activity follow-up turn.
                cfg = uc.load()
                detailed = gd.load_detailed() or {}
                sel_act = next(
                    (a for a in detailed.get("activities", []) if a.get("activityId") == aid),
                    None,
                )
                date_background = _build_date_background(sel_act, detailed) if sel_act else ""
                tag_instruction = (
                    i18n.t("prompt.tag_instruction.tagged", tag=_tag_label(tag))
                    if tag else
                    i18n.t("prompt.tag_instruction.untagged")
                )
                with db.connect() as conn:
                    comment = db.comment_get(conn, aid) or ""
                comment_instruction = (
                    i18n.t("prompt.comment_instruction.has_comment")
                    if comment else i18n.t("prompt.comment_instruction.no_comment")
                )
                prompt_name = uc.ACTIVITY_TAG_TO_PROMPT.get(tag, "review_report")
                user_prompt = load_prompt(prompt_name).format(
                    activity_context=ctx_md,
                    date_background=date_background,
                    tag_instruction=tag_instruction,
                    comment_instruction=comment_instruction,
                ) + _follow_ups_instruction()
                messages_list = [
                    {"role": "system", "content": coach_sys(cfg) + ta.now_block()},
                    {"role": "user",   "content": user_prompt},
                ]

                yield _sse("status", i18n.t("sse.writing"))
                full = ""
                try:
                    for chunk in llm_stream(messages_list, DEFAULT_MODEL):
                        full += chunk
                        yield _sse("chunk", chunk)
                except Exception as e:
                    err = i18n.t("sse.llm_failed", e=e)
                    full += err
                    yield _sse("chunk", err)

                # Persist (full text including <follow_ups> block — JS extracts on render)
                if full:
                    try:
                        with db.connect() as conn:
                            db.review_chat_append(conn, aid, "assistant", full, model=DEFAULT_MODEL)
                    except Exception:
                        pass
                yield "event: done\ndata: \n\n"
            except Exception as e:
                yield _sse("chunk", i18n.t("sse.internal_error", e=e))
                yield "event: done\ndata: \n\n"

        return StreamingResponse(_gen_lazy(), media_type="text/event-stream",
                                  headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})

    messages = _build_messages()

    def _gen():
        # Initial status — covers overall + activity chats uniformly. Client
        # also runs a cycling timer so the user sees several "thinking"
        # phrases rotate every ~2.5s (writing phase often takes 5-30s).
        # Note: activity_seed_lazy has its own multi-phase status above,
        # so this single initial status only fires for the non-lazy kinds.
        yield f"event: status\ndata: {json.dumps(i18n.t('js.stream.chip0'), ensure_ascii=False)}\n\n"
        full = ""
        try:
            # Use tool-calling for overall chat; plain stream for activity / seed
            if kind == "overall":
                handlers = rt.make_overall_tool_handlers()
                for ev_type, payload in llm_stream_with_tools(
                    messages, rt.OVERALL_TOOL_SCHEMAS, handlers,
                    DEFAULT_MODEL,
                ):
                    if ev_type == "text":
                        full += payload
                        yield f"event: chunk\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    elif ev_type == "tool_call":
                        yield _tool_call_sse_event(payload)
                    elif ev_type == "done":
                        break
            elif kind == "activity":
                # Per-activity drill-down tools: get_raw_window_by_time /
                # _by_distance / get_window_stats. The LLM uses these to
                # answer questions like "第 8.5km 的步频" or "Lap 4 末段
                # HR drift" by pulling the relevant 1Hz slice from SQLite.
                handlers = rt.make_tool_handlers(aid)
                for ev_type, payload in llm_stream_with_tools(
                    messages, rt.TOOL_SCHEMAS, handlers, DEFAULT_MODEL,
                ):
                    if ev_type == "text":
                        full += payload
                        yield f"event: chunk\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
                    elif ev_type == "tool_call":
                        yield _tool_call_sse_event(payload)
                    elif ev_type == "done":
                        break
            else:  # activity_seed = initial report
                for chunk in llm_stream(messages, DEFAULT_MODEL):
                    full += chunk
                    yield f"event: chunk\ndata: {json.dumps(chunk, ensure_ascii=False)}\n\n"
        except Exception as e:
            err = i18n.t("ui.llm_call_failed", e=e)
            full += err
            yield f"event: chunk\ndata: {json.dumps(err)[1:-1]}\n\n"

        # Persist assistant msg
        try:
            if kind == "overall" and full:
                with db.connect() as conn:
                    db.overall_chat_append(conn, "assistant", full, model=DEFAULT_MODEL)
            elif kind in ("activity", "activity_seed") and full:
                with db.connect() as conn:
                    db.review_chat_append(conn, aid, "assistant", full, model=DEFAULT_MODEL)
        except Exception:
            pass

        yield "event: done\ndata: \n\n"

    return StreamingResponse(_gen(), media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})


# ── Settings (phase / personal_note / races) ───────────────────────────────────
@rt_route("/settings")
def get_settings(req=None):
    cfg = uc.load()
    with db.connect() as conn:
        races = db.races_list(conn)

    phase = cfg.get("phase", "maintenance")
    personal_note = cfg.get("personal_note", "")

    # Taper intentionally omitted — the coach reads the race calendar and
    # auto-applies taper logic when a race is N days away. No reason to
    # ask the user to switch phases manually as race approaches.
    phase_options = [
        Option(i18n.t("phase.base"),        value="base",        selected=(phase == "base")),
        Option(i18n.t("phase.build"),       value="build",       selected=(phase == "build")),
        Option(i18n.t("phase.race_prep"),   value="race_prep",   selected=(phase == "race_prep")),
        Option(i18n.t("phase.recovery"),    value="recovery",    selected=(phase == "recovery")),
        Option(i18n.t("phase.maintenance"), value="maintenance", selected=(phase == "maintenance")),
    ]

    race_rows = [_race_row_view(r) for r in races]
    if not race_rows:
        race_rows = [Div(i18n.t("settings.race.empty"), cls="text-sm text-gray-500 py-3")]

    panel = (
        Div(
            # Scroll-reset shim: chat pages leave the window scrolled to
            # the bottom; on htmx swap show:top handles it, but full-page
            # loads (e.g. after a /locale POST → 303 → GET /settings) hit
            # the browser's scroll-restoration and land at the previous
            # position. Forcing scrollTo(0,0) here keeps entry consistent.
            Script("window.scrollTo(0, 0);"),
            Div(
                H1(i18n.t("settings.title"), cls="text-2xl font-semibold mb-6"),

                # ── Language ──────────────────────────────────────────────
                # Dropdown — JS reads value at change time and POSTs to
                # /locale. See `_lang_switcher_script` for why we don't
                # use a plain form+onchange.
                H3(i18n.t("settings.lang.heading"),
                   cls="text-sm font-medium text-gray-200 mb-2"),
                Div(
                    _lang_switcher_script(),
                    _lang_dropdown(i18n.current_locale(), next_url="/settings", small=False),
                    P(i18n.t("settings.lang.help"),
                      cls="text-xs text-gray-500 mt-2"),
                    cls="border border-gray-800 rounded-lg p-4 bg-gray-900/40",
                ),

                # ── ① Training phase ──────────────────────────────────────
                H3(i18n.t("settings.phase.heading"),
                   cls="text-sm font-medium text-gray-200 mt-8 mb-2"),
                Div(
                    Form(
                        Select(*phase_options, name="phase",
                               cls="w-full bg-gray-800 text-gray-100 rounded "
                                   "px-3 py-2 border border-gray-700 "
                                   "focus:border-blue-500 focus:outline-none text-sm"),
                        hx_post="/settings/phase",
                        hx_trigger="change",
                        hx_target="#phase-status",
                        hx_swap="innerHTML",
                    ),
                    P(i18n.t("settings.phase.help"),
                      cls="text-xs text-gray-500 mt-2"),
                    Div(id="phase-status", cls="text-xs text-emerald-400 mt-1"),
                    cls="border border-gray-800 rounded-lg p-4 bg-gray-900/40",
                ),

                # ── ② Race calendar ───────────────────────────────────────
                H3(i18n.t("settings.race.heading"),
                   cls="text-sm font-medium text-gray-200 mt-8 mb-2"),
                Div(
                    Div(*race_rows, id="races-list"),
                    Hr(cls="border-gray-800 my-4"),
                    P(i18n.t("settings.race.add_label"), cls="text-xs text-gray-500 mb-2"),
                    Form(
                        # Row 1 — name (full width, since names are long)
                        Input(type="text", name="name",
                              placeholder=i18n.t("settings.race.name_ph"),
                              required=True,
                              cls="w-full bg-gray-800 text-gray-100 rounded "
                                  "px-3 py-2 border border-gray-700 text-sm"),
                        # Row 2 — date / distance / terrain / goal
                        Div(
                            Div(
                                Span(i18n.t("settings.race.field_date"),
                                     cls="block text-[10px] text-gray-500 mb-0.5"),
                                Input(type="date", name="date",
                                      cls="w-full bg-gray-800 text-gray-100 rounded "
                                          "px-3 py-2 border border-gray-700 text-sm"),
                            ),
                            Div(
                                Span(i18n.t("settings.race.field_dist"),
                                     cls="block text-[10px] text-gray-500 mb-0.5"),
                                _distance_picker(None, id_prefix="add-dist"),
                            ),
                            Div(
                                Span(i18n.t("settings.race.field_terrain"),
                                     cls="block text-[10px] text-gray-500 mb-0.5"),
                                _terrain_picker(""),
                            ),
                            Div(
                                Span(i18n.t("settings.race.field_goal"),
                                     cls="block text-[10px] text-gray-500 mb-0.5"),
                                _goal_time_picker(""),
                            ),
                            cls="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 "
                                "gap-2 mt-2",
                        ),
                        # Row 3 — submit
                        Button(i18n.t("settings.race.add_btn"), type="submit",
                               cls="text-sm bg-blue-600 hover:bg-blue-500 text-white "
                                   "px-4 py-1.5 rounded mt-3"),
                        hx_post="/races/add",
                        hx_target="#races-list",
                        hx_swap="innerHTML",
                    ),
                    cls="border border-gray-800 rounded-lg p-4 bg-gray-900/40",
                ),

                # ── ③ About me ────────────────────────────────────────────
                H3(i18n.t("settings.note.heading"),
                   cls="text-sm font-medium text-gray-200 mt-8 mb-2"),
                Div(
                    Form(
                        Textarea(personal_note, name="personal_note", rows="8",
                                 placeholder=i18n.t("settings.note.placeholder"),
                                 cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 "
                                     "border border-gray-700 focus:border-blue-500 "
                                     "focus:outline-none text-sm leading-relaxed"),
                        Div(
                            Button(i18n.t("settings.note.save"), type="submit",
                                   cls="text-sm bg-blue-600 hover:bg-blue-500 text-white "
                                       "px-4 py-1.5 rounded"),
                            Button(i18n.t("settings.note.distill"), type="button",
                                   onclick="distillNote(this)",
                                   cls="text-sm bg-gray-700 hover:bg-gray-600 text-gray-100 "
                                       "px-4 py-1.5 rounded"),
                            cls="flex gap-2 mt-3",
                        ),
                        hx_post="/settings/note",
                        hx_target="#note-status",
                        hx_swap="innerHTML",
                    ),
                    Div(id="note-status", cls="text-xs text-emerald-400 mt-1"),
                    cls="border border-gray-800 rounded-lg p-4 bg-gray-900/40",
                ),

                # ── ④ Long-term memory ────────────────────────────────────
                # User-pinned insights (via 📌 in any assistant bubble) AND
                # manual entries here. Auto-injected into coach_sys() +
                # review_chat_sys() prompts — every future LLM call sees them.
                H3(i18n.t("settings.insights.heading"),
                   cls="text-sm font-medium text-gray-200 mt-8 mb-2"),
                Div(
                    Div(*_insights_list_html(), id="insights-list")
                        if isinstance(_insights_list_html(), list)
                        else Div(_insights_list_html(), id="insights-list"),
                    Hr(cls="border-gray-800 my-4"),
                    P(i18n.t("settings.insights.add_label"),
                      cls="text-xs text-gray-500 mb-2"),
                    Form(
                        Textarea("",
                                 name="text",
                                 rows="3",
                                 required=True,
                                 placeholder=i18n.t("settings.insights.placeholder"),
                                 cls="w-full bg-gray-800 text-gray-100 rounded "
                                     "px-3 py-2 border border-gray-700 text-sm "
                                     "focus:border-blue-500 focus:outline-none "
                                     "leading-relaxed"),
                        Button(i18n.t("settings.insights.save"), type="submit",
                               cls="text-sm bg-blue-600 hover:bg-blue-500 text-white "
                                   "px-4 py-1.5 rounded mt-2"),
                        hx_post="/insights/add",
                        hx_target="#insights-list",
                        hx_swap="innerHTML",
                        # JS to clear the textarea after submit succeeds
                        **{"data-clear-on-send": "1"},
                    ),
                    P(i18n.t("settings.insights.help"),
                      cls="text-xs text-gray-500 mt-3"),
                    cls="border border-gray-800 rounded-lg p-4 bg-gray-900/40",
                ),

                # ── 🎓 Onboarding tour ────────────────────────────────────
                # Tour state is in localStorage; this clears it + redirects
                # to the home page where the tour auto-triggers again.
                H3(i18n.t("settings.tour.heading"),
                   cls="text-sm font-medium text-gray-200 mt-8 mb-2"),
                Div(
                    P(i18n.t("settings.tour.help"),
                      cls="text-xs text-gray-500 mb-3"),
                    Button(
                        i18n.t("settings.tour.replay"),
                        type="button",
                        onclick=("localStorage.removeItem('coach_onboarded_v1');"
                                 " window.location.href='/chat/overall'"),
                        cls="text-sm bg-gray-700 hover:bg-gray-600 text-gray-100 "
                            "px-4 py-2 rounded",
                    ),
                    cls="border border-gray-800 rounded-lg p-4 bg-gray-900/40",
                ),

                # ── ⚠ Danger Zone ─────────────────────────────────────────
                # Bottom because destructive — wipes garth session + entire
                # SQLite (activities, chats, tags, comments). Confirmation
                # dialog spells out what gets wiped + requires explicit click.
                H3(i18n.t("settings.danger.heading"),
                   cls="text-sm font-medium text-red-400 mt-12 mb-2"),
                Div(
                    P(i18n.t("settings.danger.summary"),
                      cls="text-xs text-gray-500 mb-3"),
                    Button(
                        i18n.t("settings.danger.disconnect_btn"),
                        type="button",
                        onclick="document.getElementById('disconnect-dlg').showModal()",
                        cls="w-full bg-red-600 hover:bg-red-500 text-white "
                            "px-4 py-2.5 rounded text-sm font-medium",
                    ),
                    cls="border border-red-900/50 rounded-lg p-4 bg-red-950/20",
                ),

                # ── Footer: version + open-source attribution ─────────────
                Div(
                    P(i18n.t("settings.footer.tagline", version=_APP_VERSION),
                      cls="text-[11px] text-gray-500"),
                    P(i18n.t("settings.footer.credits"),
                      cls="text-[11px] text-gray-600 mt-1 leading-relaxed"),
                    cls="text-center mt-12 pb-2",
                ),

                cls="max-w-2xl mx-auto px-6 py-8",
            ),
            # The native <dialog> for disconnect confirmation. Sits OUTSIDE
            # the scroll container at panel root so showModal() backdrop
            # covers the full viewport correctly.
            Dialog(
                Div(
                    H3(i18n.t("settings.danger.confirm_title"),
                       cls="text-base font-semibold text-red-400 mb-3"),
                    Div(
                        P(i18n.t("settings.danger.confirm_lead"),
                          Span(i18n.t("settings.danger.confirm_wipe"), cls="font-bold text-red-300"),
                          i18n.t("settings.danger.confirm_and"),
                          Span(i18n.t("settings.danger.confirm_irreversible"), cls="font-bold text-red-300"),
                          i18n.t("settings.danger.confirm_colon"),
                          cls="text-sm text-gray-200 mb-2"),
                        Ul(
                            Li(i18n.t("settings.danger.wipe_item_data"), cls="text-xs text-gray-400"),
                            Li(i18n.t("settings.danger.wipe_item_chats"), cls="text-xs text-gray-400"),
                            Li(i18n.t("settings.danger.wipe_item_cache"), cls="text-xs text-gray-400"),
                            cls="list-disc pl-5 mb-3 space-y-0.5",
                        ),
                        P(Span(i18n.t("settings.danger.keep_label"), cls="font-bold text-emerald-300"),
                          i18n.t("settings.danger.keep_paren"),
                          cls="text-sm text-gray-200 mb-1"),
                        Ul(
                            Li(i18n.t("settings.danger.keep_item_user"), cls="text-xs text-gray-400"),
                            Li(i18n.t("settings.danger.keep_item_tags"), cls="text-xs text-gray-400"),
                            cls="list-disc pl-5 mb-3 space-y-0.5",
                        ),
                        P(i18n.t("settings.danger.refresh_a"),
                          Span(i18n.t("settings.danger.refresh_dont"), cls="font-semibold text-yellow-300"),
                          i18n.t("settings.danger.refresh_b"),
                          cls="text-xs text-gray-500 italic"),
                        cls="mb-5",
                    ),
                    Div(
                        Button(
                            i18n.t("settings.danger.cancel"),
                            type="button",
                            onclick="document.getElementById('disconnect-dlg').close()",
                            cls="flex-1 bg-gray-700 hover:bg-gray-600 text-gray-100 "
                                "px-4 py-2 rounded text-sm",
                        ),
                        # Native form POST (not GET via window.location) so
                        # /logout's destructive wipe can't be triggered by a
                        # browser prefetch / accidental URL nav. The 303
                        # redirect after wipe takes user back to the login
                        # screen via a full page reload.
                        Form(
                            Button(
                                i18n.t("settings.danger.confirm"),
                                type="submit",
                                cls="w-full bg-red-600 hover:bg-red-500 text-white "
                                    "px-4 py-2 rounded text-sm font-medium",
                            ),
                            method="post",
                            action="/logout",
                            cls="flex-1",
                        ),
                        cls="flex gap-2",
                    ),
                    cls="p-5",
                ),
                id="disconnect-dlg",
                cls="app-dialog",  # raw CSS class defined in head <style>
            ),
        )
    )
    return _respond(panel, active_route="/settings", req=req)


@rt_route("/settings/phase")
def post_phase(phase: str):
    with db.connect() as conn:
        db.config_set(conn, "phase", phase)
    return Div(i18n.t("ui.saved_indicator"), cls="text-green-400")


@rt_route("/settings/note")
def post_note(personal_note: str = ""):
    with db.connect() as conn:
        db.config_set(conn, "personal_note", personal_note.strip())
    return Div(i18n.t("ui.saved_indicator"), cls="text-green-400")


@rt_route("/settings/note/distill")
def post_note_distill(personal_note: str = ""):
    text = (personal_note or "").strip()
    if not text:
        return Response("", status_code=200)
    try:
        return _refine_personal_note_with_llm(text, DEFAULT_MODEL)
    except Exception as e:
        return Response(f"refine failed: {e}", status_code=500)


@rt_route("/races/add")
def post_race_add(name: str, date: str = "",
                  distance_km: str = "", distance_km_other: str = "",
                  terrain: str = "",
                  goal_hours: str = "0", goal_minutes: str = "0"):
    if not name.strip():
        return _races_list_html()
    d_km      = _parse_form_distance(distance_km, distance_km_other)
    goal_time = _parse_form_goal_time(goal_hours, goal_minutes)
    with db.connect() as conn:
        db.races_add(conn, name.strip(), date.strip() or None, d_km,
                     terrain.strip() or None, goal_time, None)
    return _races_list_html()


# ── Pin / 长期记忆 routes ──────────────────────────────────────────────────
# Async fetch from JS (not htmx) since the dialog flow is fully client-side.
# These return tiny plain text (or empty 200) — no fragment swap needed.
@rt_route("/pin/distill")
def post_pin_distill(text: str = ""):
    """One-shot LLM call to compress text → ≤20字 insight. Delegates to
    `_distill_with_llm` via the prompts/insight_distill.md prompt."""
    text = (text or "").strip()
    if not text:
        return Response("", status_code=400)
    try:
        return _distill_with_llm(text, DEFAULT_MODEL)
    except Exception as e:
        return Response(f"distill failed: {e}", status_code=500)


@rt_route("/pin/save")
def post_pin_save(text: str = "", source: str = ""):
    """Persist a long-term insight to user_coach_insights. Source = page
    URL where the user pinned from (e.g. /chat/activity/123 or /chat/overall).
    coach_sys() / review_chat_sys() automatically inject these into the
    system prompt for ALL future LLM calls — no further wiring needed."""
    text = (text or "").strip()
    if not text:
        return Response("", status_code=400)
    with db.connect() as conn:
        db.insights_add(conn, text, source=source.strip() or "manual")
    return Response("", status_code=200)


@rt_route("/insights/add")
def post_insight_add(text: str = ""):
    """Manual add from settings page (separate from the 📌 chat-pin path).
    Uses source='manual' to distinguish in the list rendering."""
    text = (text or "").strip()
    if not text:
        return _insights_list_html()
    with db.connect() as conn:
        db.insights_add(conn, text, source="manual")
    return _insights_list_html()


@rt_route("/insights/{insight_id}/delete")
def post_insight_delete(insight_id: int):
    with db.connect() as conn:
        db.insights_delete(conn, insight_id)
    return _insights_list_html()


def _insight_row(i: dict):
    """Read-only insight row with delete button."""
    iid = i["insight_id"]
    src = (i.get("source") or "")
    src_chip = None
    if src and src != "manual":
        # source URL → chip label. activity/* → review chip; overall → coach-chat chip
        if "activity" in src:
            src_chip = Span(i18n.t("settings.insights.src_review"),
                            cls="text-[9px] bg-blue-900/40 text-blue-300 px-1.5 py-0.5 rounded ml-1")
        elif "overall" in src:
            src_chip = Span(i18n.t("settings.insights.src_overall"),
                            cls="text-[9px] bg-emerald-900/40 text-emerald-300 px-1.5 py-0.5 rounded ml-1")
    saved_at = (i.get("saved_at") or "")[:10]
    return Div(
        Div(
            Div(i.get("text", ""), cls="text-sm text-gray-100"),
            Div(
                Span(saved_at, cls="text-[10px] text-gray-500"),
                src_chip,
                cls="flex items-center mt-0.5",
            ),
            cls="flex-1 min-w-0 pr-2",
        ),
        Button(i18n.t("settings.insights.delete"),
               hx_post=f"/insights/{iid}/delete",
               hx_target="#insights-list",
               hx_swap="innerHTML",
               hx_confirm=i18n.t("settings.insights.delete_confirm"),
               cls="text-xs text-red-400 hover:text-red-300 px-2 shrink-0"),
        cls="flex items-start py-2 border-b border-gray-800",
    )


def _insights_list_html():
    with db.connect() as conn:
        items = db.insights_list(conn)
    if not items:
        return Div(i18n.t("settings.insights.empty"),
                   cls="text-sm text-gray-500 py-3")
    return [_insight_row(i) for i in items]


@rt_route("/races/{race_id}/delete")
def post_race_delete(race_id: int):
    with db.connect() as conn:
        db.races_delete(conn, race_id)
    return _races_list_html()


def _fetch_race(race_id: int) -> dict | None:
    """Single-race lookup. db.races_list returns all rows — for the per-row
    edit/view swaps we just grab the one we need."""
    with db.connect() as conn:
        for r in db.races_list(conn):
            if r.get("race_id") == race_id:
                return r
    return None


@rt_route("/races/{race_id}/edit")
def get_race_edit(race_id: int):
    """GET — swap a race row to inline edit form."""
    r = _fetch_race(race_id)
    if not r:
        return _races_list_html()
    return _race_row_edit(r)


@rt_route("/races/{race_id}/view")
def get_race_view(race_id: int):
    """GET — swap inline edit form back to read-only view (cancel)."""
    r = _fetch_race(race_id)
    if not r:
        return _races_list_html()
    return _race_row_view(r)


@rt_route("/races/{race_id}/update")
def post_race_update(race_id: int, name: str = "", date: str = "",
                     distance_km: str = "", distance_km_other: str = "",
                     terrain: str = "",
                     goal_hours: str = "0", goal_minutes: str = "0"):
    """POST — persist edits and swap back to view. Empty name falls back
    to just re-rendering the view (defensive — HTML required attr should
    prevent submit with empty name)."""
    name = name.strip()
    if not name:
        return _race_row_view(_fetch_race(race_id) or {})
    d_km      = _parse_form_distance(distance_km, distance_km_other)
    goal_time = _parse_form_goal_time(goal_hours, goal_minutes)
    with db.connect() as conn:
        db.races_update(
            conn, race_id,
            name=name,
            date=date.strip() or None,
            distance_km=d_km,
            terrain=terrain.strip() or None,
            goal_time=goal_time,
        )
    return _race_row_view(_fetch_race(race_id))


# ── Race-form input helpers ─────────────────────────────────────────────────
# All 3 used in BOTH the add form (top) AND the inline edit form (per row).
# Distances + terrain are dropdowns to eliminate typing. Goal time is two
# selects (hours + minutes-by-5) — covers ~all common race goals (3:00,
# 3:30, 1:30, 1:35, 2:00, etc.) without char-by-char input.

_RACE_DISTANCES = [
    # (i18n label key, value-as-string). Labels rendered via i18n.t at render
    # time so picker labels follow the user's locale.
    ("settings.race.dist_5k",    "5"),
    ("settings.race.dist_10k",   "10"),
    ("settings.race.dist_half",  "21.1"),
    ("settings.race.dist_full",  "42.2"),
    ("settings.race.dist_other", "other"),
]
_RACE_DISTANCE_PRESETS = {v for _, v in _RACE_DISTANCES if v != "other"}

# (i18n label key, stored value). Values are stable English keys after
# migration #9 — UI shows the locale-appropriate label via i18n.t().
_RACE_TERRAINS = [
    ("settings.race.terrain_road",  "road"),
    ("settings.race.terrain_trail", "trail"),
]


def _distance_picker(current_km, *, id_prefix: str):
    """Dropdown + conditional 'other' manual-input. id_prefix must be unique
    per form on the page (e.g. 'add-dist' vs 'edit-dist-{rid}') so the
    onchange selector finds the right manual-input field."""
    cur = "" if current_km in (None, "") else str(current_km)
    # Force booleans — `cur and ...` returns "" (falsy) when cur is empty,
    # and FastHTML emits `selected=""` for empty-string truthy values which
    # browsers treat as selected → all options end up "selected" silently.
    is_other = bool(cur) and cur not in _RACE_DISTANCE_PRESETS
    other_id = f"{id_prefix}-other"

    options = [Option(i18n.t("settings.race.dist_picker_placeholder"),
                      value="", disabled=True, selected=(not cur))]
    for label_key, val in _RACE_DISTANCES:
        sel = bool((cur == val) or (is_other and val == "other"))
        # Only pass selected=True when truly selected — passing False still
        # emits the attr in FastHTML, which browsers treat as selected.
        opt_kwargs = {"selected": True} if sel else {}
        options.append(Option(i18n.t(label_key), value=val, **opt_kwargs))

    return Div(
        Select(
            *options,
            name="distance_km",
            onchange=(f"document.getElementById('{other_id}').style.display = "
                      "this.value === 'other' ? 'block' : 'none'"),
            cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 "
                "border border-gray-700 text-sm",
        ),
        Input(
            type="number", step="0.1", min="0.1",
            name="distance_km_other",
            value=(cur if is_other else ""),
            placeholder=i18n.t("settings.race.dist_other_ph"),
            id=other_id,
            style=("display: block;" if is_other else "display: none;"),
            cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 "
                "border border-gray-700 text-sm mt-1",
        ),
    )


def _terrain_picker(current: str):
    cur = (current or "").strip()
    options = [Option(i18n.t("settings.race.terrain_picker_placeholder"),
                      value="", disabled=True, selected=(not cur))]
    for label_key, val in _RACE_TERRAINS:
        options.append(Option(i18n.t(label_key), value=val, selected=(cur == val)))
    return Select(
        *options,
        name="terrain",
        cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 "
            "border border-gray-700 text-sm",
    )


def _terrain_label(stored: str | None) -> str:
    """Render a stored terrain key ('road'/'trail') via i18n. Falls back to
    the raw value for any unmapped legacy data so nothing disappears."""
    if not stored:
        return ""
    if stored == "road":
        return i18n.t("settings.race.terrain_road")
    if stored == "trail":
        return i18n.t("settings.race.terrain_trail")
    return stored


def _goal_time_picker(current: str):
    """Two selects: hours (0-12) + minutes (0-55 by 5). Server combines into
    'H:MM:00' for storage. Edit case: parse current 'H:MM:SS', round minutes
    to nearest 5 (35→35, 33→35, 31→30) so the picker can always pre-select."""
    h_cur, m_cur = 0, 0
    if current:
        try:
            parts = (current or "").split(":")
            h_cur = int(parts[0])
            if len(parts) > 1:
                m_cur = int(parts[1])
                m_cur = round(m_cur / 5) * 5
                if m_cur >= 60:
                    h_cur += 1
                    m_cur = 0
        except Exception:
            pass

    # 0–23 小时 — covers ultras like UTMB 168K (~30h pros / DNF cutoff 46h
    # but 23h ceiling fits 99% of goals; rare beyond that anyway).
    h_options = [Option(f"{h}", value=str(h), selected=(h == h_cur)) for h in range(0, 24)]
    m_options = [Option(f"{m:02d}", value=str(m), selected=(m == m_cur)) for m in range(0, 60, 5)]

    sel_cls = ("bg-gray-800 text-gray-100 rounded px-2 py-2 "
               "border border-gray-700 text-sm flex-1 min-w-0")
    return Div(
        Select(*h_options, name="goal_hours", cls=sel_cls),
        Span(i18n.t("settings.race.goal_hours"), cls="text-xs text-gray-500 mx-1"),
        Select(*m_options, name="goal_minutes", cls=sel_cls),
        Span(i18n.t("settings.race.goal_minutes"), cls="text-xs text-gray-500 ml-1"),
        cls="flex items-center w-full",
    )


def _parse_form_distance(distance_km: str, distance_km_other: str) -> float | None:
    """Server-side: pick the right field based on what the user chose."""
    if distance_km == "other":
        try: return float(distance_km_other) if distance_km_other else None
        except ValueError: return None
    if distance_km:
        try: return float(distance_km)
        except ValueError: return None
    return None


def _parse_form_goal_time(goal_hours: str, goal_minutes: str) -> str | None:
    """Combine two select values back into 'H:MM:00' (matches existing schema).
    Returns None if both 0 (= "no goal set")."""
    try:
        h, m = int(goal_hours or 0), int(goal_minutes or 0)
    except ValueError:
        return None
    if h == 0 and m == 0:
        return None
    return f"{h}:{m:02d}:00"


def _race_row_view(r: dict):
    """Read-only race row with Edit + Delete buttons. Edit click swaps just
    this row's outerHTML to the inline edit form (#race-row-{id} target);
    delete refreshes the whole list."""
    rid = r["race_id"]
    terrain_disp = _terrain_label(r.get("terrain"))
    goal_disp    = i18n.t("settings.race.row_goal", time=r["goal_time"]) if r.get("goal_time") else ""
    date_disp    = r.get("date", "") or i18n.t("settings.race.row_dash")
    return Div(
        Div(
            Span(r.get("name", ""), cls="text-sm font-medium text-gray-100"),
            Span(f"{date_disp} · {r.get('distance_km','-')}km" +
                 (f" · {terrain_disp}" if terrain_disp else "") +
                 (f" · {goal_disp}" if goal_disp else ""),
                 cls="block text-xs text-gray-500 mt-0.5"),
            cls="flex-1 min-w-0",
        ),
        Button(i18n.t("settings.race.edit"),
               hx_get=f"/races/{rid}/edit",
               hx_target=f"#race-row-{rid}",
               hx_swap="outerHTML",
               cls="text-xs text-blue-400 hover:text-blue-300 px-2 shrink-0"),
        Button(i18n.t("settings.race.delete"),
               hx_post=f"/races/{rid}/delete",
               hx_target="#races-list",
               hx_swap="innerHTML",
               hx_confirm=i18n.t("settings.race.delete_confirm"),
               cls="text-xs text-red-400 hover:text-red-300 px-2 shrink-0"),
        id=f"race-row-{rid}",
        cls="flex items-center py-2 border-b border-gray-800",
    )


def _race_row_edit(r: dict):
    """Inline edit form replacing one race row. 3-row layout matches the
    add form: row 1 = name (full width), row 2 = date / distance / terrain
    / goal_time (4-column), row 3 = save + cancel.

    Save → POST /races/{id}/update → swap back to view.
    Cancel → GET /races/{id}/view → swap back to view (discard form)."""
    rid = r["race_id"]
    return Form(
        # Row 1 — name (full width)
        Input(type="text", name="name", value=r.get("name", ""), required=True,
              placeholder=i18n.t("settings.race.name_edit_ph"),
              cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 "
                  "border border-gray-700 text-sm"),
        # Row 2 — 4 fields (mobile: 2 cols / desktop: 4 cols)
        Div(
            Div(
                Span(i18n.t("settings.race.field_date"),
                     cls="block text-[10px] text-gray-500 mb-0.5"),
                Input(type="date", name="date", value=r.get("date") or "",
                      cls="w-full bg-gray-800 text-gray-100 rounded px-3 py-2 "
                          "border border-gray-700 text-sm"),
            ),
            Div(
                Span(i18n.t("settings.race.field_dist"),
                     cls="block text-[10px] text-gray-500 mb-0.5"),
                _distance_picker(r.get("distance_km"), id_prefix=f"edit-dist-{rid}"),
            ),
            Div(
                Span(i18n.t("settings.race.field_terrain"),
                     cls="block text-[10px] text-gray-500 mb-0.5"),
                _terrain_picker(r.get("terrain") or ""),
            ),
            Div(
                Span(i18n.t("settings.race.field_goal"),
                     cls="block text-[10px] text-gray-500 mb-0.5"),
                _goal_time_picker(r.get("goal_time") or ""),
            ),
            cls="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-4 gap-2 mt-2",
        ),
        # Row 3 — save + cancel
        Div(
            Button(i18n.t("settings.race.save"), type="submit",
                   cls="text-sm bg-blue-600 hover:bg-blue-500 text-white "
                       "px-4 py-1.5 rounded"),
            A(i18n.t("settings.race.cancel"), href="#",
              hx_get=f"/races/{rid}/view",
              hx_target=f"#race-row-{rid}",
              hx_swap="outerHTML",
              cls="text-sm text-gray-400 hover:text-gray-200 px-2 cursor-pointer"),
            cls="flex items-center gap-2 mt-3",
        ),
        hx_post=f"/races/{rid}/update",
        hx_target=f"#race-row-{rid}",
        hx_swap="outerHTML",
        id=f"race-row-{rid}",
        cls="py-3 border-b border-gray-800 bg-gray-900/60 px-2 rounded",
    )


def _races_list_html():
    with db.connect() as conn:
        races = db.races_list(conn)
    if not races:
        return Div(i18n.t("settings.race.empty"), cls="text-sm text-gray-500 py-3")
    return [_race_row_view(r) for r in races]


# ── Boot ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("APP_PORT", "8507"))
    print(f"\n[coach] DATA_DIR={_DATA_DIR}")
    print(f"[coach] DB={_DB_PATH} (exists={os.path.exists(_DB_PATH)})")
    print(f"[coach] LLM_BASE={LLM_BASE}  model={DEFAULT_MODEL}")
    print(f"[coach] serving on http://0.0.0.0:{port}\n")
    # reload=True is FastHTML's default — fine for hacking, but in containers
    # it spawns a watchfiles thread that opens an inotify watcher per file
    # and trips "too many open files" against the host's user limit. Manual
    # `pkill + ./start.sh` is enough for dev; disable here.
    serve(host="0.0.0.0", port=port, reload=False)
