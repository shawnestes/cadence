"""Detect probable typos of major mailbox-provider domains in email addresses.

Pure: no I/O. A lead typed as ``jane@gmai.com`` is not jane's mailbox, and
mailing it either bounces or lands at whoever registered the typo domain.
Neither outcome is worth the send, so the loader rejects such rows rather than
guessing a correction.

Only near misses of a fixed provider list are flagged. Unknown domains are
never flagged on spelling alone — a corporate domain one letter away from
another corporate domain is ordinary, not a typo.
"""

from __future__ import annotations

# Order matters only for ties: the first provider at the minimum distance wins.
_FUZZY_PROVIDERS: tuple[str, ...] = (
    "gmail.com",
    "yahoo.com",
    "hotmail.com",
    "outlook.com",
    "icloud.com",
    "comcast.net",
    "verizon.net",
    "sbcglobal.net",
    "bellsouth.net",
    "charter.net",
    "earthlink.net",
    "googlemail.com",
    "protonmail.com",
    "rocketmail.com",
)

# Labels too short for edit distance: one edit away from them is a different,
# often real, domain (aol.com/aim.com, msn.com/mac.com, cox.net/fox.net). Only
# a misspelled TLD on the exact label is flagged for these.
_SHORT_PROVIDERS: tuple[str, ...] = (
    "aol.com",
    "msn.com",
    "live.com",
    "me.com",
    "mac.com",
    "att.net",
    "cox.net",
    "ymail.com",
)

_TLD_TYPOS: dict[str, frozenset[str]] = {
    "com": frozenset({"co", "con", "cm", "om", "cmo", "comm", "ocm", "xom", "vom", "cpm", "coom"}),
    "net": frozenset({"ne", "nt", "nett", "met", "bet", "ner", "nte"}),
}

# Real mailbox domains that sit within edit distance of a provider above.
_ALLOW: frozenset[str] = frozenset(
    {
        *_FUZZY_PROVIDERS,
        *_SHORT_PROVIDERS,
        "mail.com",
        "email.com",
        "cloud.com",
        "gmx.com",
        "gmx.net",
        "aim.com",
        "cs.com",
        "fastmail.com",
        "fastmail.fm",
        "juno.com",
        "netzero.net",
        "windstream.net",
        "frontier.com",
        "optonline.net",
        "centurylink.net",
    }
)


def _osa_distance(a: str, b: str, limit: int) -> int:
    """Optimal-string-alignment distance; returns limit + 1 once it is exceeded.

    Counts insertions, deletions, substitutions and adjacent transpositions
    (``gmial`` -> ``gmail`` is one edit).
    """
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


def _max_edits(label: str) -> int:
    # Two edits on a 5-letter label reaches unrelated words; on a 7-letter
    # one ("outlook") it still only reaches misspellings ("outookl").
    return 2 if len(label) >= 7 else 1


def probable_typo(domain: str) -> str | None:
    """Return the provider ``domain`` is probably a misspelling of, else None.

    ``domain`` is expected lowercase, as produced by ``normalize_email``.
    Edit distance is measured on the label left of the TLD and only against
    providers with the same TLD, so country-code variants (``hotmail.ca``,
    ``protonmail.ch``) are never mistaken for typos. A misspelled TLD is
    recognised only through ``_TLD_TYPOS`` on an exactly matching label.
    """
    if domain in _ALLOW:
        return None

    label, _, tld = domain.rpartition(".")
    if not label:
        return None

    for provider in (*_FUZZY_PROVIDERS, *_SHORT_PROVIDERS):
        p_label, _, p_tld = provider.rpartition(".")
        if label == p_label and tld in _TLD_TYPOS.get(p_tld, frozenset()):
            return provider

    best: str | None = None
    best_distance = 3
    for provider in _FUZZY_PROVIDERS:
        p_label, _, p_tld = provider.rpartition(".")
        if tld != p_tld:
            continue
        limit = _max_edits(p_label)
        distance = _osa_distance(label, p_label, limit)
        if distance <= limit and distance < best_distance:
            best, best_distance = provider, distance
    return best
