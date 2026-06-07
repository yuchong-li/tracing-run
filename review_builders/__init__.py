"""ReviewBuilder framework — per-activity-type context builders.

Each builder reads raw activity data from `cache/garmin.db` (the SQLite tier)
and emits a `BuildResult`:
  - `context_md`: markdown text fed to the coach LLM as a user message
  - `highlight_windows`: drill-zone hints (label + sec range + channels)
  - `builder_hash`: AST-derived signature, bumps when builder logic changes

`DefaultBuilder` is the fallback for unrecognized tags; typed builders
(IntervalBuilder / LongRunBuilder / TempoBuilder / RaceBuilder / TrailBuilder /
HillBuilder / AerobicBuilder) handle the tagged workout types.

Public API:
  dispatch(tag, activity_type_key) -> ReviewBuilder
"""

from review_builders.base      import BuildResult, ReviewBuilder
from review_builders.default   import DefaultBuilder
from review_builders.aerobic   import AerobicBuilder
from review_builders.steady    import SteadyBuilder
from review_builders.long_run  import LongRunBuilder
from review_builders.tempo     import TempoBuilder
from review_builders.intervals import IntervalBuilder
from review_builders.race      import RaceBuilder
from review_builders.trail     import TrailBuilder
from review_builders.hill      import HillBuilder

import user_config as uc

__all__ = ["dispatch", "is_beta", "BuildResult", "ReviewBuilder",
           "DefaultBuilder", "AerobicBuilder", "SteadyBuilder", "LongRunBuilder",
           "TempoBuilder", "IntervalBuilder", "RaceBuilder", "TrailBuilder",
           "HillBuilder"]

# Builder name → class. Tags whose mapping in user_config.ACTIVITY_TAG_TO_BUILDER
# points to a name NOT in this registry fall through to DefaultBuilder, and
# is_beta() flags them with a "(beta)" suffix in the UI.
_BUILDER_REGISTRY: dict[str, type[ReviewBuilder]] = {
    "DefaultBuilder":  DefaultBuilder,
    "AerobicBuilder":  AerobicBuilder,    # aerobic
    "SteadyBuilder":   SteadyBuilder,     # steady (own builder: lap-to-lap EF trend + steady-framed ceiling)
    "LongRunBuilder":  LongRunBuilder,    # long_run
    "TempoBuilder":    TempoBuilder,      # tempo + threshold
    "IntervalBuilder": IntervalBuilder,   # intervals
    "RaceBuilder":     RaceBuilder,       # race
    "TrailBuilder":    TrailBuilder,      # trail
    "HillBuilder":     HillBuilder,       # hill
}


def dispatch(tag: str, activity_type_key: str) -> ReviewBuilder:
    """Return the builder appropriate for (tag, activity_type_key).

    Resolution order:
      1. user_config.ACTIVITY_TAG_TO_BUILDER[tag] → builder name
      2. _BUILDER_REGISTRY[name]                  → builder class
      3. instantiate
    Falls back to DefaultBuilder if any step fails (empty / unmarked tag,
    an unmapped tag like `other`, or a named builder class that isn't
    registered yet)."""
    builder_name = uc.ACTIVITY_TAG_TO_BUILDER.get(tag or "")
    builder_cls  = _BUILDER_REGISTRY.get(builder_name) if builder_name else None
    return (builder_cls or DefaultBuilder)()


def is_beta(tag: str) -> bool:
    """A tag is "beta" when EITHER of these is true:
      1. ACTIVITY_TAG_TO_BUILDER points to a builder name not yet in
         _BUILDER_REGISTRY (dispatch falls back to DefaultBuilder).
      2. ACTIVITY_TAG_TO_PROMPT lacks an entry for this tag (the LLM call
         falls back to the generic review_report.md prompt).

    A tag is "fully shipped" only when BOTH the typed builder and the typed
    prompt exist. Tags intentionally pointing to DefaultBuilder (untagged /
    `other`) are NOT beta — DefaultBuilder + generic prompt is their permanent home.

    UI uses this to suffix selectbox labels with "(beta)"."""
    builder_name = uc.ACTIVITY_TAG_TO_BUILDER.get(tag or "")
    if not builder_name:
        return False                # untagged or `other` — DefaultBuilder is correct
    builder_ready = builder_name in _BUILDER_REGISTRY
    prompt_ready  = tag in uc.ACTIVITY_TAG_TO_PROMPT
    return not (builder_ready and prompt_ready)
