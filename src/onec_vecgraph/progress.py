"""Shared throttled progress reporter for long-running operations (index / callgraph /
vectorize / ingest): logs % done, average speed and ETA via the package logger.

Visible on stderr once the CLI configures logging (cli._configure); a no-op otherwise — the
standard library-logging contract, so importing this never forces output on library users.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger("onec_vecgraph.progress")


def fmt_duration(seconds: float) -> str:
    """Human-readable duration: '8с', '1м36с', '2ч05м'."""
    total = int(seconds) if seconds > 0 else 0
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}ч{m:02d}м"
    if m:
        return f"{m}м{s:02d}с"
    return f"{s}с"


class Progress:
    """Throttled progress reporter: call advance() per item, finish() at the end.

    `unit` is the genitive-plural count word ('объектов', 'модулей', 'чанков'); `rate_word`
    annotates speed (defaults to '<unit>/с'); `detail` annotates the start line (e.g. batch size).
    Lines are emitted at most once per `every_sec`; the 100% line is the final summary only.
    """

    def __init__(self, total: int, label: str, *, unit: str = "элементов",
                 rate_word: str | None = None, detail: str | None = None,
                 every_sec: float = 2.0) -> None:
        self.total = total
        self.label = label
        self.unit = unit
        self.rate_word = rate_word or f"{unit}/с"
        self.every = every_sec
        self.done = 0
        self.start = time.perf_counter()
        self.last = self.start
        if total:
            suffix = f" ({detail})" if detail else ""
            log.info("[%s] старт: %s %s%s", label, f"{total:,}", unit, suffix)

    def advance(self, n: int = 1) -> None:
        self.done += n
        now = time.perf_counter()
        # Throttle by wall-time; never emit a mid-run line at 100% (the final summary covers it).
        if self.done < self.total and now - self.last >= self.every:
            self._emit(now)
            self.last = now

    def _emit(self, now: float) -> None:
        elapsed = now - self.start
        rate = self.done / elapsed if elapsed > 0 else 0.0
        pct = 100.0 * self.done / self.total if self.total else 100.0
        eta = (self.total - self.done) / rate if rate > 0 else 0.0
        log.info("[%s] %s/%s %s (%.1f%%) | %.0f %s | прошло %s | ETA ~%s",
                 self.label, f"{self.done:,}", f"{self.total:,}", self.unit, pct, rate,
                 self.rate_word, fmt_duration(elapsed), fmt_duration(eta))

    def finish(self) -> dict:
        elapsed = time.perf_counter() - self.start
        rate = self.done / elapsed if elapsed > 0 else 0.0
        if self.total:
            log.info("[%s] готово: %s %s за %s (%.0f %s)",
                     self.label, f"{self.done:,}", self.unit, fmt_duration(elapsed), rate, self.rate_word)
        return {"elapsed_sec": round(elapsed, 2), "items_per_sec": round(rate, 1)}
