"""Mining Match - candidate/job matching engine (Python).

Pure functions over plain dicts (JSON-friendly) so the same shapes can later
come straight from the data layer. Mirrors js/matchingEngine.js; both are
tested against fixtures/match_cases.json so scores stay identical.

Scoring in one paragraph
------------------------
Five components, each scored 0..1: licences, qualifications, skills,
experience, location. A component is skipped when the job has no requirements
for it, and the remaining weights are renormalised. "Required" items count
1.0, "preferred" items count 0.5. The overall score is 0..100.

Eligibility is separate from score: a candidate is NOT eligible if any
required licence is missing/expired on the job start date, or any required
qualification is missing. Ranking puts eligible candidates first, then
sorts by score.
"""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Optional

__all__ = [
    "DEFAULT_WEIGHTS",
    "MatchResult",
    "normalize",
    "register_aliases",
    "score_candidate",
    "rank_candidates",
    "match_jobs_for_candidate",
]

DEFAULT_WEIGHTS = {
    "licences": 0.30,
    "qualifications": 0.20,
    "skills": 0.25,
    "experience": 0.20,
    "location": 0.05,
}
EXPIRY_WARNING_DAYS = 90  # valid but expires within this many days of start
PREFERRED_FACTOR = 0.5  # preferred items count half as much as required
RELOCATION_FACTOR = 0.7  # location score if willing to relocate / FIFO

# Lower-case alias -> canonical name. Extend with register_aliases().
ALIASES = {
    "ss11": "standard 11",
    "s11": "standard 11",
    "hr": "heavy rigid licence",
    "hr licence": "heavy rigid licence",
    "heavy rigid": "heavy rigid licence",
    "cbm": "coal board medical",
    "wah": "working at heights",
}

_NO_EXPIRY = date.max


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def normalize(text: Any) -> str:
    t = re.sub(r"\s+", " ", str(text or "").strip().lower())
    t = t.replace("license", "licence")
    return ALIASES.get(t, t)


def register_aliases(mapping: dict) -> None:
    """Add aliases, e.g. {"cert iii": "certificate iii"}. Keys are normalised."""
    for alias, canonical in mapping.items():
        key = re.sub(r"\s+", " ", str(alias).strip().lower()).replace("license", "licence")
        ALIASES[key] = re.sub(r"\s+", " ", str(canonical).strip().lower()).replace(
            "license", "licence"
        )


def _to_date(value: Any) -> Optional[date]:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _round1(x: float) -> float:
    return math.floor(x * 10 + 0.5) / 10


def _num(n: float) -> str:
    return format(n, "g")


def _weighted(parts: list) -> Optional[float]:
    total = sum(w for w, _ in parts)
    return None if total == 0 else sum(w * v for w, v in parts) / total


def _tiers(job: dict, required_key: str, preferred_key: str):
    for item in job.get(required_key) or []:
        yield item, True
    for item in job.get(preferred_key) or []:
        yield item, False


def _tier_weight(required: bool) -> float:
    return 1.0 if required else PREFERRED_FACTOR


# A component returns (score or None, blockers, gaps, warnings)
Component = tuple


# --------------------------------------------------------------------------
# Components
# --------------------------------------------------------------------------
def _held_licences(candidate: dict) -> dict:
    held: dict = {}
    for item in candidate.get("licences") or []:
        if isinstance(item, dict):
            name = item.get("type") or item.get("name")
            expiry = _to_date(item.get("expiry")) or _NO_EXPIRY
        else:
            name, expiry = item, _NO_EXPIRY
        key = normalize(name)
        held[key] = max(held.get(key, date.min), expiry)
    return held


def _licence_status(held: dict, key: str, ref: date):
    expiry = held.get(key)
    if expiry is None:
        return "missing", None
    if expiry < ref:
        return "expired", expiry
    if expiry != _NO_EXPIRY and (expiry - ref).days <= EXPIRY_WARNING_DAYS:
        return "expiring", expiry
    return "valid", expiry


def _score_licences(candidate: dict, job: dict, ref: date) -> Component:
    held = _held_licences(candidate)
    parts, blockers, gaps, warnings = [], [], [], []
    for item, required in _tiers(job, "required_licences", "preferred_licences"):
        status, expiry = _licence_status(held, normalize(item), ref)
        ok = status in ("valid", "expiring")
        parts.append((_tier_weight(required), 1.0 if ok else 0.0))
        if status == "expiring":
            warnings.append(f"{item} expires {expiry.isoformat()}")
        if not ok:
            reason = "missing" if status == "missing" else f"expired {expiry.isoformat()}"
            if required:
                blockers.append(f"{item}: {reason}")
            else:
                gaps.append(f"{item} (preferred): {reason}")
    return _weighted(parts), blockers, gaps, warnings


def _score_qualifications(candidate: dict, job: dict) -> Component:
    held = {
        normalize(q.get("name") if isinstance(q, dict) else q)
        for q in candidate.get("qualifications") or []
    }
    parts, blockers, gaps = [], [], []
    for item, required in _tiers(job, "required_qualifications", "preferred_qualifications"):
        ok = normalize(item) in held
        parts.append((_tier_weight(required), 1.0 if ok else 0.0))
        if not ok:
            if required:
                blockers.append(f"{item}: missing")
            else:
                gaps.append(f"{item} (preferred): missing")
    return _weighted(parts), blockers, gaps, []


def _held_skills(candidate: dict) -> dict:
    skills: dict = {}
    for item in candidate.get("skills") or []:
        if isinstance(item, dict):
            name, years = item.get("name"), item.get("years")
        else:
            name, years = item, None
        key = normalize(name)
        if key not in skills or (years is not None and (skills[key] is None or years > skills[key])):
            skills[key] = years
    return skills


def _score_skills(candidate: dict, job: dict) -> Component:
    held = _held_skills(candidate)
    parts, gaps = [], []
    for item, required in _tiers(job, "required_skills", "preferred_skills"):
        if isinstance(item, dict):
            name, min_years = item.get("name"), item.get("min_years")
        else:
            name, min_years = item, None
        key = normalize(name)
        label = name if required else f"{name} (preferred)"
        if key not in held:
            value = 0.0
            gaps.append(f"{label}: missing")
        else:
            years = held[key]
            # Unknown years (plain skill name) gets full credit for presence.
            value = 1.0 if (years is None or not min_years) else min(1.0, years / min_years)
            if value < 1.0:
                gaps.append(f"{label}: {_num(years)}/{_num(min_years)} yrs")
        parts.append((_tier_weight(required), value))
    return _weighted(parts), [], gaps, []


def _score_experience(candidate: dict, job: dict) -> Component:
    min_years = job.get("min_experience_years")
    if not min_years:
        return None, [], [], []
    roles = {normalize(r) for r in job.get("relevant_roles") or []}
    total = 0.0
    for entry in candidate.get("experience") or []:
        if not roles or normalize(entry.get("role")) in roles:
            total += float(entry.get("years") or 0)
    value = min(1.0, total / min_years)
    gaps = [f"experience: {_num(total)}/{_num(min_years)} yrs relevant"] if value < 1.0 else []
    return value, [], gaps, []


def _score_location(candidate: dict, job: dict) -> Component:
    want = normalize(job.get("location"))
    if not want:
        return None, [], [], []
    have = normalize(candidate.get("location"))
    if have == want:
        return 1.0, [], [], []
    if candidate.get("willing_to_relocate") or candidate.get("willing_fifo"):
        return RELOCATION_FACTOR, [], [], ["relocation or FIFO required"]
    shown = candidate.get("location") or "unknown"
    return 0.0, [], [f"location: {shown} (job in {job.get('location')})"], []


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
@dataclass
class MatchResult:
    candidate_id: Any
    job_id: Any
    score: float  # 0..100
    eligible: bool  # False if any required licence/qualification is not met
    breakdown: dict  # component -> 0..100 (None if not scored for this job)
    blockers: list = field(default_factory=list)  # why not eligible
    gaps: list = field(default_factory=list)  # unmet but non-blocking
    warnings: list = field(default_factory=list)  # e.g. licence expiring soon

    def to_dict(self) -> dict:
        return asdict(self)


def score_candidate(
    candidate: dict,
    job: dict,
    weights: Optional[dict] = None,
    as_of: Any = None,
) -> MatchResult:
    """Score one candidate against one job.

    Licence validity is judged on the job's start_date (falling back to
    `as_of`, then today) so a licence that lapses before the start doesn't count.
    """
    w = {**DEFAULT_WEIGHTS, **(weights or {})}
    for key, value in w.items():
        if key not in DEFAULT_WEIGHTS:
            raise ValueError(f"Unknown weight: {key}")
        if not value >= 0:
            raise ValueError("Weights must be >= 0")

    ref = _to_date(job.get("start_date")) or _to_date(as_of) or date.today()
    results = {
        "licences": _score_licences(candidate, job, ref),
        "qualifications": _score_qualifications(candidate, job),
        "skills": _score_skills(candidate, job),
        "experience": _score_experience(candidate, job),
        "location": _score_location(candidate, job),
    }

    active = {k: r[0] for k, r in results.items() if r[0] is not None and w[k] > 0}
    total_weight = sum(w[k] for k in active)
    overall = 100 * sum(w[k] * s for k, s in active.items()) / total_weight if total_weight else 0.0

    blockers = [b for r in results.values() for b in r[1]]
    gaps = [g for r in results.values() for g in r[2]]
    warnings = [x for r in results.values() for x in r[3]]
    if not active:
        warnings.append("job has no scorable requirements")

    return MatchResult(
        candidate_id=candidate.get("id"),
        job_id=job.get("id"),
        score=_round1(overall),
        eligible=not blockers,
        breakdown={k: None if r[0] is None else _round1(r[0] * 100) for k, r in results.items()},
        blockers=blockers,
        gaps=gaps,
        warnings=warnings,
    )


def rank_candidates(
    candidates: list,
    job: dict,
    weights: Optional[dict] = None,
    as_of: Any = None,
    min_score: float = 0.0,
    limit: Optional[int] = None,
    eligible_only: bool = False,
) -> list:
    """Rank candidates for a job: eligible first, then score, then name."""
    scored = [(c, score_candidate(c, job, weights, as_of)) for c in candidates]
    scored = [(c, r) for c, r in scored if r.score >= min_score and (r.eligible or not eligible_only)]
    scored.sort(
        key=lambda cr: (
            not cr[1].eligible,
            -cr[1].score,
            str(cr[0].get("name") or "").lower(),
            str(cr[1].candidate_id),
        )
    )
    results = [r for _, r in scored]
    return results if limit is None else results[:limit]


def match_jobs_for_candidate(
    candidate: dict,
    jobs: list,
    weights: Optional[dict] = None,
    as_of: Any = None,
    min_score: float = 0.0,
    limit: Optional[int] = None,
    eligible_only: bool = False,
) -> list:
    """Rank jobs for a candidate: eligible first, then score, then job title."""
    scored = [(j, score_candidate(candidate, j, weights, as_of)) for j in jobs]
    scored = [(j, r) for j, r in scored if r.score >= min_score and (r.eligible or not eligible_only)]
    scored.sort(
        key=lambda jr: (
            not jr[1].eligible,
            -jr[1].score,
            str(jr[0].get("title") or "").lower(),
            str(jr[1].job_id),
        )
    )
    results = [r for _, r in scored]
    return results if limit is None else results[:limit]
