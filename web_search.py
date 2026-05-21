"""Web search backend — Tavily API.

Exposed to the LLM via review_tools.web_search. Tavily is purpose-built for
LLM agents: returns clean markdown excerpts, a synthesized answer, and a
source list. https://tavily.com — free tier: 1000 searches/month.

Requires env var TAVILY_API_KEY. If missing, search() returns a structured
error the LLM can surface to the user, instead of crashing the chat.
"""

import os
import requests

_ENDPOINT = "https://api.tavily.com/search"
_TIMEOUT_S = 12

# Per-result content cap — keeps the tool response token-light. 500 chars is
# usually enough for the LLM to grok the source's gist; full pages would
# blow up context.
_CONTENT_CAP = 500


def search(query: str, max_results: int = 5) -> dict:
    """Search the web. Returns:
      {answer, results: [{title, url, content, score}, ...], n}
    On error returns {error: "..."} so the LLM can surface it gracefully."""
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    if not key:
        return {"error": "TAVILY_API_KEY not set — web_search unavailable"}

    if not query or not query.strip():
        return {"error": "query is required"}

    try:
        max_results = max(1, min(int(max_results), 10))
    except (TypeError, ValueError):
        max_results = 5

    try:
        r = requests.post(
            _ENDPOINT,
            json={
                "api_key":        key,
                "query":          query.strip(),
                "search_depth":   "basic",
                "max_results":    max_results,
                "include_answer": True,
            },
            timeout=_TIMEOUT_S,
        )
    except requests.RequestException as e:
        return {"error": f"web_search network error: {e}"}

    if r.status_code != 200:
        return {"error": f"web_search HTTP {r.status_code}: {r.text[:200]}"}

    try:
        data = r.json()
    except ValueError:
        return {"error": "web_search returned non-JSON response"}

    results = []
    for item in (data.get("results") or [])[:max_results]:
        content = (item.get("content") or "").strip()
        if len(content) > _CONTENT_CAP:
            content = content[:_CONTENT_CAP] + "…"
        results.append({
            "title":   (item.get("title") or "").strip(),
            "url":     item.get("url") or "",
            "content": content,
            "score":   round(item.get("score", 0.0), 3),
        })

    return {
        "query":   query.strip(),
        "answer":  (data.get("answer") or "").strip(),
        "n":       len(results),
        "results": results,
    }
