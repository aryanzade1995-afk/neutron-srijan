"""Investigator feedback: the loop that turns closed cases into training labels.

The pitch claims confirmed fraud becomes labelled data that improves scoring. This
is that mechanism, kept deliberately small: an append-only JSON Lines log, replayed
into memory at startup. Append-only matters because a freeze decision is an audit
trail - a verdict is superseded by a later entry, never edited in place.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

VERDICTS = {"confirmed_fraud", "false_positive", "under_review"}


@dataclass
class Verdict:
    entry_txn_id: str
    end_node: str
    verdict: str
    note: str = ""
    reviewer: str = "unattributed"
    recorded_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def as_dict(self) -> dict:
        return asdict(self)


class FeedbackStore:
    """Append-only verdict log keyed by entry transaction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._latest: dict[str, Verdict] = {}
        self._count = 0
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                self._latest[record["entry_txn_id"]] = Verdict(**record)
                self._count += 1
            except (json.JSONDecodeError, TypeError, KeyError):
                # a malformed line must not take the service down on boot
                continue

    def record(self, verdict: Verdict) -> Verdict:
        if verdict.verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {sorted(VERDICTS)}")
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(verdict.as_dict()) + "\n")
            self._latest[verdict.entry_txn_id] = verdict
            self._count += 1
        return verdict

    def for_chain(self, entry_txn_id: str) -> Verdict | None:
        return self._latest.get(entry_txn_id)

    def all(self) -> list[dict]:
        return [v.as_dict() for v in
                sorted(self._latest.values(), key=lambda v: v.recorded_at, reverse=True)]

    def summary(self) -> dict:
        counts = {name: 0 for name in sorted(VERDICTS)}
        for verdict in self._latest.values():
            counts[verdict.verdict] = counts.get(verdict.verdict, 0) + 1
        return {
            "chains_reviewed": len(self._latest),
            "entries_logged": self._count,
            "counts": counts,
        }

    def training_labels(self) -> dict[str, int]:
        """Confirmed end nodes as positives, dismissed ones as negatives.

        Consumed by a retrain rather than applied live - a scorer that shifted
        under an investigator mid-review would make the queue untrustworthy.
        """
        labels: dict[str, int] = {}
        for verdict in self._latest.values():
            if verdict.verdict == "confirmed_fraud":
                labels[verdict.end_node] = 1
            elif verdict.verdict == "false_positive":
                labels[verdict.end_node] = 0
        return labels
