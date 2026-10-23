"""The evidence ledger: what the agent is actually allowed to cite.

Every tool records the canonical URL of anything it surfaced, and every tool
result is scanned for the commit shas and #issue numbers it showed the model.
When the agent submits its findings we check each citation against this ledger
— links, but also bare references like "#412" or "a1b2c3d" written into the
prose. A citation the agent never actually retrieved is a hallucination risk, and
the report says so instead of quietly presenting it as sourced. An answer that
cites nothing retrieved at all is flagged as unsupported as a whole. This is the
difference between an answer that looks researched and one that demonstrably is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_URL_RE = re.compile(r"https?://[^\s\)\]\>\"'`,]+")
_TRAILING_PUNCT = ".,;:!?"
# A short or full commit sha standing on its own: not part of a path or a word.
_SHA_RE = re.compile(r"(?<![\w/])[0-9a-f]{7,40}(?![\w/])")
# An issue or PR number: "#412", but not an HTML entity, URL fragment or "##".
_REF_RE = re.compile(r"(?<![\w/&#])#(\d{1,7})\b")
_URL_NUMBER_RE = re.compile(r"/(?:pull|issues)/(\d+)")
_URL_SHA_RE = re.compile(r"/commit/([0-9a-f]{7,40})")


def normalise(url: str) -> str:
    url = url.strip().rstrip(_TRAILING_PUNCT)
    url = url.split("#")[0]
    return url.rstrip("/")


def extract_urls(text: str) -> list[str]:
    return [normalise(match) for match in _URL_RE.findall(text or "")]


def extract_shas(text: str) -> list[str]:
    """Hex runs that look like commit shas. Requiring a digit and a letter keeps
    plain numbers and words like 'defaced' from being read as citations."""
    return [
        match
        for match in _SHA_RE.findall(text or "")
        if any(c.isdigit() for c in match) and any(c.isalpha() for c in match)
    ]


def extract_refs(text: str) -> list[int]:
    return [int(number) for number in _REF_RE.findall(text or "")]


@dataclass
class CitationCheck:
    """The result of checking an answer's citations against the ledger."""

    verified: list[str] = field(default_factory=list)
    unverified_urls: list[str] = field(default_factory=list)
    unverified_refs: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        """supported: everything cited was retrieved. partial: some of it was not.
        unsupported: nothing cited was retrieved, or nothing was cited at all."""
        if not self.verified:
            return "unsupported"
        if self.unverified_urls or self.unverified_refs:
            return "partial"
        return "supported"


@dataclass
class EvidenceLedger:
    """URLs, commit shas and issue numbers the tools genuinely retrieved during this investigation."""

    seen: set[str] = field(default_factory=set)
    labels: dict[str, str] = field(default_factory=dict)
    shas: set[str] = field(default_factory=set)
    numbers: set[int] = field(default_factory=set)

    def record(self, url: str, label: str = "") -> str:
        if not url:
            return ""
        key = normalise(url)
        self.seen.add(key)
        if label and key not in self.labels:
            self.labels[key] = label
        self.shas.update(_URL_SHA_RE.findall(key))
        self.numbers.update(int(n) for n in _URL_NUMBER_RE.findall(key))
        return url

    def record_many(self, urls: list[str]) -> None:
        for url in urls:
            self.record(url)

    def record_text(self, text: str) -> None:
        """Everything a tool result showed the model is citable: its links, and
        the shas and #numbers in it, which is all a local-only repo ever shows."""
        self.record_many(extract_urls(text))
        self.shas.update(extract_shas(_URL_RE.sub(" ", text or "")))
        self.numbers.update(extract_refs(text))

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

    def ref_supported(self, ref: str) -> bool:
        """A bare '#412' or 'a1b2c3d' written into the answer."""
        if ref.startswith("#"):
            return ref[1:].isdigit() and int(ref[1:]) in self.numbers
        return any(_same_sha(ref, known) for known in self.shas)

    def unsupported(self, *texts: str) -> list[str]:
        missing: list[str] = []
        for text in texts:
            for url in extract_urls(text):
                if not self.is_supported(url) and url not in missing:
                    missing.append(url)
        return missing

    def check(self, *texts: str) -> CitationCheck:
        """Check every citation in the given texts: links first, then the bare
        shas and #numbers left once the links are removed."""
        result = CitationCheck()
        for text in texts:
            for url in extract_urls(text):
                if self.is_supported(url):
                    _add(result.verified, url)
                else:
                    _add(result.unverified_urls, url)
            prose = _URL_RE.sub(" ", text or "")
            refs = [f"#{n}" for n in extract_refs(prose)] + extract_shas(prose)
            for ref in refs:
                if self.ref_supported(ref):
                    _add(result.verified, ref)
                else:
                    _add(result.unverified_refs, ref)
        return result


def _add(bucket: list[str], item: str) -> None:
    if item not in bucket:
        bucket.append(item)


def _same_sha(a: str, b: str) -> bool:
    shortest = min(len(a), len(b))
    return shortest >= 7 and a[:shortest] == b[:shortest]


def _same_commit(a: str, b: str) -> bool:
    marker = "/commit/"
    if marker not in a or marker not in b:
        return False
    a_base, _, a_sha = a.partition(marker)
    b_base, _, b_sha = b.partition(marker)
    if a_base != b_base or not a_sha or not b_sha:
        return False
    return _same_sha(a_sha, b_sha)
