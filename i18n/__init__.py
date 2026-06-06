"""i18n — translatable string catalog + per-request locale resolution.

Resolution order (set by `set_request_locale` in the LocaleMiddleware):
  1. db.locale_get()  — user's persisted choice
  2. cookie 'locale'  — used pre-DB-write transient hint (e.g. /locale POST)
  3. Accept-Language  — first-visit auto-detect (zh* → zh-CN, else en-US)
  4. DEFAULT_LOCALE env var
  5. db.LOCALE_DEFAULT ('en-US')

Catalogs live in `i18n/<lang>.py` as a STRINGS dict keyed by dotted strings
(e.g. `tag.aerobic`). Missing keys log a warning and fall back to
zh-CN; missing in zh-CN too returns the key itself so the UI is debuggable.

Usage:
  from i18n import t
  Span(t("settings.title"))
  Div(t("race.days_left", days=42))   # template: "还剩 {days} 天" / "{days} days left"
"""

import logging
import os
from contextvars import ContextVar

import db

log = logging.getLogger(__name__)

# Per-request locale set by middleware. Reading outside a request defaults
# to the env DEFAULT_LOCALE → db.LOCALE_DEFAULT.
_LOCALE_CTX: ContextVar[str | None] = ContextVar("locale", default=None)

# Fallback chain when a key is missing in the requested locale's catalog.
_FALLBACK_CHAIN = ("zh-CN", "en-US")


def _load_catalog(lang: str) -> dict[str, str]:
    # `import i18n.<modname>` — modname is lang with '-' → '_' (en-US → en_US)
    modname = lang.replace("-", "_")
    try:
        mod = __import__(f"i18n.{modname}", fromlist=["STRINGS"])
        return getattr(mod, "STRINGS", {}) or {}
    except ImportError:
        log.warning("i18n: catalog %r not found, falling back", lang)
        return {}


# Catalogs are loaded lazily on first access and cached for the process
# lifetime. Hot-reload during dev: restart the process (same as prompt files).
_CATALOGS: dict[str, dict[str, str]] = {}


def _get_catalog(lang: str) -> dict[str, str]:
    if lang not in _CATALOGS:
        _CATALOGS[lang] = _load_catalog(lang)
    return _CATALOGS[lang]


def env_default_locale() -> str:
    """DEFAULT_LOCALE env var (set per-instance in prod compose) or library default."""
    return os.environ.get("DEFAULT_LOCALE") or db.LOCALE_DEFAULT


def current_locale() -> str:
    """Return the locale active for the current request, or the env default."""
    return _LOCALE_CTX.get() or env_default_locale()


def set_request_locale(value: str) -> None:
    """Called by the request middleware once per request."""
    _LOCALE_CTX.set(value)


def pick_lang_from_accept(header: str | None) -> str:
    """Map an Accept-Language header to one of LOCALES_SUPPORTED.
    zh-* → zh-CN, anything else (including missing) → en-US."""
    if not header:
        return "en-US"
    # Header format: "zh-CN,zh;q=0.9,en;q=0.8" — first tag wins for our purposes
    first = header.split(",")[0].strip().lower()
    if first.startswith("zh"):
        return "zh-CN"
    return "en-US"


def t(key: str, lang: str | None = None, **fmt) -> str:
    """Translate `key` to current (or specified) locale, applying str.format(**fmt).

    Missing keys: warn-once + fall back through _FALLBACK_CHAIN; if still
    missing, return the key itself (visible in UI → debuggable)."""
    target = lang or current_locale()
    cat = _get_catalog(target)
    raw = cat.get(key)
    if raw is None:
        for fb in _FALLBACK_CHAIN:
            if fb == target:
                continue
            raw = _get_catalog(fb).get(key)
            if raw is not None:
                _warn_missing(key, target)
                break
    if raw is None:
        _warn_missing(key, target, no_fallback=True)
        raw = key
    if not fmt:
        return raw
    try:
        return raw.format(**fmt)
    except (KeyError, IndexError) as e:
        log.warning("i18n: format failed for %r in %r: %s", key, target, e)
        return raw


_WARNED: set[tuple[str, str]] = set()


def _warn_missing(key: str, lang: str, *, no_fallback: bool = False) -> None:
    pair = (key, lang)
    if pair in _WARNED:
        return
    _WARNED.add(pair)
    if no_fallback:
        log.warning("i18n: missing key %r in all catalogs", key)
    else:
        log.warning("i18n: missing key %r in %r, used fallback", key, lang)


__all__ = [
    "t",
    "current_locale",
    "set_request_locale",
    "pick_lang_from_accept",
    "env_default_locale",
]
