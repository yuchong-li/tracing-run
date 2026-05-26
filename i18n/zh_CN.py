"""zh-CN string catalog. Keys are dotted (namespace.key)."""

STRINGS: dict[str, str] = {
    # ── Language picker (used in P1 LoginPanel + future settings) ─────────────
    "auth.lang.zh":       "简体中文",
    "auth.lang.en":       "English",
    "auth.lang.label":    "语言 / Language",

    # ── Lock screen (PasswordMiddleware /lock) ────────────────────────────────
    "lock.title":          "🔐 锁屏",
    "lock.brand":          "tracing.run",
    "lock.prompt":         "请输入密码",
    "lock.password_ph":    "密码",
    "lock.submit":         "进入",
    "lock.wrong":          "密码错误",

    # ── Garmin connect / login ────────────────────────────────────────────────
    "login.title":         "连接 Garmin",
    "login.subtitle":      "使用你的 Garmin Connect 邮箱密码登录。MFA（如有）会在下一步弹出。",
    "login.email_label":   "邮箱",
    "login.password_label": "密码",
    "login.submit":        "登录",
    "login.connecting":    "登录中…",
    "login.opening":       "登录中… (正在打开 Garmin)",
    "login.success":       "登录成功，跳转中…",
    "login.failed":        "登录失败",
    "login.retry":         "重试",
    "login.mfa_submitted": "MFA 已提交，验证中…",
    "login.mfa_prompt":    "Garmin 要求 MFA 验证码（看你邮箱）",
    "login.mfa_ph":        "MFA 6 位码",
    "login.mfa_submit":    "提交",
    "login.idle":          "（idle）",

    # ── Empty data panel (first-time after login, before sync) ────────────────
    "empty.title":         "尚无活动数据",
    "empty.subtitle":      "点击下方进行第一次同步（90 天范围）。约 10–30 秒。",
    "empty.full_sync":     "🔄 全量同步",

    # ── Reconnect dialog (session expired) ────────────────────────────────────
    "reconnect.title":     "⚠ Garmin 登录已过期",
    "reconnect.body":      "授权 token 过期，需要重新登录获取新 token。",
    "reconnect.hint_lead": "点「重连 Garmin」",
    "reconnect.hint_body": "只会刷新授权 —— 所有 Garmin 数据 / AI 对话历史 / races / 备注 / 长期记忆 ",
    "reconnect.hint_kept": "全部保留",
    "reconnect.hint_tail": "。",
    "reconnect.dismiss":   "稍后再说",
    "reconnect.cta":       "重连 Garmin",

    # ── iOS "Add to Home Screen" hint overlay ─────────────────────────────────
    "install.title":       "装到主屏幕，体验更接近 app",
    "install.subtitle":    "独立窗口，无浏览器边框，从主屏一点直达。",
    "install.step1_a":     "点 Safari 底部的 ",
    "install.step1_b":     " 分享按钮",
    "install.step2_a":     "下拉，选 ",
    "install.step2_pill":  "「添加到主屏幕」",
    "install.step3_a":     "右上角点 ",
    "install.step3_pill":  "「Add」",
    "install.dismiss":     "以后再说",
    "install.ack":         "我知道了",

    # ── Mobile header (P3 batch 2) ────────────────────────────────────────────
    "mobile.add_to_home":  "📱 添加到桌面",
    "mobile.dismiss_hint": "不再提示",

    # ── Sidebar (P3 batch 2) ──────────────────────────────────────────────────
    "sidebar.no_data":          "尚未同步数据",
    "sidebar.sync_incremental": "🔄 同步 Garmin",
    "sidebar.settings":         "⚙️ 设置",
    "sidebar.last_sync":        "上次同步：{when}",
    "sidebar.coach_chat":       "对话教练",
    "sidebar.coach_chat_sub":   "跨活动 / 趋势 / 规划",
    "sidebar.recent":           "复盘训练",
    "sidebar.day.today":        "今天",
    "sidebar.day.yesterday":    "昨天",
    "sidebar.day.last_7":       "最近 7 天",
    "sidebar.day.last_30":      "最近 30 天",
    "sidebar.day.older":        "更早",

    # ── Greeting buckets (5-22 / 12-18 / 18-22 / else) ────────────────────────
    "greeting.morning":   "早上好",
    "greeting.afternoon": "下午好",
    "greeting.evening":   "晚上好",
    "greeting.night":     "夜深了",
    # Comma between greeting and name. Locale-specific punctuation (zh full-
    # width 「，」 vs en ", ").
    "greeting.sep":       "，",

    # ── Race countdown (sidebar header) ───────────────────────────────────────
    "race.days_left":         "还有 {days} 天",
    "race.no_target":         "暂无目标赛事",
    "phase.race_prep_short":  "备赛",
    "phase.recovery_short":   "恢复期",
    "phase.recovery_hint":    "降负荷优先",

    # ── _fmt_relative (sidebar last-sync timestamp) ───────────────────────────
    "rel.dash":         "—",
    "rel.just_now":     "刚刚",
    "rel.minutes_ago":  "{n} 分钟前",
    "rel.hours_ago":    "{n} 小时前",
    "rel.days_ago":     "{n} 天前",
    # Date format strftime() pattern for older timestamps. Locale-specific.
    "rel.older_fmt":    "%m月%d日",

    # ── Activity-row date format (sidebar list) ───────────────────────────────
    "act.row_date_fmt": "%m月%d日 %H:%M",

    # ── Sync state (toast + status polling) ───────────────────────────────────
    "sync.in_progress": "⏳ 同步中…",
    "sync.in_progress_pct": "⏳ {msg} ({pct}%)",
    "sync.done_msg":    "完成 ✓",
    "sync.done_toast":  "✓ 同步完成",
    "sync.error_pfx":   "❌ {msg}",
    "sync.session_expired_pfx": "Garmin 登录过期：{e}",

    # ── Activity-type display map (used by sidebar + LLM context) ─────────────
    "activity_type.running":             "跑步",
    "activity_type.trail_running":       "越野跑",
    "activity_type.virtual_ride":        "Zwift骑行",
    "activity_type.cycling":             "骑行",
    "activity_type.indoor_cycling":      "室内骑行",
    "activity_type.swimming":            "游泳",
    "activity_type.open_water_swimming": "开放水域游泳",
    "activity_type.strength_training":   "力量训练",
    "activity_type.fitness_equipment":   "器械训练",
    "activity_type.walking":             "步行",
    "activity_type.hiking":              "徒步",
    "activity_type._unknown":            "运动",

    # ── Settings page (P3 batch 3) ────────────────────────────────────────────
    "settings.title":            "设置",

    "settings.lang.heading":     "🌐 语言",
    "settings.lang.help":        "切换整个界面以及教练回复的语言，选择后立即生效。",

    "settings.phase.heading":    "📅 训练阶段",
    "settings.phase.help":       "决定教练给建议时的 framing — 备赛期偏强度规划，恢复期偏负荷管理。",
    "phase.base":                "base — 打底",
    "phase.build":               "build — 提强度",
    "phase.race_prep":           "race_prep — 备赛",
    "phase.recovery":            "recovery — 恢复",
    "phase.maintenance":         "maintenance — 维持",

    "settings.race.heading":     "🏁 比赛计划",
    "settings.race.empty":       "尚无比赛",
    "settings.race.add_label":   "➕ 添加比赛",
    "settings.race.name_ph":     "比赛名（必填，例：墨尔本马拉松 2026）",
    "settings.race.name_edit_ph": "比赛名",
    "settings.race.field_date":  "日期",
    "settings.race.field_dist":  "距离",
    "settings.race.field_terrain": "路面",
    "settings.race.field_goal":  "目标用时",
    "settings.race.dist_5k":     "5 km (5K)",
    "settings.race.dist_10k":    "10 km (10K)",
    "settings.race.dist_half":   "21.1 km (半马)",
    "settings.race.dist_full":   "42.2 km (全马)",
    "settings.race.dist_other":  "Other (手动输入)",
    "settings.race.dist_picker_placeholder": "— 选择距离 —",
    "settings.race.dist_other_ph": "自定义距离 (km)",
    "settings.race.terrain_road": "公路",
    "settings.race.terrain_trail": "越野",
    "settings.race.terrain_picker_placeholder": "— 选择路面 —",
    "settings.race.goal_hours":  "时",
    "settings.race.goal_minutes": "分",
    "settings.race.add_btn":     "添加比赛",
    "settings.race.save":        "保存",
    "settings.race.cancel":      "取消",
    "settings.race.edit":        "编辑",
    "settings.race.delete":      "删除",
    "settings.race.delete_confirm": "删除这场比赛？",
    "settings.race.row_dash":    "—",
    "settings.race.row_goal":    "目标 {time}",

    "settings.note.heading":     "👤 关于我（教练会看到）",
    "settings.note.placeholder": ("例：32 岁男 / 178cm / 68kg，跑龄 5 年，"
                                  "半马 PB 1:35，目标 2026 墨马 sub-3:30，"
                                  "左膝 ITBS 病史。\n\n"
                                  "建议覆盖：年龄 / 性别 / 身高 / 体重 / "
                                  "跑龄 / PB / 目标 / 伤病史 / 生活节奏。"
                                  "写得乱也没关系，下面点 ✨ 整理 让 AI 帮你理顺。"),
    "settings.note.save":        "保存",
    "settings.note.distill":     "✨ 整理",

    "settings.insights.heading": "🧠 长期记忆",
    "settings.insights.empty":   "尚无长期记忆。在任何 assistant 回复下方点 📌 加入。",
    "settings.insights.add_label": "➕ 手动添加 insight",
    "settings.insights.placeholder": ("例：我的真实 Z2 上限是 142bpm（比 Garmin 算的 138 高 4bpm，请按 142 判断）。\n"
                                       "或：左膝有 ITBS 病史，长距离后期 cadence 掉到 165 以下要警惕。"),
    "settings.insights.save":    "保存",
    "settings.insights.help":    ("教练每次回答都会自动看到上面这些条目。"
                                  "如果包含具体数值（例：「Z2 上限 142」），教练会以你的"
                                  "数字为准而不是 Garmin 自动算的。"),
    "settings.insights.delete":  "删除",
    "settings.insights.delete_confirm": "删除这条长期记忆？",
    "settings.insights.src_review": "复盘",
    "settings.insights.src_overall": "追问",

    "settings.tour.heading":     "🎓 引导",
    "settings.tour.help":        ("第一次启动时会有 4 步走查（侧栏 / 对话教练 / 复盘 / 设置）。"
                                  "想再看一遍点这里。"),
    "settings.tour.replay":      "🎓 重新走一遍引导",

    "settings.danger.heading":   "⚠ Danger Zone",
    "settings.danger.summary":   ("断开会清除全部活动数据 + 所有 AI 对话历史。"
                                  "races / 备注 / tags / coach insights 不会动。"),
    "settings.danger.disconnect_btn": "断开 Garmin（清除全部数据）",
    "settings.danger.confirm_title": "⚠ 确认要断开 Garmin 吗？",
    "settings.danger.confirm_lead": "断开会 ",
    "settings.danger.confirm_wipe": "清除以下所有内容",
    "settings.danger.confirm_and":  "，且 ",
    "settings.danger.confirm_irreversible": "无法恢复",
    "settings.danger.confirm_colon": "：",
    "settings.danger.wipe_item_data": "所有 Garmin 活动数据（SQLite + 缓存）",
    "settings.danger.wipe_item_chats": "所有 AI 对话历史（每个活动的复盘 chat + 对话教练）",
    "settings.danger.wipe_item_cache": "训练背景缓存（builder 算的 Pa:HR / 心率漂移等）",
    "settings.danger.keep_label": "保留",
    "settings.danger.keep_paren": "（这些不在 cache/ 里）：",
    "settings.danger.keep_item_user": "races / personal_note / coach insights",
    "settings.danger.keep_item_tags": "activity tags / 课表/备注",
    "settings.danger.refresh_a": "如果只是想刷新 Garmin 登录授权，",
    "settings.danger.refresh_dont": "不要点这个",
    "settings.danger.refresh_b": " —— 等 session 过期会有重连提示。",
    "settings.danger.cancel":   "取消",
    "settings.danger.confirm":  "确认断开 + 清除数据",

    "settings.footer.tagline":   "tracing.run · v{version}",
    "settings.footer.credits":   ("感谢这些开源项目让本应用成为可能：FastHTML · htmx · "
                                  "Tailwind CSS · marked.js · plotly · garth · "
                                  "garminconnect · Playwright"),

    # ── Chat panels (overall + activity) ─────────────────────────────────────
    "chat.overall.placeholder":  ("和教练聊聊训练…"
                                  "|跨活动追问…"
                                  "|串起训练里的细节…"
                                  "|跳出来看大盘…"
                                  "|聊聊最近的训练节奏…"),
    "chat.activity.placeholder": ("针对任意片段追问…"
                                  "|钻取任意时段…"
                                  "|回看任意一段…"
                                  "|追问某个 lap 或某段距离…"
                                  "|放大任意细节…"),
    "chat.empty":                "开始对话…",
    "chat.send":                 "发送",
    "chat.model_pfx":            "模型 {model}",
    "chat.clear":                "清空对话",
    "chat.clear_confirm":        "确定清空当前对话？",
    "chat.clear_confirm_full":   "确定清空当前对话？（报告 + 追问都会删）",
    "chat.activity.empty_pre_report": "生成完报告后，在这里追问 / drill 任何一段…",
    "chat.activity.placeholder_locked": "先生成复盘再追问…",
    "chat.activity.placeholder_generating": "报告生成中，稍后追问…",

    # ── Overall chat header ───────────────────────────────────────────────────
    "chat.overall.title":     "对话教练",
    "chat.overall.subtitle":  "跨活动 / 趋势 / 规划。需要单次活动 1Hz 秒级 drill 请到该活动卡片。",

    # ── Activity chat header (comment editor) ─────────────────────────────────
    "activity.comment.summary":     "📋 课表/备注",
    "activity.comment.placeholder": "写下本次课表或事后笔记（教练会看到）",
    "activity.comment.save":        "保存备注",

    # ── Pre-report nudge panel ────────────────────────────────────────────────
    "nudge.title":          "准备好复盘这次训练吗？",
    "nudge.subtitle":       "选好类型，教练用对应的复盘框架来分析。",
    "nudge.tag.required":   "训练类型",
    "nudge.tag.required_hint": " 必选",
    "nudge.tag.help":       "不同类型走不同复盘框架（间歇 / 长距离 / 节奏跑 / 比赛 / 越野…）",
    "nudge.tag.placeholder": "— 选择训练类型 —",
    "nudge.comment.label":  "课表 / 备注",
    "nudge.comment.optional": " 可选",
    "nudge.comment.placeholder": "例：4×1km @4:00, 间歇 90s, WU 2km, CD 1km\n或：今天感觉腿很沉，本来想 60min 改成 40min",
    "nudge.comment.help":   "告诉教练这次本来要做什么 → 评估执行情况会更准。",
    "nudge.cta":            "🔬 开始生成复盘报告",

    # ── Report card (Row 3) ──────────────────────────────────────────────────
    "report.card.title":        "📋 复盘报告",
    "report.pill.running":      "🔵 生成中…",
    "report.pill.stale":        "🟠 已过期",
    "report.pill.done":         "🟢 已生成",
    "report.pill.empty":        "⚪ 尚未生成",
    "report.stale.banner":      "⚠ 你改了标签，这份报告还是旧 builder 生成的。",
    "report.stale.regenerate":  "重新生成",
    "report.chips_text":        "✍️ 教练撰写中",

    # ── SSE status events (server-pushed phase markers) ──────────────────────
    "sse.fetch_first":   "🛰️ 首次访问，从 Garmin 拉取 1Hz 数据 (5–30s)…",
    "sse.fetch_cached":  "🛰️ 读取活动数据…",
    "sse.fetch_failed":  "\n\n❌ 拉取活动数据失败：{e}",
    "sse.build_review":  "🔬 构建专属复盘分析（per-lap、Pa:HR、cardiac drift…）",
    "sse.build_failed":  "\n\n❌ 构建复盘失败：{e}",
    "sse.writing":       "✍️ 教练撰写中…",
    "sse.llm_failed":    "\n\n❌ LLM 调用失败：{e}",
    "sse.internal_error": "\n\n❌ 内部错误：{e}",
    "sse.fetch_detail_failed": "❌ 拉取活动详情失败：{e}",

    # ── Stale-session error panel ─────────────────────────────────────────────
    "stale_session.error":     "❌ Garmin 登录已过期：{err}",

    # ── Pin dialog (📌 add to long-term memory) ──────────────────────────────
    "pin.btn":           "📌 加入长期记忆",
    "pin.dlg.title":     "📌 加入长期记忆",
    "pin.dlg.body":      "挑出这条回复中你想固化的部分。教练在所有未来对话里（主页 + 复盘）都会看到。",
    "pin.dlg.distill":   "✨ 提炼 (≤20字)",
    "pin.dlg.cancel":    "取消",
    "pin.dlg.save":      "保存到长期记忆",

    # ── Inline-JS strings (injected via window.I18N per-request) ─────────────
    "js.loading":                "正在加载…",
    "js.tour.skip":              "跳过",
    "js.tour.next":              "下一步 →",
    "js.tour.done":              "完成 ✓",
    "js.tour.step1.title":       "👋 欢迎来到 tracing.run",
    "js.tour.step1.text":        "基于你 Garmin 1Hz 数据的单次复盘 + 跨活动追问。先快速过一下 4 个核心位置。",
    "js.tour.step2.title":       "🔄 同步",
    "js.tour.step2.text":        "训练结束后点这里把新活动拉下来。首次连接的全量历史同步会自动完成。",
    "js.tour.step3.title":       "💬 对话教练",
    "js.tour.step3.text":        "跨活动的对话入口。例：「我最近一直在进步吗」「上周长距离比这周累在哪」。教练会调用工具查具体数据。",
    "js.tour.step4.title":       "🔬 单次复盘",
    "js.tour.step4.text":        "点侧栏任何活动 → 教练自动用对应 typed builder 分析（长距离 / 间歇 / 节奏跑等不同框架）→ 你可以继续追问 1Hz 秒级细节。",
    "js.tour.step5.title":       "⚙ 设置",
    "js.tour.step5.text":        "训练阶段 / 比赛计划 / 关于我 / 长期记忆 / 断开 Garmin。所有设定都会进教练的 prompt。",
    "js.pin.distilling":         "✨ 提炼中…",
    "js.pin.distilled":          "✓ 已提炼。可以再编辑或直接保存。",
    "js.pin.distill_empty":      "提炼返回空，保留原文。",
    "js.pin.distill_fail":       "提炼失败：{e}",
    "js.pin.saving":             "保存中…",
    "js.pin.saved":              "✓ 已加入长期记忆，关闭…",
    "js.pin.save_fail":          "保存失败：{e}",
    "js.pin.save_btn":           "保存到长期记忆",
    "js.note.empty":             "请先写点东西再整理",
    "js.note.organizing":        "✨ 整理中…",
    "js.note.organized":         "✓ 已整理。检查一下后点保存。",
    "js.note.organize_empty":    "整理返回空，保留原文。",
    "js.note.organize_fail":     "整理失败：{e}",
    "js.report.chip0":           "✍️ 教练撰写中",
    "js.report.chip1":           "🧠 在揉数据",
    "js.report.chip2":           "📊 在对比上次",
    "js.report.chip3":           "🔬 找出关键拐点",
    "js.report.chip4":           "⚡ 拼最后一段",
    "js.report.pill.done":       "🟢 已生成",
    "js.report.pill.fail":       "❌ 失败",
    "js.report.fail_msg":        "生成失败",
    "js.stream.chip0":           "🧠 教练思考中…",
    "js.stream.chip1":           "📊 引用 1Hz 数据切片…",
    "js.stream.chip2":           "🔍 对照训练背景与课表…",
    "js.stream.chip3":           "💭 斟酌教练措辞…",
    "js.stream.chip4":           "✍️ 整理建议中…",
    "js.stream.error":           "连接中断，请重试",

    # ── Activity tag taxonomy (P2). Keys mirror user_config.ACTIVITY_TAG_KEYS.
    "tag.empty":              "— 未标记 —",
    "tag.aerobic_recovery":   "有氧恢复",
    "tag.aerobic_base":       "有氧基础",
    "tag.long_run":           "长距离",
    "tag.tempo":              "节奏跑",
    "tag.threshold":          "阈值跑",
    "tag.intervals":          "间歇训练",
    "tag.hill":               "爬坡训练",
    "tag.trail":              "越野",
    "tag.race":               "比赛",
    "tag.other":              "其他",

    # ── Plotly chart axes / traces / map markers (P3 batch 5).
    "chart.hr_bpm":           "心率 (bpm)",
    "chart.pace_min_km":      "配速 (min/km)",
    "chart.speed_kmh":        "速度 (km/h)",
    "chart.elev_m":           "海拔 (m)",
    "chart.cadence_spm":      "步频 (spm)",
    "chart.gct_ms":           "触地 (ms)",
    "chart.duration_min":     "时长 (min)",
    "chart.route":            "路线",
    "chart.start":            "起点",
    "chart.end":              "终点",

    # ── Stats grid labels (📊 关键数据 card) (P3 batch 5).
    "stats.distance":         "距离",
    "stats.duration":         "时长",
    "stats.avg_pace":         "均配",
    "stats.avg_speed":        "均速",
    "stats.hr":               "心率",
    "stats.elev_gain":        "爬升",
    "stats.calories":         "卡路里",
    "stats.training_load":    "训练负荷",
    "stats.te":               "TE",
    "stats.vo2max":           "VO₂Max",
    "stats.avg_cadence":      "均步频",
    "stats.avg_gct":          "均 GCT",
    "stats.avg_stride":       "均步幅",
    "stats.vert_ratio":       "垂直比",

    # ── Lap table column headers (📋 分段 card) (P3 batch 5).
    "lap.col_index":          "#",
    "lap.col_distance":       "距离",
    "lap.col_duration":       "时长",
    "lap.col_pace":           "配速",
    "lap.col_speed":          "均速",
    "lap.col_hr":             "HR",
    "lap.col_cadence":        "步频",
    "lap.col_gct":            "GCT",

    # ── Activity-stats carousel (🔍 训练数据详情) (P3 batch 5).
    "carousel.title":             "🔍 训练数据详情",
    "carousel.title_swipe":       "🔍 训练数据详情 · 左右滑动 ({n} 张)",
    "carousel.no_data":           "（暂无活动详情数据）",
    "carousel.card_stats":        "📊 活动数据",
    "carousel.card_route":        "🗺️ 地图",
    "carousel.card_hr_pace_elev": "📈 心率 / 配速 / 海拔",
    "carousel.card_dynamics":     "🏃 跑步动态 (步频 / 触地)",
    "carousel.card_hr_zones":     "🎯 心率区间",
    "carousel.card_laps":         "📋 分段",

    # ── Training Effect (TE) labels — keys mirror garmin_data.TE_LABEL_MAP (P3 batch 5).
    "te_label.AEROBIC_BASE":      "有氧基础",
    "te_label.AEROBIC_CAPACITY":  "有氧能力",
    "te_label.LACTATE_THRESHOLD": "乳酸阈值",
    "te_label.SPEED":             "速度",
    "te_label.ANAEROBIC":         "无氧",
    "te_label.RECOVERY":          "恢复训练",

    # ── Prompt-injection blocks (P5: locale-aware coach_sys / review_chat_sys).
    # Headers wrapping personal_note + coach_insights when assembled into LLM
    # system prompts. Output language follows the request locale.
    "prompt.personal_note_header":          "【关于用户的近况/背景】",
    "prompt.long_term_insights_header":     "【长期记忆 — 用户已固化的关键 insight】",
    "prompt.race_context.race_prep_with_race": "当前处于备赛阶段，目标赛事：{name}（距今 {days} 天）",
    "prompt.race_context.race_prep":        "当前处于备赛阶段",
    "prompt.race_context.daily":            "当前处于日常训练阶段",

    # tag_instruction / comment_instruction injected into typed report prompts.
    "prompt.tag_instruction.tagged":        "用户已明确标注此次训练为「{tag}」，以此为准，无需再推断训练类型。",
    "prompt.tag_instruction.untagged":      "用户未标注训练类型，请通过分段详情中的 splitType 自行判断：INTERVAL_ACTIVE=间歇训练；只有 RWD_RUN=连续跑，再看配速/心率分布判断强度。",
    "prompt.comment_instruction.has_comment": "用户提供了课表/备注，请在分析中对照课表评估实际执行情况（是否完成目标配速/强度/结构）。",
    "prompt.comment_instruction.no_comment":  "用户未提供课表。",

    # date_background section emitted by coach_helpers._build_date_background
    "date_bg.header":                       "## 训练背景（以 {date} 为基准，距今 {days} 天）",
    "date_bg.note":                         "注意：以下所有数据均对应 {date} 前后，不是今天。",
    "date_bg.surrounding_header":           "\n前后活动：",
    "date_bg.rel_before":                   "当天前 {n}天",
    "date_bg.rel_after":                    "当天后 {n}天",
    "date_bg.avg_pace":                     "均配 {pace}",
    "date_bg.avg_hr":                       "均心率 {hr}bpm",
    "date_bg.aerobic_te":                   "有氧效果 {te}/5",
    "date_bg.surrounding_line":             "- {date}（{rel}） {typ}：{stats}",

    # Follow-up suggestion block appended to every typed-report user message.
    "prompt.follow_ups_instruction": """

---

**追问建议（强制要求，放在你回复的最末尾）：**

```
<follow_ups>
["第 1 个追问 (≤15字)", "第 2 个追问", "第 3 个追问"]
</follow_ups>
```

基于本次回复的具体内容，给 self-coaching 跑者推荐 **3 个最值得追问的、具体的、不重复**的问题。每条 ≤15 字，**用户点一下就直接发问**，所以要写成提问语气。优先针对你回复中提到的数据异常、可改进点、值得深挖的对比。**禁止**生成空泛的"如何改进"、"接下来怎么练"这种问题；必须扣本次活动的具体数字。
""",

    # build_coaching_context section headers + per-line labels (LLM context).
    "coach_ctx.header":                  "# Garmin 运动数据（{date}）\n",
    "coach_ctx.recent_activities_header": "\n## 最近 {days} 天活动（共 {n} 次）",
    "coach_ctx.avg_pace":                "均配 {pace}",
    "coach_ctx.avg_speed":               "均速 {kmh:.1f}km/h",
    "coach_ctx.hr_avg_max":              "心率 {avg:.0f}/{max:.0f}bpm",
    "coach_ctx.te_no_label":             "TE {te:.1f}",
    "coach_ctx.te_with_label":           "TE {te:.1f}（{label}）",
    "coach_ctx.user_tag":                "  ⚑ 用户手动标注: 【{label}】",
    "coach_ctx.user_comment":            "  📋 课表/备注: {comment}",
    "coach_ctx.cadence":                 "步频{c:.0f}spm",
    "coach_ctx.gct":                     "GCT{gct:.0f}ms",
    "coach_ctx.stride":                  "步幅{s:.0f}cm",
    "coach_ctx.vert_osc":                "垂振{v:.1f}cm",
    "coach_ctx.normalized_power":        "NP{np:.0f}W",
    "coach_ctx.longterm_header":         "\n## 6 个月训练趋势（周粒度，共 {n} 周）",
    "coach_ctx.longterm_table":          "| 周 | 跑步km | 骑行km | 次数 | 周负荷 |\n|-----|--------|--------|------|--------|",

    # _format_laps_ctx / _format_splits_ctx
    "lap_ctx.laps_prefix":     "  分段({n} laps): ",
    "lap_ctx.warmup":          "热身{km:.1f}km {sp}",
    "lap_ctx.main":            "主段{label} {sp}",
    "lap_ctx.recovery":        "恢复 {sp}",
    "lap_ctx.cooldown":        "放松{km:.1f}km {sp}",
    "lap_ctx.splits_prefix":   "  分段: ",

    # Starter-chip system (overall coach chat home-page suggestions).
    "starter.context_header":             "# 今天日期：{date}\n",
    "starter.recent_activities_header":   "## 最近 7 天活动",
    "starter.empty":                      "（无）",
    "starter.no_data":                    "（无数据）",
    "starter.untagged":                   "未标记",
    "starter.activity_line":              "- {date} [{tag}] {name} | {km:.1f}km / {mins:.0f}分 / @{pace} / HR {hr}",
    "starter.activity_comment":           "  > 备注：{comment}",
    "starter.weekly_header":              "\n## 上周训练总量",
    "starter.weekly_line":                "- {week}: 跑 {run_km:.1f}km / 骑 {ride_km:.1f}km / 共 {acts} 次活动 / 周 load {load:.0f}",
    "starter.upcoming_races_header":      "\n## 近期比赛（12 周内）",
    "starter.race_line":                  "- {date} {name} ({km}km, {terrain}) — 还剩 {days} 天",
    "starter.pinned_insights_header":     "\n## 长期备忘（pinned insights）",
    "starter.system_prompt": """你是一位 self-coached 跑者的私人教练。用户刚打开你的对话窗口，还没说话。
你刚浏览完他最近的训练记录（见下方 user message），要给他 **3 个最值得开聊的问题** 作为可点击的 chip。

**强制规则：**

1. 每条 ≤15 中文字符
2. 必须 grounded —— 引用真实日期 / 活动名 / 比赛名 / 公里数 / tag
3. 三条覆盖三个不同方向（各占一条）：
   - **(A) 钻特定活动**：引用最近一次最有信息量的具体 run（用日期或活动名）
   - **(B) 趋势 / 对比**：week-over-week，或同 tag 跨活动对比
   - **(C) 前瞻规划**：有比赛就 race-aware（引用比赛名 + 剩余天数），无比赛就规划下周
4. 禁止空泛（"最近怎么样" / "该怎么练" / "如何提升"）—— 每条必须扣具体日期或数字
5. 问句口吻，用户点一下就直接发问（不要陈述句）

**仅输出 JSON 数组**，无任何其它文字 / markdown / 解释：

["问题 A", "问题 B", "问题 C"]
""",
    "starter.fallback_chip_1":            "最近一次跑得怎么样",
    "starter.fallback_chip_2":            "本周训练量评估一下",
    "starter.fallback_chip_3":            "下周该怎么安排",

    # Overall coach chat sys_prompt assembly.
    "overall_sys.user_data_header":       "\n\n【用户 Garmin 数据】\n",
    "overall_sys.coach_analysis_empty":   "\n\n【教练分析】\n（无）",
    "overall_sys.length_cap":             "\n\n【回复长度硬约束】\n- 单次回复严格 **≤ 650 中文字符**（含标点）。\n- 优先级：结论 → 关键数字（HR / 配速 / 距离 / TE）→ 教练判断 → 1 条行动建议。\n- 触及上限就砍：举例、铺垫、「为什么这样」的展开、重复 baked context 已展示的数字。\n- 想给更多细节就让用户追问；本端是 chat，不是一次性长篇报告。",
    "overall_sys.activity_data_header":   "\n\n【本次训练完整数据】\n",
    "overall_sys.training_background_header": "\n\n【训练背景】\n",
    "overall_sys.prior_summary_header":    "\n\n【之前对话摘要】\n",

    # 时间感知模块（追加到 system prompt + 历史消息时间戳）。
    "time_awareness.now_header":           "\n\n【当前时间】\n",
    "time_awareness.now_format":           "{date}（{weekday}）{time} {tz}",
    "time_awareness.weekday_0":            "周一",
    "time_awareness.weekday_1":            "周二",
    "time_awareness.weekday_2":            "周三",
    "time_awareness.weekday_3":            "周四",
    "time_awareness.weekday_4":            "周五",
    "time_awareness.weekday_5":            "周六",
    "time_awareness.weekday_6":            "周日",

    # Web 搜索工具的使用指引（启用时追加到聊天 system prompt）。
    "web_search.guidance":                 "\n\n【Web 搜索工具】\n你有 `web_search(query)` 工具可用。\n使用场景：训练方法学问题（如「30 岁男性 Zone 2 心率多少合适」）、横向对比（如「我这 VO2max 在同年龄段算什么水平」）、比赛结果、装备、伤病康复方案、你不一定知道的近期研究。\n不要使用：用户自己的数据已经在 context 中能回答的问题、闲聊或与跑步无关的泛话题、对话里已经讨论清楚的事实。\n使用搜索结果时，请用 URL 标注来源。",

    # 缩写解释规则 —— 追加到聊天 system prompt，让 LLM 在面向用户的正文里第一次出现时展开。
    "abbreviations.glossary":              "\n\n【缩写解释规则】\n在面向用户的正文里第一次出现以下缩写时，请先展开（例：「垂直振幅比（VR）8.2%」），之后再继续用缩写：\n- VR = Vertical Ratio（垂直振幅比）\n- CV = Coefficient of Variation（变异系数，配速稳定性 / 锯齿指标）\n- GCT = Ground Contact Time（着地时间）\n- EF = Efficiency Factor（效率因子，配速 ÷ HR）\n- TE = Training Effect（训练效益，Garmin 的有氧 / 无氧负荷得分）\n- Pa:HR = Pace-to-HR ratio（脱节率）\n- GAP = Grade-Adjusted Pace（坡度调整配速）\nContext 里的数据表格可以保留缩写——只在用户能看到的正文里展开。",

    # chat_helpers.summarize_chunk (LLM-context labels)
    "chat_summary.prior_summary_label":    "已有摘要（之前部分）：",
    "chat_summary.chunk_label":            "对话片段：",

    # UI: starter chips loading placeholder + LLM streaming errors
    "ui.starter_chips_loading":            "🏃 教练正在翻你最近的训练记录，马上给你几个开聊方向…",
    "ui.activity_not_found":               "活动不存在",
    "ui.saved_indicator":                  "✓ 已保存",
    "ui.llm_call_failed":                  "\n\n❌ LLM 调用失败：{e}",

    # UI: tool-call status badges (shown in chat while LLM calls a tool)
    "ui.tool_badge.default_channel":       "默认通道",
    "ui.tool_badge.window_time":           "第 {start}–{end} · {channels}",
    "ui.tool_badge.stats_window":          "stats · 第 {start}–{end}s",
    "ui.tool_badge.recent_default":        "近期",
    "ui.tool_badge.metric_window":         "{metric} · 近 {days}d",

    # UI: sync progress messages (Garmin data fetch)
    "ui.sync.starting":                    "启动中…",
    "ui.sync.completed":                   "完成 ✓",
    "ui.sync.user_info":                   "获取用户信息…",
    "ui.sync.hrv":                         "拉取 HRV 数据…",
    "ui.sync.training_status":             "拉取训练状态…",
    "ui.sync.activities_list":             "拉取活动列表 ({start} → {end})…",
    "ui.sync.daily_summary":               "日摘要 {date}…",
    "ui.sync.sleep":                       "睡眠 {date}…",
    "ui.sync.longterm_activities":         "拉取 {weeks} 周活动列表…",
    "ui.sync.longterm_hrv":                "拉取 6 个月 HRV…",
    "ui.sync.longterm_sleep":              "拉取 6 个月睡眠评分…",

    # UI: Garmin auth flow error messages
    "ui.auth.login_failed":                "登录失败，请检查邮箱/密码（页面：{page}）",
    "ui.auth.mfa_timeout":                 "MFA 超时（2分钟内未收到验证码）",
    "ui.auth.mfa_no_ticket":               "MFA 提交后未收到 ticket，请重试",
    "ui.auth.no_cas_ticket":               "未在回调 URL 中找到 CAS ticket",
}
