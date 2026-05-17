"""Chat compression + insight-distillation helpers (no UI deps)."""

import i18n
from ui.llm import llm_stream, load_prompt

RECENT_N    = 50    # min messages sent verbatim to LLM
RESUM_EVERY = 20    # only re-summarize when the unsummarized older block grows by N


def summarize_chunk(msgs_chunk: list, prior_summary: str, model: str) -> str:
    """One-shot LLM call to compress a chunk of older messages into prose."""
    chunk_text = "\n".join(f"{m['role']}: {m['content']}" for m in msgs_chunk)
    prior_block = (
        "\n\n" + i18n.t("chat_summary.prior_summary_label") + prior_summary
        if prior_summary else ""
    )
    sys = load_prompt("chat_summary").format(prior_block=prior_block)
    try:
        user_msg = i18n.t("chat_summary.chunk_label") + "\n" + chunk_text
        out = "".join(llm_stream(
            [{"role": "system", "content": sys},
             {"role": "user",   "content": user_msg}],
            model,
        ))
        return out.strip()
    except Exception:
        return prior_summary  # don't break chat on summarizer failure


def maybe_resummarize(msgs: list, summary: str, summary_idx: int, model: str):
    """Returns (new_summary, new_summary_idx). No-op if threshold not crossed."""
    n = len(msgs)
    if n <= RECENT_N:
        return summary, summary_idx
    older_end = n - RECENT_N
    if older_end - summary_idx < RESUM_EVERY:
        return summary, summary_idx
    chunk = msgs[summary_idx:older_end]
    return summarize_chunk(chunk, summary, model), older_end


def _distill_with_llm(text: str, model: str) -> str:
    """One-shot call to compress the textarea content into a ≤20字 insight.

    Used by the ✨ 提炼 button in the pin popover. Returns the trimmed
    one-line result; raises on LLM failure (caller wraps in spinner+error).
    """
    msgs = [
        {"role": "system", "content": load_prompt("insight_distill")},
        {"role": "user",   "content": text},
    ]
    out = "".join(llm_stream(msgs, model))
    return out.strip().strip('"\'""''').rstrip("。.!?！？")


def _refine_personal_note_with_llm(text: str, model: str) -> str:
    """Reorganize a freeform 关于我 note into structured markdown."""
    msgs = [
        {"role": "system", "content": load_prompt("personal_note_refine")},
        {"role": "user",   "content": text},
    ]
    out = "".join(llm_stream(msgs, model)).strip()
    # Defensive: strip ```markdown / ``` fences if the model added them anyway
    if out.startswith("```"):
        out = out.split("\n", 1)[-1] if "\n" in out else out
        if out.endswith("```"):
            out = out[:-3].rstrip()
    return out
