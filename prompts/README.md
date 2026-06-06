# Prompts

LLM prompts live here as plain `.md` files, organised by locale:

```text
prompts/
├── en/        # English prompts
└── zh-cn/     # Simplified Chinese prompts
```

`ui/llm.py:load_prompt(name, lang=None)` reads `prompts/<lang>/<name>.md`
on demand (no caching, no restart needed when iterating on prompts). The
active locale is taken from `i18n.current_locale()` unless overridden.

## Files

| File | Type | Used by |
| --- | --- | --- |
| `coach_system.md` | system | overall coach chat |
| `review_chat_system.md` | system | per-activity review chat (base voice; typed addendums extracted from the report prompts) |
| `chat_summary.md` | system | rolling-summary compression of older messages |
| `insight_distill.md` | system | one-shot compression of pin-popover text → ≤20-字 insight (✨ 提炼 button) |
| `personal_note_refine.md` | system | one-shot refinement of the user's personal note |
| `review_report.md` | user | per-activity review for the untagged / fallback case |
| `review_report_aerobic.md` | user | typed-builder review prompt for the Aerobic tag (merged recovery+base) |
| `review_report_steady.md` | user | …Steady (High-Z2→mid/high-Z3 cruise; reuses AerobicBuilder, reframed reading) |
| `review_report_aerobic_recovery.md` | user | orphaned after the aerobic merge — no tag maps here; kept for now |
| `review_report_tempo.md` | user | …Tempo |
| `review_report_threshold.md` | user | …Threshold |
| `review_report_intervals.md` | user | …Intervals |
| `review_report_long_run.md` | user | …Long Run |
| `review_report_hill.md` | user | …Hill |
| `review_report_trail.md` | user | …Trail |
| `review_report_race.md` | user | …Race |

## Conventions

- Placeholders are Python `str.format()` style (`{name}`). Literal `{` or `}`
  in prompt text must be doubled (`{{`, `}}`).
- Builders emit neutral-English context blocks; the prompt drives the
  output language for the user-facing response.
- For "structural" prompts that require a specific output shape (e.g.
  `review_report_long_run.md`), keep the example template inside the same
  file — the LLM mirrors the structure shown.
- Each typed report prompt may include a
  `<!-- chat-addendum-start --> … <!-- chat-addendum-end -->` block which
  the chat code extracts and appends to `review_chat_system.md` so
  follow-up turns retain the typed voice.

## Adding a locale

1. Create `prompts/<new-locale>/` and copy a starter set of `.md` files
   (translate as needed).
2. Add `i18n/<new_locale>.py` with the matching string catalog.
3. The loader will pick up the new directory automatically; the language
   switcher in `⚙️ Settings` will list it once `i18n` registers it.
