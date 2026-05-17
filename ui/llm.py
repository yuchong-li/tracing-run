"""LLM client + prompt loader. Used by every page that talks to the model."""

import json
import os
from datetime import date

import requests

import i18n
import user_config as uc

LLM_BASE      = os.environ.get("LLM_BASE",      "http://localhost:4000/v1")
LLM_API_KEY   = os.environ.get("LLM_API_KEY",   "local-proxy")
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "gpt-5.4")

# Models that think/reason internally before producing output.
# max_tokens caps the OUTPUT only — they still need budget for the thinking phase.
# Maintain this set manually based on litellm config.yaml rather than guessing by name.
THINKING_MODELS: set[str] = {"kimi-thinking", "grok-4-fast", "gpt-oss-120b"}

PROMPTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "prompts",
)

# Locale code → prompt subdirectory name. Keep lowercase short forms
# (en, zh-cn) — they read cleaner than the canonical XX-YY locale codes
# in URL/path contexts.
_LOCALE_TO_DIR = {
    "en-US": "en",
    "zh-CN": "zh-cn",
}


def _prompt_dir(lang: str | None) -> str:
    """Resolve the prompt subdirectory for `lang` (or current request locale)."""
    target = lang or i18n.current_locale()
    return _LOCALE_TO_DIR.get(target, _LOCALE_TO_DIR["zh-CN"])


def load_prompt(name: str, lang: str | None = None) -> str:
    """Read prompts/<lang_dir>/<name>.md fresh each call so edits hot-reload
    during dev. `lang` defaults to the current request locale."""
    path = os.path.join(PROMPTS_DIR, _prompt_dir(lang), f"{name}.md")
    with open(path, encoding="utf-8") as f:
        return f.read().rstrip("\n")


_ADDENDUM_START = "<!-- chat-addendum-start -->"
_ADDENDUM_END   = "<!-- chat-addendum-end -->"


def extract_chat_addendum(prompt_name: str, lang: str | None = None) -> str:
    """Extract the <!-- chat-addendum-start --> ... <!-- chat-addendum-end -->
    block from a typed report prompt, for injection into follow-up chat
    sys_content. The addendum is the tight role + style + anti-patterns
    section that should persist when the user asks follow-up questions.

    Returns "" if the prompt file doesn't exist or doesn't contain the markers
    (e.g. generic review_report.md falls through silently)."""
    try:
        text = load_prompt(prompt_name, lang=lang)
    except FileNotFoundError:
        return ""
    s = text.find(_ADDENDUM_START)
    e = text.find(_ADDENDUM_END)
    if s == -1 or e == -1 or e <= s:
        return ""
    return text[s + len(_ADDENDUM_START):e].strip()


def _personal_note_block(cfg: dict, lang: str | None = None) -> str:
    """Optional personal-background block — empty if user hasn't filled it in.

    Wraps the free-text personal note (injuries, recovery status, life
    events that may interrupt training) so the AI sees it as an explicit
    user-provided context block in the system prompt."""
    note = ((cfg or {}).get("personal_note") or "").strip()
    if not note:
        return ""
    header = i18n.t("prompt.personal_note_header", lang=lang)
    return f"\n\n{header}\n{note}"


def _long_term_insights_block(cfg: dict, lang: str | None = None) -> str:
    """Optional pinned-insights block — empty when user hasn't pinned anything.

    Items are dicts with `text` and `saved_at` keys (see user_config.py).
    Date prefix helps the AI judge recency."""
    items = (cfg or {}).get("coach_insights", []) or []
    if not items:
        return ""
    header = i18n.t("prompt.long_term_insights_header", lang=lang)
    lines = ["", "", header]
    for it in items:
        date_str = (it.get("saved_at") or "")[:10]  # YYYY-MM-DD
        prefix   = f"({date_str}) " if date_str else ""
        lines.append(f"- {prefix}{it['text']}")
    return "\n".join(lines)


def coach_sys(cfg: dict) -> str:
    lang = i18n.current_locale()
    phase = (cfg or {}).get("phase", "")
    nr = uc.next_race(cfg) if phase == "race_prep" else None
    if nr:
        rd = (date.fromisoformat(nr["date"]) - date.today()).days
        ctx = i18n.t("prompt.race_context.race_prep_with_race",
                     lang=lang, name=nr["name"], days=rd)
    elif phase == "race_prep":
        ctx = i18n.t("prompt.race_context.race_prep", lang=lang)
    else:
        ctx = i18n.t("prompt.race_context.daily", lang=lang)
    return load_prompt("coach_system", lang=lang).format(
        race_context=ctx,
        personal_note_block=_personal_note_block(cfg, lang=lang),
        long_term_insights_block=_long_term_insights_block(cfg, lang=lang),
    )


def review_chat_sys(cfg: dict) -> str:
    """System prompt for the per-activity review chat — same personal-note
    flow as `coach_sys`, but using the review-specific base prompt."""
    lang = i18n.current_locale()
    return load_prompt("review_chat_system", lang=lang).format(
        personal_note_block=_personal_note_block(cfg, lang=lang),
        long_term_insights_block=_long_term_insights_block(cfg, lang=lang),
    )


def llm_stream(messages: list[dict], model: str):
    body: dict = {"model": model, "messages": messages, "stream": True}
    resp = requests.post(
        f"{LLM_BASE}/chat/completions",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {LLM_API_KEY}",
        },
        json=body,
        stream=True,
        timeout=180,
    )
    resp.raise_for_status()
    for line in resp.iter_lines():
        if not line or not line.startswith(b"data: "):
            continue
        chunk = line[6:]
        if chunk == b"[DONE]":
            break
        try:
            _d = json.loads(chunk)["choices"][0]["delta"]
            # reasoning_content / thinking fields are internal — skip them
            if _d.get("reasoning_content") or _d.get("thinking"):
                continue
            delta = _d.get("content", "")
            if delta:
                yield delta
        except Exception:
            pass


# ── Tool-calling stream ─────────────────────────────────────────────────────
# Multi-turn loop: stream text → if tool_calls, execute Python handlers →
# append tool results to messages → re-stream → repeat until LLM emits
# only text (no more tool_calls).
#
# Yields:
#   ("text", str)        — assistant text delta (stream incrementally to UI)
#   ("tool_call", dict)  — informational: a tool call about to execute
#                          {name, args, id}
#   ("tool_result", dict)— informational: tool completed
#                          {name, args, id, result_summary}
#   ("done", None)       — final marker
#
# UI consumes the typed events: render text deltas as chat content; render
# tool_call/tool_result as collapsed inline cards or gray notes.

# Models known to NOT support tool calling well via LiteLLM proxy. If the
# user's model is in this set, we skip the tools parameter entirely and
# yield a one-time hint event so the UI can warn the user.
TOOL_CALLING_UNSUPPORTED_HINTS = {
    "kimi-thinking", "grok-4-fast", "gpt-oss-120b",
}


def _tools_supported(model: str) -> bool:
    """Return False for models known to mishandle tool calling. Default
    True (let the API call surface the error if support is uncertain)."""
    return model not in TOOL_CALLING_UNSUPPORTED_HINTS


def llm_stream_with_tools(
    messages: list[dict],
    tools: list[dict],
    tool_handlers: dict,
    model: str,
    max_iterations: int = 4,
):
    """Stream LLM response, executing tool calls until the model emits text-only.

    Args:
      messages       — chat messages so far (will be APPENDED to in-place
                       as tool calls + tool results are added)
      tools          — OpenAI-format tool schemas (review_tools.TOOL_SCHEMAS)
      tool_handlers  — {name → callable}; activity_id should be bound by the
                       caller via review_tools.make_tool_handlers(aid)
      model          — model name (must support tool calling — see
                       _tools_supported; otherwise yields ("fallback", reason))
      max_iterations — safety cap on tool-call rounds (typical ≤2)

    Yields typed events: ("text"|"tool_call"|"tool_result"|"fallback"|"done", payload)
    """
    if not _tools_supported(model):
        yield ("fallback", f"model `{model}` doesn't support drill-down tools — "
                            "switch to gpt-5.4 (or another tools-capable model) for "
                            "follow-up questions about specific seconds/distances.")
        for chunk in llm_stream(messages, model):
            yield ("text", chunk)
        yield ("done", None)
        return

    for iteration in range(max_iterations):
        # Accumulate tool_call deltas across stream chunks
        pending_calls = {}  # idx → {id, name, arguments_str}
        finish_reason = None

        body: dict = {
            "model":    model,
            "messages": messages,
            "tools":    tools,
            "stream":   True,
        }
        try:
            resp = requests.post(
                f"{LLM_BASE}/chat/completions",
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {LLM_API_KEY}",
                },
                json=body,
                stream=True,
                timeout=180,
            )
            resp.raise_for_status()
        except requests.exceptions.HTTPError as e:
            # Some proxies 400 when tools sent to unsupported model — graceful
            # fallback to non-tool stream.
            if e.response is not None and e.response.status_code == 400:
                yield ("fallback", f"LLM proxy rejected tools for model `{model}`: "
                                    f"{e.response.text[:200]}. Falling back to plain stream.")
                for chunk in llm_stream(messages, model):
                    yield ("text", chunk)
                yield ("done", None)
                return
            raise

        for line in resp.iter_lines():
            if not line or not line.startswith(b"data: "):
                continue
            chunk = line[6:]
            if chunk == b"[DONE]":
                break
            try:
                evt = json.loads(chunk)["choices"][0]
                delta = evt.get("delta", {})
                fr    = evt.get("finish_reason")
                if fr:
                    finish_reason = fr
                # Skip thinking / reasoning fields
                if delta.get("reasoning_content") or delta.get("thinking"):
                    continue
                # Text content delta
                if delta.get("content"):
                    yield ("text", delta["content"])
                # Tool-call deltas — accumulate by index (one tool call may
                # arrive across multiple chunks; arguments are streamed)
                for tc_delta in delta.get("tool_calls", []) or []:
                    idx = tc_delta.get("index", 0)
                    slot = pending_calls.setdefault(
                        idx, {"id": None, "name": None, "arguments": ""}
                    )
                    if tc_delta.get("id"):
                        slot["id"] = tc_delta["id"]
                    fn = tc_delta.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
            except Exception:
                pass

        # If no tool calls were requested, we're done
        if not pending_calls or finish_reason != "tool_calls":
            yield ("done", None)
            return

        # Append the assistant message with the tool_calls (required by API)
        assistant_msg = {
            "role":       "assistant",
            "content":    None,
            "tool_calls": [
                {
                    "id":       slot["id"],
                    "type":     "function",
                    "function": {
                        "name":      slot["name"],
                        "arguments": slot["arguments"],
                    },
                }
                for slot in pending_calls.values()
                if slot["id"] and slot["name"]
            ],
        }
        if not assistant_msg["tool_calls"]:
            yield ("done", None)
            return
        messages.append(assistant_msg)

        # Execute each tool call, append role:tool messages
        for slot in pending_calls.values():
            if not slot["id"] or not slot["name"]:
                continue
            try:
                args = json.loads(slot["arguments"]) if slot["arguments"] else {}
            except Exception:
                args = {}
            yield ("tool_call", {
                "id":   slot["id"],
                "name": slot["name"],
                "args": args,
            })

            handler = tool_handlers.get(slot["name"])
            if handler is None:
                result = {"error": f"unknown tool: {slot['name']}"}
            else:
                try:
                    result = handler(**args)
                except Exception as e:
                    result = {"error": f"tool execution failed: {e}"}

            messages.append({
                "role":         "tool",
                "tool_call_id": slot["id"],
                "content":      json.dumps(result, ensure_ascii=False),
            })
            yield ("tool_result", {
                "id":     slot["id"],
                "name":   slot["name"],
                "args":   args,
                "result_summary": _summarize_tool_result(result),
            })

        # Loop: next iteration re-streams with the updated messages
    # If we hit max_iterations, treat as done (LLM may have looped)
    yield ("done", None)


def _summarize_tool_result(result: dict) -> str:
    """One-line summary of tool result for UI display."""
    if "error" in result:
        return f"❌ {result['error'][:80]}"
    if "rows" in result:
        win = result.get("window", {})
        sampling = result.get("sampling", "?")
        ds  = win.get("duration_s", "?")
        return f"✓ {result['rows']} rows, {ds}s window, {sampling}"
    return "✓ tool returned"
