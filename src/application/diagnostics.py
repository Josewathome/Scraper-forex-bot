"""
diagnostics.py — Optional signal diagnostics collector.

DiagnosticsCollector records why signals were rejected or accepted
during a single evaluation cycle.  It is passed as an optional `diag`
parameter to analysis functions — callers pass None to skip collection.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class DiagnosticsCollector:
    """
    Collects rejection and signal events during one entry evaluation.
    Pass an instance to analysis functions; inspect after evaluation.
    """
    rejections: List[Dict] = field(default_factory=list)
    signals:    List[Dict] = field(default_factory=list)

    def record_rejection(self, reason: str, **kwargs) -> None:
        self.rejections.append({"reason": reason, **kwargs})

    def record_signal(self, signal_type: str, **kwargs) -> None:
        self.signals.append({"signal_type": signal_type, **kwargs})

    def get_summary(self) -> Dict:
        return {
            "rejections": len(self.rejections),
            "signals":    len(self.signals),
            "rejection_reasons": [r["reason"] for r in self.rejections],
        }

    def reset(self) -> None:
        self.rejections.clear()
        self.signals.clear()
