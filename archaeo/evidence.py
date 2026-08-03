"""The evidence ledger: what the agent is actually allowed to cite.

Every tool records the canonical URL of anything it surfaced. When the agent
submits its findings we check each citation against this ledger. A link the
agent never actually retrieved is a hallucination risk, and the report says so
instead of quietly presenting it as sourced. This is the difference between an
answer that looks researched and one that demonstrably is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_URL_RE = re.compile(r"https?://[^\s\)\]\>\"'`,]+")
_TRAILING_PUNCT = ".,;:!?"


def normalise(url: str) -> str:
    url = url.strip().rstrip(_TRAILING_PUNCT)
    url = url.split("#")[0]
    return url.rstrip("/")


def extract_urls(text: str) -> list[str]:
    return [normalise(match) for match in _URL_RE.findall(text or "")]


@dataclass
class EvidenceLedger:
    """URLs the tools genuinely retrieved during this investigation."""

    seen: set[str] = field(default_factory=set)
    labels: dict[str, str] = field(default_factory=dict)

    def record(self, url: str, label: str = "") -> str:
        if not url:
            return ""
        key = normalise(url)
        self.seen.add(key)
        if label and key not in self.labels:
            self.labels[key] = label
        return url

    def record_many(self, urls: list[str]) -> None:
        for url in urls:
            self.record(url)

    def is_supported(self, url: str) -> bool:
        key = normalise(url)
        if key in self.seen:
            return True
        # A commit URL is supported if we saw the commit under a short sha, and
        # vice versa: GitHub accepts any unambiguous prefix.
        for known in self.seen:
            if _same_commit(key, known):
                return True
        return False

    def unsupported(self, *texts: str) -> list[str]:
        missing: list[str] = []
        for text in texts:
            for url in extract_urls(text):
                if not self.is_supported(url) and url not in missing:
                    missing.append(url)
        return missing


def _same_commit(a: str, b: str) -> bool:
    marker = "/commit/"
    if marker not in a or marker not in b:
        return False
    a_base, _, a_sha = a.partition(marker)
    b_base, _, b_sha = b.partition(marker)
    if a_base != b_base or not a_sha or not b_sha:
        return False
    shortest = min(len(a_sha), len(b_sha))
    return shortest >= 7 and a_sha[:shortest] == b_sha[:shortest]
