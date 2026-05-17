"""Background report-generation jobs.

Each activity has at most one in-flight generation job. The worker thread
runs the LLM call independent of any client connection — clients subscribe
via `subscribe(aid)` to receive a replay-from-start queue of section events.

Section events fire when the running text crosses a `\n## ` boundary (the
markdown second-level heading the typed report prompts emit between
sections). Clients render each completed section as a finished block and
show a chips-style placeholder for the in-progress one.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional


_JOBS: "dict[int, _Job]" = {}
_LOCK = threading.Lock()


class _Job:
    """One in-flight (or recently-finished) report generation. Thread-safe."""

    def __init__(self, aid: int, builder_name: str,
                 on_complete: Optional[Callable[[str], None]] = None):
        self.aid = aid
        self.builder_name = builder_name
        self.status: str = "running"   # "running" | "done" | "error"
        self.sections: list[str] = []  # completed section markdown blobs
        self.current: str = ""         # in-progress section buffer
        self.full_text: str = ""       # everything streamed so far
        self.error: Optional[str] = None
        self.started_at = time.time()
        self.queues: "list[queue.Queue]" = []
        self.lock = threading.Lock()
        self._on_complete = on_complete

    # ── client-facing API ────────────────────────────────────────────────
    def subscribe(self) -> "queue.Queue":
        """Return a new event queue with the current state replayed onto it
        (each completed section + a current_started hint if the next section
        has already started flowing in)."""
        q: queue.Queue = queue.Queue(maxsize=1024)
        with self.lock:
            for s in self.sections:
                q.put_nowait(("section", s))
            if self.current.strip():
                q.put_nowait(("current_started", None))
            if self.status == "done":
                q.put_nowait(("done", self.builder_name))
            elif self.status == "error":
                q.put_nowait(("error", self.error or "generation failed"))
            self.queues.append(q)
        return q

    def unsubscribe(self, q: "queue.Queue") -> None:
        with self.lock:
            try:
                self.queues.remove(q)
            except ValueError:
                pass

    # ── worker-facing API ────────────────────────────────────────────────
    def feed_chunk(self, text: str) -> None:
        """Append a new LLM token chunk. Detects `\\n## ` section boundaries
        and broadcasts a `section` event for each completed section."""
        with self.lock:
            self.full_text += text
            had_current = bool(self.current.strip())
            self.current += text
            while True:
                idx = self.current.find("\n## ")
                if idx == -1:
                    break
                section_text = self.current[:idx]
                # next section starts with `## ` (no leading \n)
                self.current = self.current[idx + 1:]
                if section_text.strip():
                    self.sections.append(section_text)
                    self._broadcast_locked(("section", section_text))
            if (not had_current) and self.current.strip():
                self._broadcast_locked(("current_started", None))

    def finalize(self) -> None:
        """Mark done. Flushes any remaining buffer as a final section, then
        notifies subscribers + invokes the on_complete callback (typically a
        DB write)."""
        full_text: str
        on_complete: Optional[Callable[[str], None]]
        with self.lock:
            tail = self.current
            self.current = ""
            if tail.strip():
                self.sections.append(tail)
                self._broadcast_locked(("section", tail))
            self.status = "done"
            self._broadcast_locked(("done", self.builder_name))
            full_text = self.full_text
            on_complete = self._on_complete
        if on_complete is not None:
            try:
                on_complete(full_text)
            except Exception:
                # Persistence failed — leave the job in memory so the client
                # still sees the result; user can manually regenerate.
                pass

    def set_error(self, msg: str) -> None:
        with self.lock:
            self.status = "error"
            self.error = msg
            self._broadcast_locked(("error", msg))

    # ── internal ─────────────────────────────────────────────────────────
    def _broadcast_locked(self, event: tuple) -> None:
        for q in self.queues:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass


# ── module-level registry ───────────────────────────────────────────────────
def get(aid: int) -> Optional[_Job]:
    with _LOCK:
        return _JOBS.get(aid)


def start(aid: int, builder_name: str,
          worker: Callable[["_Job"], None],
          on_complete: Optional[Callable[[str], None]] = None
          ) -> "tuple[_Job, bool]":
    """Start a generation job for `aid`. If one is already running, returns
    (existing, False) and does NOT spawn a duplicate thread. Otherwise creates
    a new job, replacing any previous done/errored one in place, and spawns
    its worker thread. Returns (job, True) for the new-start case."""
    with _LOCK:
        existing = _JOBS.get(aid)
        if existing and existing.status == "running":
            return existing, False
        job = _Job(aid, builder_name, on_complete=on_complete)
        _JOBS[aid] = job

    def _run():
        try:
            worker(job)
            job.finalize()
        except Exception as e:
            job.set_error(f"{type(e).__name__}: {e}")

    t = threading.Thread(target=_run, name=f"report-{aid}", daemon=True)
    t.start()
    return job, True


def reset(aid: int) -> None:
    """Drop the cached job entry — call when the user has consumed the result
    (e.g. on /clear). Subsequent get() returns None until start() is called."""
    with _LOCK:
        _JOBS.pop(aid, None)
