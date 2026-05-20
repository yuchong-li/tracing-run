"""en-US string catalog. Keys are dotted (namespace.key)."""

STRINGS: dict[str, str] = {
    # ── Language picker ───────────────────────────────────────────────────────
    "auth.lang.zh":       "简体中文",
    "auth.lang.en":       "English",
    "auth.lang.label":    "Language / 语言",

    # ── Lock screen (PasswordMiddleware /lock) ────────────────────────────────
    "lock.title":          "🔐 Locked",
    "lock.brand":          "tracing.run",
    "lock.prompt":         "Enter password",
    "lock.password_ph":    "Password",
    "lock.submit":         "Enter",
    "lock.wrong":          "Wrong password",

    # ── Garmin connect / login ────────────────────────────────────────────────
    "login.title":         "Connect Garmin",
    "login.subtitle":      "Sign in with your Garmin Connect email + password. MFA (if enabled) prompts on the next step.",
    "login.email_label":   "Email",
    "login.password_label": "Password",
    "login.submit":        "Sign in",
    "login.connecting":    "Signing in…",
    "login.opening":       "Signing in… (opening Garmin)",
    "login.success":       "Signed in. Redirecting…",
    "login.failed":        "Sign-in failed",
    "login.retry":         "Retry",
    "login.mfa_submitted": "MFA submitted, verifying…",
    "login.mfa_prompt":    "Garmin needs your MFA code (check your email)",
    "login.mfa_ph":        "6-digit MFA code",
    "login.mfa_submit":    "Submit",
    "login.idle":          "(idle)",

    # ── Empty data panel (first-time after login, before sync) ────────────────
    "empty.title":         "No activity data yet",
    "empty.subtitle":      "Tap below to run your first sync (last 90 days). Takes about 10–30 seconds.",
    "empty.full_sync":     "🔄 Full sync",

    # ── Reconnect dialog (session expired) ────────────────────────────────────
    "reconnect.title":     "⚠ Garmin session expired",
    "reconnect.body":      "Your auth token has expired and needs a fresh sign-in.",
    "reconnect.hint_lead": "Tap “Reconnect Garmin”",
    "reconnect.hint_body": " — only re-authorizes. All Garmin data / AI chat history / races / notes / long-term memory are ",
    "reconnect.hint_kept": "preserved",
    "reconnect.hint_tail": ".",
    "reconnect.dismiss":   "Later",
    "reconnect.cta":       "Reconnect Garmin",

    # ── iOS "Add to Home Screen" hint overlay ─────────────────────────────────
    "install.title":       "Install to home screen for the app feel",
    "install.subtitle":    "Standalone window, no browser chrome, one tap from home.",
    "install.step1_a":     "Tap the ",
    "install.step1_b":     " share button at the bottom of Safari",
    "install.step2_a":     "Scroll down, choose ",
    "install.step2_pill":  "“Add to Home Screen”",
    "install.step3_a":     "Tap ",
    "install.step3_pill":  "“Add”",
    "install.dismiss":     "Maybe later",
    "install.ack":         "Got it",

    # ── Mobile header (P3 batch 2) ────────────────────────────────────────────
    "mobile.add_to_home":  "📱 Install",
    "mobile.dismiss_hint": "Don't show again",

    # ── Sidebar (P3 batch 2) ──────────────────────────────────────────────────
    "sidebar.no_data":          "No data synced yet",
    "sidebar.sync_incremental": "🔄 Sync Garmin",
    "sidebar.settings":         "⚙️ Settings",
    "sidebar.last_sync":        "Last sync: {when}",
    "sidebar.coach_chat":       "Tracing with coach",
    "sidebar.coach_chat_sub":   "Cross-activity / trends / planning",
    "sidebar.recent":           "Activity debrief",
    "sidebar.day.today":        "Today",
    "sidebar.day.yesterday":    "Yesterday",
    "sidebar.day.last_7":       "Last 7 days",
    "sidebar.day.last_30":      "Last 30 days",
    "sidebar.day.older":        "Older",

    # ── Greeting buckets ──────────────────────────────────────────────────────
    "greeting.morning":   "Good morning",
    "greeting.afternoon": "Good afternoon",
    "greeting.evening":   "Good evening",
    "greeting.night":     "Up late",
    "greeting.sep":       ", ",

    # ── Race countdown ────────────────────────────────────────────────────────
    "race.days_left":         "{days} days to go",
    "race.no_target":         "No target race set",
    "phase.race_prep_short":  "Race prep",
    "phase.recovery_short":   "Recovery",
    "phase.recovery_hint":    "Lighter loads",

    # ── _fmt_relative (sidebar last-sync timestamp) ───────────────────────────
    "rel.dash":         "—",
    "rel.just_now":     "just now",
    "rel.minutes_ago":  "{n} min ago",
    "rel.hours_ago":    "{n} hr ago",
    "rel.days_ago":     "{n}d ago",
    "rel.older_fmt":    "%b %d",

    # ── Activity-row date format (sidebar list) ───────────────────────────────
    "act.row_date_fmt": "%b %d %H:%M",

    # ── Sync state (toast + status polling) ───────────────────────────────────
    "sync.in_progress": "⏳ Syncing…",
    "sync.in_progress_pct": "⏳ {msg} ({pct}%)",
    "sync.done_msg":    "Done ✓",
    "sync.done_toast":  "✓ Sync complete",
    "sync.error_pfx":   "❌ {msg}",
    "sync.session_expired_pfx": "Garmin session expired: {e}",

    # ── Activity-type display map (used by sidebar + LLM context) ─────────────
    "activity_type.running":             "Run",
    "activity_type.trail_running":       "Trail run",
    "activity_type.virtual_ride":        "Zwift ride",
    "activity_type.cycling":             "Ride",
    "activity_type.indoor_cycling":      "Indoor ride",
    "activity_type.swimming":            "Swim",
    "activity_type.open_water_swimming": "Open-water swim",
    "activity_type.strength_training":   "Strength",
    "activity_type.fitness_equipment":   "Gym machine",
    "activity_type.walking":             "Walk",
    "activity_type.hiking":              "Hike",
    "activity_type._unknown":            "Activity",

    # ── Settings page (P3 batch 3) ────────────────────────────────────────────
    "settings.title":            "Settings",

    "settings.lang.heading":     "🌐 Language",
    "settings.lang.help":        "Switches the entire UI and the coach's responses. Takes effect immediately.",

    "settings.phase.heading":    "📅 Training phase",
    "settings.phase.help":       "Frames coach's recommendations — race-prep favors intensity planning, recovery favors load management.",
    "phase.base":                "Base — foundation",
    "phase.build":               "Build — adding intensity",
    "phase.race_prep":           "Race prep — peaking",
    "phase.recovery":            "Recovery",
    "phase.maintenance":         "Maintenance",

    "settings.race.heading":     "🏁 Race plan",
    "settings.race.empty":       "No races yet",
    "settings.race.add_label":   "➕ Add race",
    "settings.race.name_ph":     "Race name (required, e.g. “Melbourne Marathon 2026”)",
    "settings.race.name_edit_ph": "Race name",
    "settings.race.field_date":  "Date",
    "settings.race.field_dist":  "Distance",
    "settings.race.field_terrain": "Terrain",
    "settings.race.field_goal":  "Goal time",
    "settings.race.dist_5k":     "5 km (5K)",
    "settings.race.dist_10k":    "10 km (10K)",
    "settings.race.dist_half":   "21.1 km (half)",
    "settings.race.dist_full":   "42.2 km (full)",
    "settings.race.dist_other":  "Other (custom)",
    "settings.race.dist_picker_placeholder": "— Pick distance —",
    "settings.race.dist_other_ph": "Custom distance (km)",
    "settings.race.terrain_road": "Road",
    "settings.race.terrain_trail": "Trail",
    "settings.race.terrain_picker_placeholder": "— Pick terrain —",
    "settings.race.goal_hours":  "h",
    "settings.race.goal_minutes": "m",
    "settings.race.add_btn":     "Add race",
    "settings.race.save":        "Save",
    "settings.race.cancel":      "Cancel",
    "settings.race.edit":        "Edit",
    "settings.race.delete":      "Delete",
    "settings.race.delete_confirm": "Delete this race?",
    "settings.race.row_dash":    "—",
    "settings.race.row_goal":    "goal {time}",

    "settings.note.heading":     "👤 About me (the coach reads this)",
    "settings.note.placeholder": ("e.g. 32M / 178cm / 68kg, running 5 yrs, "
                                  "half-PB 1:35, target sub-3:30 for Melbourne 2026, "
                                  "ITBS history (left knee).\n\n"
                                  "Useful to cover: age / sex / height / weight / "
                                  "years running / PBs / goal / injury history / lifestyle. "
                                  "Messy is fine — tap ✨ Tidy below to let the AI structure it."),
    "settings.note.save":        "Save",
    "settings.note.distill":     "✨ Tidy",

    "settings.insights.heading": "🧠 Long-term memory",
    "settings.insights.empty":   "No long-term memory yet. Tap 📌 below any assistant reply to add.",
    "settings.insights.add_label": "➕ Add insight manually",
    "settings.insights.placeholder": ("e.g. My true Z2 ceiling is 142bpm (Garmin's auto-zone says 138 — please use 142).\n"
                                       "Or: ITBS history on left knee — flag if cadence drops below 165 late in long runs."),
    "settings.insights.save":    "Save",
    "settings.insights.help":    ("The coach automatically sees these in every reply. "
                                  "If a value is included (e.g. “Z2 ceiling 142”), the coach "
                                  "uses your number rather than what Garmin auto-calculates."),
    "settings.insights.delete":  "Delete",
    "settings.insights.delete_confirm": "Delete this insight?",
    "settings.insights.src_review": "Review",
    "settings.insights.src_overall": "Coach chat",

    "settings.tour.heading":     "🎓 Onboarding tour",
    "settings.tour.help":        ("First launch shows a 4-step tour (sidebar / coach chat / review / settings). "
                                  "Tap to watch again."),
    "settings.tour.replay":      "🎓 Replay tour",

    "settings.danger.heading":   "⚠ Danger Zone",
    "settings.danger.summary":   ("Disconnecting wipes all activity data + every AI chat history. "
                                  "Races / notes / tags / coach insights stay."),
    "settings.danger.disconnect_btn": "Disconnect Garmin (wipe all data)",
    "settings.danger.confirm_title": "⚠ Disconnect Garmin?",
    "settings.danger.confirm_lead": "Disconnecting will ",
    "settings.danger.confirm_wipe": "wipe everything below",
    "settings.danger.confirm_and":  ", and ",
    "settings.danger.confirm_irreversible": "cannot be undone",
    "settings.danger.confirm_colon": ":",
    "settings.danger.wipe_item_data": "All Garmin activity data (SQLite + caches)",
    "settings.danger.wipe_item_chats": "All AI chat history (per-activity reviews + coach chat)",
    "settings.danger.wipe_item_cache": "Builder context cache (Pa:HR / HR drift / etc.)",
    "settings.danger.keep_label": "Kept",
    "settings.danger.keep_paren": " (these aren't in cache/):",
    "settings.danger.keep_item_user": "Races / personal_note / coach insights",
    "settings.danger.keep_item_tags": "Activity tags / workout notes",
    "settings.danger.refresh_a": "If you just want to refresh Garmin auth, ",
    "settings.danger.refresh_dont": "don't tap this",
    "settings.danger.refresh_b": " — when the session expires, a reconnect prompt appears.",
    "settings.danger.cancel":   "Cancel",
    "settings.danger.confirm":  "Confirm — disconnect + wipe",

    "settings.footer.tagline":   "tracing.run · v{version}",
    "settings.footer.credits":   ("Thanks to the open-source projects that make this app possible: "
                                  "FastHTML · htmx · Tailwind CSS · marked.js · plotly · garth · "
                                  "garminconnect · Playwright"),

    # ── Chat panels (overall + activity) ─────────────────────────────────────
    "chat.overall.placeholder":  ("Tracing with coach…"
                                  "|Talk across activities…"
                                  "|Cross-activity chat…"
                                  "|Connect the dots in your training…"
                                  "|Step back, look at the arc…"),
    "chat.activity.placeholder": ("Drill into any segment…"
                                  "|Trace any moment…"
                                  "|Zoom into any window…"
                                  "|Inspect a specific stretch…"
                                  "|Ask about any part of this run…"),
    "chat.empty":                "Start a conversation…",
    "chat.send":                 "Tracing",
    "chat.model_pfx":            "model {model}",
    "chat.clear":                "Clear chat",
    "chat.clear_confirm":        "Clear the current conversation?",
    "chat.clear_confirm_full":   "Clear the current conversation? (Report + follow-ups will be deleted)",
    "chat.activity.empty_pre_report": "After the report is generated, ask follow-ups / drill into any segment here…",
    "chat.activity.placeholder_locked": "Generate the review report first…",
    "chat.activity.placeholder_generating": "Report generating, ask follow-ups in a moment…",

    # ── Overall chat header ───────────────────────────────────────────────────
    "chat.overall.title":     "Tracing with coach",
    "chat.overall.subtitle":  "Cross-activity / trends / planning. For 1Hz second-level drill on a single activity, open that activity's card.",

    # ── Activity chat header (comment editor) ─────────────────────────────────
    "activity.comment.summary":     "📋 Workout / notes",
    "activity.comment.placeholder": "Write down today's workout or post-run notes (the coach will see this)",
    "activity.comment.save":        "Save notes",

    # ── Pre-report nudge panel ────────────────────────────────────────────────
    "nudge.title":          "Ready to review this workout?",
    "nudge.subtitle":       "Pick the type — the coach uses the matching review framework.",
    "nudge.tag.required":   "Workout type",
    "nudge.tag.required_hint": " required",
    "nudge.tag.help":       "Different types use different review frameworks (Intervals / Long Run / Tempo / Race / Trail…)",
    "nudge.tag.placeholder": "— Pick a workout type —",
    "nudge.comment.label":  "Workout / notes",
    "nudge.comment.optional": " optional",
    "nudge.comment.placeholder": "e.g. 4×1km @4:00, 90s rest, WU 2km, CD 1km\nOr: legs felt heavy today, planned 60min but cut to 40min",
    "nudge.comment.help":   "Tell the coach what you intended → execution analysis becomes much more accurate.",
    "nudge.cta":            "🔬 Generate review report",

    # ── Report card (Row 3) ──────────────────────────────────────────────────
    "report.card.title":        "📋 Review report",
    "report.pill.running":      "🔵 Generating…",
    "report.pill.stale":        "🟠 Stale",
    "report.pill.done":         "🟢 Ready",
    "report.pill.empty":        "⚪ Not generated",
    "report.stale.banner":      "⚠ You re-tagged this; the report still uses the old builder.",
    "report.stale.regenerate":  "Regenerate",
    "report.chips_text":        "✍️ Coach is writing",

    # ── SSE status events (server-pushed phase markers) ──────────────────────
    "sse.fetch_first":   "🛰️ First visit — pulling 1Hz data from Garmin (5–30s)…",
    "sse.fetch_cached":  "🛰️ Reading activity data…",
    "sse.fetch_failed":  "\n\n❌ Failed to fetch activity data: {e}",
    "sse.build_review":  "🔬 Building dedicated review (per-lap, Pa:HR, cardiac drift…)",
    "sse.build_failed":  "\n\n❌ Build failed: {e}",
    "sse.writing":       "✍️ Coach is writing…",
    "sse.llm_failed":    "\n\n❌ LLM call failed: {e}",
    "sse.internal_error": "\n\n❌ Internal error: {e}",
    "sse.fetch_detail_failed": "❌ Failed to fetch activity detail: {e}",

    # ── Stale-session error panel ─────────────────────────────────────────────
    "stale_session.error":     "❌ Garmin session expired: {err}",

    # ── Pin dialog (📌 add to long-term memory) ──────────────────────────────
    "pin.btn":           "📌 Add to memory",
    "pin.dlg.title":     "📌 Add to long-term memory",
    "pin.dlg.body":      "Pick out the part of this reply you want to lock in. The coach sees it in every future conversation (home + reviews).",
    "pin.dlg.distill":   "✨ Tidy (≤20 words)",
    "pin.dlg.cancel":    "Cancel",
    "pin.dlg.save":      "Save to memory",

    # ── Inline-JS strings (injected via window.I18N per-request) ─────────────
    "js.loading":                "Loading…",
    "js.tour.skip":              "Skip",
    "js.tour.next":              "Next →",
    "js.tour.done":              "Done ✓",
    "js.tour.step1.title":       "👋 Welcome to tracing.run",
    "js.tour.step1.text":        "Per-activity reviews + cross-activity follow-ups, all built on your Garmin 1Hz data. Quick 4-step tour of the core spots.",
    "js.tour.step2.title":       "🔄 Sync",
    "js.tour.step2.text":        "Tap here after each workout to pull in your new activities. The initial full-history sync runs automatically when you first connect.",
    "js.tour.step3.title":       "💬 Ask the coach",
    "js.tour.step3.text":        "Cross-activity chat. e.g. “Have I been improving lately?” / “Why was last week's long run harder than this week's?” The coach calls tools to look up the data.",
    "js.tour.step4.title":       "🔬 Single review",
    "js.tour.step4.text":        "Tap any activity in the sidebar → the coach uses the matching typed builder (Long Run / Intervals / Tempo / etc.) → ask follow-ups about 1Hz second-level details.",
    "js.tour.step5.title":       "⚙ Settings",
    "js.tour.step5.text":        "Training phase / race calendar / About me / long-term memory / disconnect Garmin. Every setting flows into the coach's prompt.",
    "js.pin.distilling":         "✨ Tidying…",
    "js.pin.distilled":          "✓ Tidied. Edit further or save as is.",
    "js.pin.distill_empty":      "Tidy returned empty, keeping original.",
    "js.pin.distill_fail":       "Tidy failed: {e}",
    "js.pin.saving":             "Saving…",
    "js.pin.saved":              "✓ Added to memory, closing…",
    "js.pin.save_fail":          "Save failed: {e}",
    "js.pin.save_btn":           "Save to memory",
    "js.note.empty":             "Write something first, then tidy",
    "js.note.organizing":        "✨ Tidying…",
    "js.note.organized":         "✓ Tidied. Review then tap save.",
    "js.note.organize_empty":    "Tidy returned empty, keeping original.",
    "js.note.organize_fail":     "Tidy failed: {e}",
    "js.report.chip0":           "✍️ Coach is writing",
    "js.report.chip1":           "🧠 Crunching the data",
    "js.report.chip2":           "📊 Comparing to last time",
    "js.report.chip3":           "🔬 Finding key inflections",
    "js.report.chip4":           "⚡ Stitching the closing bits",
    "js.report.pill.done":       "🟢 Ready",
    "js.report.pill.fail":       "❌ Failed",
    "js.report.fail_msg":        "Generation failed",
    "js.stream.chip0":           "🧠 Coach thinking…",
    "js.stream.chip1":           "📊 Pulling 1Hz slices…",
    "js.stream.chip2":           "🔍 Cross-checking context + workout plan…",
    "js.stream.chip3":           "💭 Choosing words…",
    "js.stream.chip4":           "✍️ Drafting recommendations…",

    # ── Activity tag taxonomy (P2). Keys mirror user_config.ACTIVITY_TAG_KEYS.
    "tag.empty":              "— Untagged —",
    "tag.aerobic_recovery":   "Aerobic Recovery",
    "tag.aerobic_base":       "Aerobic Base",
    "tag.long_run":           "Long Run",
    "tag.tempo":              "Tempo",
    "tag.threshold":          "Threshold",
    "tag.intervals":          "Intervals",
    "tag.hill":               "Hill Reps",
    "tag.trail":              "Trail",
    "tag.race":               "Race",
    "tag.other":              "Other",

    # ── Plotly chart axes / traces / map markers (P3 batch 5).
    "chart.hr_bpm":           "HR (bpm)",
    "chart.pace_min_km":      "Pace (min/km)",
    "chart.speed_kmh":        "Speed (km/h)",
    "chart.elev_m":           "Elevation (m)",
    "chart.cadence_spm":      "Cadence (spm)",
    "chart.gct_ms":           "GCT (ms)",
    "chart.duration_min":     "Duration (min)",
    "chart.route":            "Route",
    "chart.start":            "Start",
    "chart.end":              "End",

    # ── Stats grid labels (key stats card) (P3 batch 5).
    "stats.distance":         "Distance",
    "stats.duration":         "Duration",
    "stats.avg_pace":         "Avg pace",
    "stats.avg_speed":        "Avg speed",
    "stats.hr":               "HR",
    "stats.elev_gain":        "Elev gain",
    "stats.calories":         "Calories",
    "stats.training_load":    "Training load",
    "stats.te":               "TE",
    "stats.vo2max":           "VO₂Max",
    "stats.avg_cadence":      "Avg cadence",
    "stats.avg_gct":          "Avg GCT",
    "stats.avg_stride":       "Avg stride",
    "stats.vert_ratio":       "Vert ratio",

    # ── Lap table column headers (laps card) (P3 batch 5).
    "lap.col_index":          "#",
    "lap.col_distance":       "Distance",
    "lap.col_duration":       "Duration",
    "lap.col_pace":           "Pace",
    "lap.col_speed":          "Avg speed",
    "lap.col_hr":             "HR",
    "lap.col_cadence":        "Cadence",
    "lap.col_gct":            "GCT",

    # ── Activity-stats carousel (P3 batch 5).
    "carousel.title":             "🔍 Workout details",
    "carousel.title_swipe":       "🔍 Workout details · swipe ({n} cards)",
    "carousel.no_data":           "(no detailed data available)",
    "carousel.card_stats":        "📊 Activity stats",
    "carousel.card_route":        "🗺️ Map",
    "carousel.card_hr_pace_elev": "📈 HR / Pace / Elevation",
    "carousel.card_dynamics":     "🏃 Running dynamics (cadence / GCT)",
    "carousel.card_hr_zones":     "🎯 HR zones",
    "carousel.card_laps":         "📋 Laps",

    # ── Training Effect (TE) labels — keys mirror garmin_data.TE_LABEL_MAP (P3 batch 5).
    "te_label.AEROBIC_BASE":      "Aerobic base",
    "te_label.AEROBIC_CAPACITY":  "Aerobic capacity",
    "te_label.LACTATE_THRESHOLD": "Lactate threshold",
    "te_label.SPEED":             "Speed",
    "te_label.ANAEROBIC":         "Anaerobic",
    "te_label.RECOVERY":          "Recovery",

    # ── Prompt-injection blocks (P5: locale-aware coach_sys / review_chat_sys).
    # Headers wrapping personal_note + coach_insights when assembled into LLM
    # system prompts. Output language follows the request locale.
    "prompt.personal_note_header":          "[About the runner — current status / background]",
    "prompt.long_term_insights_header":     "[Long-term memory — key insights pinned by the runner]",
    "prompt.race_context.race_prep_with_race": "currently in race-prep phase, target race: {name} ({days} days from now)",
    "prompt.race_context.race_prep":        "currently in race-prep phase",
    "prompt.race_context.daily":            "currently in daily training phase",

    # tag_instruction / comment_instruction injected into typed report prompts.
    "prompt.tag_instruction.tagged":        "The user has explicitly tagged this session as \"{tag}\"; treat that as ground truth, no need to infer the workout type.",
    "prompt.tag_instruction.untagged":      "The user did not tag the workout type. Infer it from splitType in the lap details: INTERVAL_ACTIVE = intervals; only RWD_RUN = continuous run; then read pace / HR distribution to judge intensity.",
    "prompt.comment_instruction.has_comment": "The user provided a workout plan / note. In your analysis, compare actual execution against the plan (did the runner hit target pace / intensity / structure).",
    "prompt.comment_instruction.no_comment":  "The user did not provide a workout plan.",

    # date_background section emitted by coach_helpers._build_date_background
    "date_bg.header":                       "## Training background (anchored to {date}, {days} days ago)",
    "date_bg.note":                         "Note: all data below corresponds to around {date}, not today.",
    "date_bg.surrounding_header":           "\nSurrounding activities:",
    "date_bg.rel_before":                   "{n} days before",
    "date_bg.rel_after":                    "{n} days after",
    "date_bg.avg_pace":                     "avg pace {pace}",
    "date_bg.avg_hr":                       "avg HR {hr} bpm",
    "date_bg.aerobic_te":                   "Aerobic TE {te}/5",
    "date_bg.surrounding_line":             "- {date} ({rel}) {typ}: {stats}",

    # Follow-up suggestion block appended to every typed-report user message.
    "prompt.follow_ups_instruction": """

---

**Follow-up suggestions (mandatory, place at the very end of your reply):**

```
<follow_ups>
["First follow-up question (≤10 words)", "Second follow-up", "Third follow-up"]
</follow_ups>
```

Based on the specific content of this reply, recommend **3 of the most worthwhile, specific, non-overlapping** follow-up questions for a self-coaching runner. Each ≤10 words, **a single click sends the question**, so write them as questions. Prioritize data anomalies, improvement opportunities, and comparisons worth digging into that were mentioned in your reply. **Forbidden**: vague questions like "how to improve" or "what to do next" — every question must hook into specific numbers from this activity.
""",

    # build_coaching_context section headers + per-line labels.
    "coach_ctx.header":                  "# Garmin training data ({date})\n",
    "coach_ctx.recent_activities_header": "\n## Recent {days}-day activities ({n} sessions)",
    "coach_ctx.avg_pace":                "avg pace {pace}",
    "coach_ctx.avg_speed":               "avg speed {kmh:.1f} km/h",
    "coach_ctx.hr_avg_max":              "HR {avg:.0f}/{max:.0f} bpm",
    "coach_ctx.te_no_label":             "TE {te:.1f}",
    "coach_ctx.te_with_label":           "TE {te:.1f} ({label})",
    "coach_ctx.user_tag":                "  ⚑ User tag: [{label}]",
    "coach_ctx.user_comment":            "  📋 Workout/note: {comment}",
    "coach_ctx.cadence":                 "cadence {c:.0f} spm",
    "coach_ctx.gct":                     "GCT {gct:.0f} ms",
    "coach_ctx.stride":                  "stride {s:.0f} cm",
    "coach_ctx.vert_osc":                "vert osc {v:.1f} cm",
    "coach_ctx.normalized_power":        "NP {np:.0f} W",
    "coach_ctx.longterm_header":         "\n## 6-month training trend (weekly granularity, {n} weeks)",
    "coach_ctx.longterm_table":          "| Week | Run km | Ride km | Sessions | Weekly load |\n|------|--------|---------|----------|-------------|",

    # _format_laps_ctx / _format_splits_ctx
    "lap_ctx.laps_prefix":     "  Laps ({n}): ",
    "lap_ctx.warmup":          "warmup {km:.1f}km {sp}",
    "lap_ctx.main":            "main{label} {sp}",
    "lap_ctx.recovery":        "recovery {sp}",
    "lap_ctx.cooldown":        "cooldown {km:.1f}km {sp}",
    "lap_ctx.splits_prefix":   "  Splits: ",

    # Starter-chip system (overall coach chat home-page suggestions).
    "starter.context_header":             "# Today's date: {date}\n",
    "starter.recent_activities_header":   "## Last 7 days of activities",
    "starter.empty":                      "(none)",
    "starter.no_data":                    "(no data)",
    "starter.untagged":                   "untagged",
    "starter.activity_line":              "- {date} [{tag}] {name} | {km:.1f}km / {mins:.0f}min / @{pace} / HR {hr}",
    "starter.activity_comment":           "  > Note: {comment}",
    "starter.weekly_header":              "\n## Last week's training volume",
    "starter.weekly_line":                "- {week}: run {run_km:.1f}km / ride {ride_km:.1f}km / {acts} sessions / weekly load {load:.0f}",
    "starter.upcoming_races_header":      "\n## Upcoming races (within 12 weeks)",
    "starter.race_line":                  "- {date} {name} ({km}km, {terrain}) — {days} days remaining",
    "starter.pinned_insights_header":     "\n## Pinned insights",
    "starter.system_prompt": """You are the personal coach for a self-coached runner. The user just opened your chat window without saying anything.
You've just reviewed their recent training (see the user message below); give them **3 of the most worthwhile conversation-starter questions** as clickable chips.

**Mandatory rules:**

1. Each ≤10 English words
2. Must be grounded — reference real dates / activity names / race names / mileage / tag
3. Three chips cover three different angles (one each):
   - **(A) Drill into a specific activity**: reference the most recent informative run (use date or activity name)
   - **(B) Trend / comparison**: week-over-week, or same-tag cross-activity comparison
   - **(C) Forward planning**: race-aware if a race exists (reference race name + days remaining); otherwise plan next week
4. No vague questions ("how have I been doing" / "how should I train" / "how to improve") — every chip must hook into a specific date or number
5. Phrased as questions, single-click sends them (not declarative)

**Output a JSON array only**, no other text / markdown / explanation:

["Question A", "Question B", "Question C"]
""",
    "starter.fallback_chip_1":            "How was the most recent run?",
    "starter.fallback_chip_2":            "Assess this week's training volume",
    "starter.fallback_chip_3":            "How should next week look?",

    # Overall coach chat sys_prompt assembly.
    "overall_sys.user_data_header":       "\n\n[User Garmin data]\n",
    "overall_sys.coach_analysis_empty":   "\n\n[Coaching analysis]\n(none)",
    "overall_sys.length_cap":             "\n\n[Hard reply-length constraint]\n- A single reply is strictly **≤ 400 English words** (including punctuation).\n- Priority: conclusion → key numbers (HR / pace / distance / TE) → coach's read → 1 actionable recommendation.\n- When you hit the cap, drop: examples, padding, \"why this is\" expansions, repeating numbers already shown in baked context.\n- Want more detail? Let the user follow up; this is a chat, not a one-shot long-form report.",
    "overall_sys.activity_data_header":   "\n\n[Full session data for this activity]\n",
    "overall_sys.training_background_header": "\n\n[Training background]\n",
    "overall_sys.prior_summary_header":    "\n\n[Prior conversation summary]\n",

    # Time-awareness block (appended to system prompt + history annotation).
    "time_awareness.now_header":           "\n\n[Current time]\n",
    "time_awareness.now_format":           "{date} ({weekday}) {time} {tz}",
    "time_awareness.weekday_0":            "Monday",
    "time_awareness.weekday_1":            "Tuesday",
    "time_awareness.weekday_2":            "Wednesday",
    "time_awareness.weekday_3":            "Thursday",
    "time_awareness.weekday_4":            "Friday",
    "time_awareness.weekday_5":            "Saturday",
    "time_awareness.weekday_6":            "Sunday",

    # chat_helpers.summarize_chunk (LLM-context labels)
    "chat_summary.prior_summary_label":    "Existing summary (earlier portion): ",
    "chat_summary.chunk_label":            "Conversation chunk:",

    # UI: starter chips loading placeholder + LLM streaming errors
    "ui.starter_chips_loading":            "🏃 Coach is reviewing your recent training, conversation starters coming…",
    "ui.activity_not_found":               "Activity not found",
    "ui.saved_indicator":                  "✓ Saved",
    "ui.llm_call_failed":                  "\n\n❌ LLM call failed: {e}",

    # UI: tool-call status badges (shown in chat while LLM calls a tool)
    "ui.tool_badge.default_channel":       "default channels",
    "ui.tool_badge.window_time":           "{start}–{end} · {channels}",
    "ui.tool_badge.stats_window":          "stats · {start}–{end}s",
    "ui.tool_badge.recent_default":        "recent",
    "ui.tool_badge.metric_window":         "{metric} · last {days}d",

    # UI: sync progress messages (Garmin data fetch)
    "ui.sync.starting":                    "Starting…",
    "ui.sync.completed":                   "Done ✓",
    "ui.sync.user_info":                   "Fetching user info…",
    "ui.sync.hrv":                         "Fetching HRV data…",
    "ui.sync.training_status":             "Fetching training status…",
    "ui.sync.activities_list":             "Fetching activity list ({start} → {end})…",
    "ui.sync.daily_summary":               "Daily summary {date}…",
    "ui.sync.sleep":                       "Sleep {date}…",
    "ui.sync.longterm_activities":         "Fetching {weeks}-week activity list…",
    "ui.sync.longterm_hrv":                "Fetching 6-month HRV…",
    "ui.sync.longterm_sleep":              "Fetching 6-month sleep scores…",

    # UI: Garmin auth flow error messages
    "ui.auth.login_failed":                "Login failed; check email / password (page: {page})",
    "ui.auth.mfa_timeout":                 "MFA timeout (no code received within 2 minutes)",
    "ui.auth.mfa_no_ticket":               "After MFA submission, no ticket received — please retry",
    "ui.auth.no_cas_ticket":               "CAS ticket not found in callback URL",
}
