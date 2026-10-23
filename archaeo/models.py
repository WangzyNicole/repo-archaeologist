"""Data structures shared by the tools, the agent loop, and the renderers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass
class TimelineEvent:
    """One decision point in the history of the code being investigated."""

    date: str = ""
    kind: Literal["commit", "pr", "issue", "release", "other"] = "other"
    ref: str = ""            # e.g. "a1b2c3d" or "#412"
    title: str = ""
    url: str = ""
    actor: str = ""
    why_it_matters: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Findings:
    """The agent's final answer."""

    answer: str = ""                                # markdown narrative
    timeline: list[TimelineEvent] = field(default_factory=list)
    confidence: Literal["high", "medium", "low"] = "medium"
    open_questions: list[str] = field(default_factory=list)

    # Filled in by verification, not by the model.
    unverified_urls: list[str] = field(default_factory=list)
    unverified_refs: list[str] = field(default_factory=list)   # bare "#412" / "a1b2c3d" never retrieved
    verified_citations: list[str] = field(default_factory=list)
    support: Literal["supported", "partial", "unsupported", ""] = ""   # "" = not checked

    def to_dict(self) -> dict[str, Any]:
        return {
            "answer": self.answer,
            "timeline": [event.to_dict() for event in self.timeline],
            "confidence": self.confidence,
            "open_questions": list(self.open_questions),
            "support": self.support,
            "verified_citations": list(self.verified_citations),
            "unverified_urls": list(self.unverified_urls),
            "unverified_refs": list(self.unverified_refs),
        }

    @property
    def unverified(self) -> list[str]:
        return self.unverified_urls + self.unverified_refs

    @classmethod
    def from_tool_input(cls, payload: dict[str, Any]) -> "Findings":
        events: list[TimelineEvent] = []
        for raw in payload.get("timeline") or []:
            if not isinstance(raw, dict):
                continue
            events.append(
                TimelineEvent(
                    date=str(raw.get("date", "")),
                    kind=raw.get("kind") if raw.get("kind") in {"commit", "pr", "issue", "release", "other"} else "other",
                    ref=str(raw.get("ref", "")),
                    title=str(raw.get("title", "")),
                    url=str(raw.get("url", "")),
                    actor=str(raw.get("actor", "")),
                    why_it_matters=str(raw.get("why_it_matters", "")),
                )
            )
        events.sort(key=lambda e: e.date or "9999")
        confidence = payload.get("confidence")
        return cls(
            answer=str(payload.get("answer", "")).strip(),
            timeline=events,
            confidence=confidence if confidence in {"high", "medium", "low"} else "medium",
            open_questions=[str(q) for q in (payload.get("open_questions") or [])],
        )


@dataclass
class ToolCall:
    """A single step of the investigation, kept for the audit trail."""

    name: str
    arguments: dict[str, Any]
    result_preview: str = ""
    duration_ms: int = 0
    error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Investigation:
    """Everything about one run, serialisable to JSON for the report and web UI."""

    question: str
    repo: str
    target: str = ""
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))
    finished_at: str = ""
    findings: Findings | None = None
    trace: list[ToolCall] = field(default_factory=list)
    thinking: list[str] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "repo": self.repo,
            "target": self.target,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "findings": self.findings.to_dict() if self.findings else None,
            "trace": [call.to_dict() for call in self.trace],
            "thinking": list(self.thinking),
            "usage": dict(self.usage),
            "error": self.error,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)
