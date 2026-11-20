"""Per-run investigation records (PRD §12). Only the parent process writes these.

REC-1: one JSON line per result appended to `data/runs/<YYYY-MM-DD>.jsonl` (date in ET).
REC-3: at startup and after each write, delete `*.jsonl` older than 30 days.
REC-4: the directory is created if missing; a write failure is logged and never
affects the run's outcome or its notification.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_RETENTION_DAYS = 30


class RunRecords:
    """Appends one JSON line per run and prunes old files."""

    def __init__(
        self,
        directory: str,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.directory = Path(directory)
        self._now = now or (lambda: datetime.now(_ET))

    @property
    def _today(self) -> datetime:
        return self._now().astimezone(_ET)

    def _day_file(self, day: datetime) -> Path:
        return self.directory / f"{day.date().isoformat()}.jsonl"

    def prune(self) -> None:
        """REC-3: delete `*.jsonl` older than 30 days before today. Never touch others."""
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem failure
            logger.error("run_records: cannot create %s: %s", self.directory, exc)
            return
        cutoff = self._today.date() - timedelta(days=_RETENTION_DAYS)
        for path in self.directory.glob("*.jsonl"):
            try:
                day = datetime.strptime(path.stem, "%Y-%m-%d").date()
            except ValueError:
                logger.warning("run_records: skipping non-dated file %s", path.name)
                continue
            if day < cutoff:
                try:
                    path.unlink()
                    logger.debug("run_records: pruned %s", path.name)
                except OSError as exc:  # pragma: no cover
                    logger.error("run_records: cannot prune %s: %s", path.name, exc)

    def append(self, record: dict) -> None:
        """REC-1: append one JSON line for a record; then prune (REC-3).

        A failure is logged and never propagates (REC-4).
        """
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self._day_file(self._today)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except (OSError, TypeError, ValueError) as exc:
            logger.error("run_records: failed to write record for %s: %s",
                         record.get("run_id", "?"), exc)
        finally:
            try:
                self.prune()
            except Exception:  # noqa: BLE001 - pruning must never break a run
                logger.error("run_records: pruning failed", exc_info=True)