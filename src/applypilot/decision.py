"""Generic per-vacancy decision engine.

The scanner produces evidence (score, filters, problems, description).  This
module turns that evidence into one explicit action per vacancy/track:

``APPLY``   - send or prepare to send now;
``REVIEW``  - a human must confirm a missing fact;
``REJECT``  - a hard criterion is not met;
``APPLIED`` - the channel already has a confirmed/ambiguous application;
``BLOCKED`` - the channel cannot be automated safely.

Only policy-approved soft unknowns are auto-approved.  Personal claims are
never invented: an unknown language level, degree or skill becomes ``REVIEW``
unless the private profile contains an explicit fact that resolves it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .scoring import auto_apply_title_eligible
from .storage import AUTO_APPROVAL_REASON, Store

DECISION_ACTIONS = {"APPLY", "REVIEW", "REJECT", "APPLIED", "BLOCKED"}
TERMINAL_APPLICATION_STATES = {"APPLIED_CONFIRMED", "ALREADY_APPLIED", "UNKNOWN"}
WORKFLOW_STATES = {"NEEDS_FORM", "NEEDS_LETTER", "NEEDS_MANUAL"}

DEFAULT_DECISION_POLICY: dict[str, Any] = {
    "auto_approve": True,
    "allow_missing_salary": True,
    "allow_unknown_publication": True,
    "allow_stretch_grade": False,
    "soft_unknowns": (
        "salary:unpublished",
        "salary:missing_allowed",
        "salary:unpublished_not_verified",
        "publication:time_not_verified",
        "publication:outside_requested_window",
    ),
}


@dataclass(frozen=True)
class Decision:
    action: str
    reason_code: str
    reason: str = ""
    auto_approve: bool = False
    resolved: tuple[str, ...] = ()
    blockers: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "auto_approve": self.auto_approve,
            "resolved": list(self.resolved),
            "blockers": list(self.blockers),
        }


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _contains(term: Any, text: str) -> bool:
    value = _text(term)
    if not value:
        return False
    if re.fullmatch(r"[a-z0-9а-яё]+", value) and len(value) <= 4:
        return bool(re.search(rf"(?<![\w]){re.escape(value)}(?![\w])", text))
    return value in text


def _profile_skills(profile: Mapping[str, Any] | None) -> set[str]:
    profile = profile or {}
    skills: set[str] = set()
    professional = profile.get("professional")
    if isinstance(professional, Mapping):
        skills.update(_text(value) for value in professional.get("skills", []) if _text(value))
    skills.update(_text(value) for value in profile.get("skills", []) if _text(value))
    return skills


def _english_rank(value: Any) -> int:
    match = re.search(r"(?<![a-z])(a1|a2|b1|b2|c1|c2)(?![a-z])", _text(value))
    if not match:
        return 0
    return {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}[match.group(1)]


def _required_english(description: str, title: str) -> tuple[int, str]:
    text = f"{_text(title)} {_text(description)}"
    match = re.search(
        r"(?<![a-z])(a1|a2|b1|b2|c1|c2)(?![a-z])|"
        r"(upper[- ]intermediate|advanced|fluent|свободн\w*|продвинут\w*)",
        text,
    )
    if not match:
        return 0, ""
    token = (match.group(1) or match.group(2) or "").casefold()
    if token in {"upper-intermediate", "upper intermediate", "advanced", "fluent",
                 "свободный", "свободное", "свободно", "продвинутый"}:
        return 4, token
    return {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}.get(token, 0), token


def _salary_floor(search: Mapping[str, Any] | None) -> int:
    salary = (search or {}).get("salary") or {}
    try:
        return int(salary.get("from", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _salary_can_overlap(row: Mapping[str, Any], search: Mapping[str, Any] | None) -> bool:
    salary = row.get("salary")
    if not isinstance(salary, Mapping) or not salary:
        return False
    floor = _salary_floor(search)
    if floor <= 0:
        return True
    try:
        upper = int(salary.get("to")) if salary.get("to") is not None else None
    except (TypeError, ValueError):
        return False
    return upper is not None and upper >= floor


def resolve_problem(
    problem: str,
    row: Mapping[str, Any],
    *,
    search: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Resolve one review problem using only explicit private facts.

    Returns ``("resolved", reason)``, ``("block", reason)`` or
    ``("review", reason)``.  The caller must not turn ``review`` into an
    application.
    """
    code = str(problem or "").strip()
    search = search or {}
    profile = profile or {}
    description = str(row.get("description") or "")
    title = str(row.get("name") or "")

    if code.startswith("salary:unpublished") or code == "salary:missing_allowed":
        salary_cfg = search.get("salary") or {}
        if str(salary_cfg.get("missing", "include")) == "include":
            return "resolved", "unpublished salary is allowed by search policy"
        return "block", "salary is required by search policy"
    if code == "salary:below_or_unverified":
        if _salary_can_overlap(row, search) and str((search.get("salary") or {}).get("policy", "possible")) == "possible":
            return "resolved", "salary range overlaps the configured target"
        return "review", "salary floor is below target or has no upper bound"
    if code.startswith("salary:currency") or code in {"salary:malformed", "salary:range_unknown"}:
        return "review", "salary currency/range needs a human check"
    if code.startswith("publication:time_not_verified"):
        if int(search.get("days", 0) or 0) > 0:
            return "resolved", "active scan already observed the listing"
        return "review", "publication date is unknown"
    if code == "language:english_requirement_check":
        required, token = _required_english(description, title)
        candidate = _english_rank(profile.get("english_level"))
        if required and candidate:
            if candidate >= required:
                return "resolved", f"English {profile.get('english_level')} meets {token}"
            return "block", f"English {profile.get('english_level')} is below required {token}"
        return "review", "English requirement needs an explicit owner decision"
    if code == "education:requirement_check":
        if profile.get("has_degree") is True or str(profile.get("education") or "").strip():
            return "resolved", "degree/education is present in the private profile"
        if profile.get("has_degree") is False:
            return "block", "vacancy requires a degree not present in the profile"
        return "review", "degree requirement needs an explicit owner decision"
    if code == "stack:title_needs_review":
        skills = _profile_skills(profile)
        title_terms = [
            term for term in ("java", "rust", "php", ".net", "c++", "go", "golang")
            if _contains(term, _text(title))
        ]
        if title_terms and all(
            any(_contains(term, _text(skill)) for skill in skills)
            for term in title_terms
        ):
            return "resolved", "title stack is present in the private skills"
        return "block", "title stack is not confirmed in the private skills"
    if code == "geo:remote_not_verified":
        if row.get("is_remote") is True or _contains("удал", _text(description)) or _contains("remote", _text(description)):
            return "resolved", "remote format is explicit on the listing"
        return "review", "remote format is not verified"
    if code == "grade:seniority_check":
        if str(profile.get("max_grade") or "").casefold() in {"junior", "middle"}:
            return "block", "vacancy grade is above the configured maximum"
        years = profile.get("experience_years")
        try:
            years_value = float(years) if years is not None else 0
        except (TypeError, ValueError):
            years_value = 0
        minimum = _minimum_experience(row)
        if years_value > 0 and minimum is not None:
            if years_value >= minimum:
                return "resolved", "experience meets the listing's explicit minimum"
            return "block", "experience is below the listing's explicit minimum"
        if years_value >= 5:
            return "review", "seniority title has no verified minimum requirement"
        if bool((search.get("decision") or {}).get("allow_stretch_grade", False)):
            return "review", "stretch grade is allowed but needs an explicit confirmation"
        return "review", "seniority needs an explicit owner decision"
    if code.startswith(("role:", "hard:", "preset_noise:", "stack:title_incompatible")):
        return "block", "hard role/stack mismatch"
    if code.startswith(("description:", "channel:", "application:")):
        return "review", "listing/channel needs a human check"
    return "review", "unclassified review problem"


def _minimum_experience(row: Mapping[str, Any]) -> float | None:
    structured = str(row.get("experience") or "").casefold()
    floor = {"noexperience": 0, "between1and3": 1, "between3and6": 3,
             "morethan6": 6.01}.get(structured)
    description = re.sub(r"<[^>]*>", " ", str(row.get("description") or "")).casefold()
    required = []
    pattern = r"(?<!\d)(\d+)(?:\s*[-–—]\s*\d+)?\s*\+?\s*(?:лет|года?|years?)"
    for match in re.finditer(pattern, description):
        context = description[max(0, match.start() - 80):match.end() + 35]
        if not re.search(r"опыт|стаж|experience", context):
            continue
        if re.search(r"желатель|будет плюсом|nice.to.have|preferred", context):
            continue
        required.append(float(match.group(1)))
    values = ([floor] if floor is not None else []) + required
    return max(values) if values else None


def _soft_unknown_reasons(
    row: Mapping[str, Any],
    *,
    search: Mapping[str, Any] | None,
    policy: Mapping[str, Any],
) -> tuple[bool, list[str]]:
    reasons = {str(value) for value in row.get("filter_reasons", []) if str(value).strip()}
    if not reasons:
        return True, []
    allowed = set(policy.get("soft_unknowns", ())) | {
        "remote:verified", "work_format:matching", "experience:matching",
        "salary:matching", "salary:gross_net_unknown",
    }
    if not policy.get("allow_missing_salary", True):
        allowed.discard("salary:unpublished")
        allowed.discard("salary:missing_allowed")
        allowed.discard("salary:unpublished_not_verified")
    if not policy.get("allow_unknown_publication", True):
        allowed.discard("publication:time_not_verified")
    unknown = sorted(reason for reason in reasons if reason not in allowed and not reason.endswith(":matching"))
    return not unknown, unknown


def decide_row(
    row: Mapping[str, Any],
    *,
    search: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
) -> Decision:
    """Return one explicit action for a vacancy/track row."""
    search = search or {}
    profile = profile or {}
    merged_policy = {**DEFAULT_DECISION_POLICY, **dict(policy or {})}
    # ``list_campaign_vacancies`` exposes the track-effective state in
    # ``application_state``.  Decisions must use the raw channel state,
    # otherwise a previous REVIEW/REJECT decision can feed back into the next
    # sweep and hide an approved READY vacancy forever.
    state = str(
        row.get("global_application_state") or row.get("application_state") or "NOT_STARTED"
    ).upper()
    review_state = str(row.get("review_state") or "UNREVIEWED").upper()
    score_decision = str(row.get("score_decision") or "unknown")
    filter_status = str(row.get("filter_status") or "unknown")

    if state in TERMINAL_APPLICATION_STATES:
        return Decision("APPLIED" if state != "UNKNOWN" else "BLOCKED",
                        "already_attempted" if state != "UNKNOWN" else "unknown_quarantine",
                        "channel already used" if state != "UNKNOWN" else "ambiguous submit is quarantined")
    if row.get("applied_before") or row.get("channel_duplicate"):
        return Decision("BLOCKED", "channel_already_used", "application channel already used")
    channel_type = str(row.get("channel_type") or row.get("application_channel_type") or "").lower()
    if channel_type == "external_ats":
        return Decision("BLOCKED", "external_ats", "external ATS requires a separate verified driver")
    if review_state == "SKIP":
        return Decision("REJECT", "owner_skip", str(row.get("review_reason") or "owner skipped"))
    if review_state in {"DEFERRED", "MAYBE"}:
        return Decision("REVIEW", f"owner_{review_state.casefold()}",
                        str(row.get("review_reason") or f"owner marked {review_state}"))
    if score_decision == "maybe" and str(row.get("description_status") or "") in {
        "telegram", "missing", "provisional",
    }:
        # Telegram-only or not-yet-enriched cards must stay visible in review,
        # not disappear as rejects with score 0.
        return Decision("REVIEW", "needs_description",
                        "full description/score is missing; enrich before deciding")
    if (str(row.get("description_status") or "") in {"missing", "provisional"}
            or not str(row.get("description") or "").strip()):
        return Decision("REVIEW", "needs_description", "full description is missing")
    if score_decision not in {"pass", "reject"}:
        return Decision("REVIEW", "insufficient_role_evidence", "role duties need verification")
    if score_decision == "reject":
        return Decision("REJECT", "score_or_filter_reject",
                        "vacancy did not pass scoring/search rules")
    if filter_status == "reject":
        # A later scan window must not erase an existing human approval when
        # the only new reject reason is a permitted soft unknown (for example,
        # an old listing outside the current ``days`` window).  Unapproved
        # rows still follow the ordinary reject path.
        soft_ok, _unknown = _soft_unknown_reasons(
            row, search=search, policy=merged_policy,
        )
        if review_state != "APPROVED" or not soft_ok:
            return Decision("REJECT", "score_or_filter_reject",
                            "vacancy did not pass scoring/search rules")
    if state in WORKFLOW_STATES:
        return Decision("BLOCKED", f"workflow_{state.casefold()}",
                        f"vacancy is in {state}; resolve the workflow first")
    if review_state == "APPROVED" and row.get("review_reason") != AUTO_APPROVAL_REASON:
        if state == "READY_TO_APPLY":
            return Decision("APPLY", "owner_approved", "owner approval is valid for this track")
        return Decision("REVIEW", "approved_without_ready",
                        f"approved vacancy is in {state}; re-check preflight")

    description_status = str(row.get("description_status") or "")
    if description_status in {"missing", "provisional"} or not str(row.get("description") or "").strip():
        return Decision("REVIEW", "needs_description", "full description is missing")

    if search:
        title_eligible = auto_apply_title_eligible(
            dict(row), dict(search), _profile_skills(profile),
        )
    else:
        title_eligible = row.get("auto_apply_eligible") is not False
    if not title_eligible:
        return Decision("REVIEW", "title_requires_manual_role_review",
                        "title gate did not accept this role automatically")

    problems = [str(value) for value in row.get("problems", []) if str(value).strip()]
    resolved: list[str] = []
    blockers: list[str] = []
    review: list[str] = []
    for problem in problems:
        status, reason = resolve_problem(problem, row, search=search, profile=profile)
        if status == "resolved":
            resolved.append(f"{problem}: {reason}")
        elif status == "block":
            blockers.append(f"{problem}: {reason}")
        else:
            review.append(f"{problem}: {reason}")
    if blockers:
        return Decision("REJECT", "hard_requirement_missing",
                        "; ".join(blockers)[:500], resolved=tuple(resolved),
                        blockers=tuple(blockers))
    if review:
        return Decision("REVIEW", "needs_evidence", "; ".join(review)[:500],
                        resolved=tuple(resolved), blockers=tuple(review))

    soft_ok, unknown_reasons = _soft_unknown_reasons(row, search=search, policy=merged_policy)
    if filter_status == "unknown" and not soft_ok:
        return Decision("REVIEW", "unknown_filter",
                        "unresolved filters: " + ", ".join(unknown_reasons),
                        resolved=tuple(resolved))
    if filter_status not in {"pass", "unknown"}:
        return Decision("REVIEW", "filter_unknown", f"filter status is {filter_status}")
    if not merged_policy.get("auto_approve", True):
        return Decision("REVIEW", "auto_approve_disabled", "policy requires owner review",
                        resolved=tuple(resolved))
    return Decision("APPLY", "fit_and_conditions_verified",
                    "all search conditions are verified or allowed by policy",
                    auto_approve=True, resolved=tuple(resolved))


def sweep_track_decisions(
    store: Store,
    account: str,
    track: str,
    source: str,
    *,
    search: Mapping[str, Any] | None = None,
    profile: Mapping[str, Any] | None = None,
    policy: Mapping[str, Any] | None = None,
    limit: int | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    """Evaluate every non-terminal row in one track and auto-approve clean FITs.

    This is deliberately broader than ``approve_scanned_fits``: it revisits
    historical ``NOT_STARTED``/``READY`` rows that fell out of the latest scan
    window.  Human decisions are never overwritten.
    """
    rows = store.list_campaign_vacancies(account, track, "all", limit, source=source)
    counts: Counter[str] = Counter()
    approved: list[str] = []
    blocked: dict[str, str] = {}
    decisions: list[tuple[dict[str, Any], str, Decision]] = []
    for row in rows:
        vacancy_id = str(row.get("id") or "")
        if not vacancy_id:
            continue
        decision = decide_row(row, search=search, profile=profile, policy=policy)
        decisions.append((row, vacancy_id, decision))

    # Collapse repeated employer+title postings only among rows that would
    # actually be sent.  A terminal attempt or an owner veto must not consume
    # the group slot and hide a clean sibling.
    seen_groups: dict[str, str] = {}
    resolved: list[tuple[dict[str, Any], str, Decision]] = []
    for row, vacancy_id, decision in decisions:
        group_key = str(row.get("job_group_key") or "")
        if decision.action == "APPLY" and group_key:
            if group_key in seen_groups:
                decision = Decision(
                    "BLOCKED", "duplicate_job_group",
                    f"same employer/title as {seen_groups[group_key]}",
                )
            else:
                seen_groups[group_key] = vacancy_id
        resolved.append((row, vacancy_id, decision))

    for row, vacancy_id, decision in resolved:
        if persist:
            store.set_track_decision(
                account, source, vacancy_id, track, decision.action,
                decision.reason_code + (f": {decision.reason}" if decision.reason else ""),
            )
        counts[decision.action] += 1
        if decision.action == "APPLY" and row.get("review_state") == "UNREVIEWED":
            if decision.auto_approve:
                approved.append(vacancy_id)
            else:
                blocked[vacancy_id] = decision.reason_code
        elif decision.action == "REVIEW":
            blocked[vacancy_id] = decision.reason_code
    updated = 0
    if approved and persist:
        # Batch approval is atomic.  Fall back to per-row approval so one bad
        # row can never hide all the clean rows in the sweep.
        try:
            updated = store.set_review_state_batch(
                account, source, approved, "APPROVED",
                reason=AUTO_APPROVAL_REASON, track=track,
            )
        except ValueError:
            for vacancy_id in approved:
                try:
                    updated += store.set_review_state_batch(
                        account, source, [vacancy_id], "APPROVED",
                        reason=AUTO_APPROVAL_REASON, track=track,
                    )
                except ValueError as exc:
                    blocked[vacancy_id] = f"approval_rejected: {str(exc)[:180]}"
    return {
        "source": source,
        "track": track,
        "scanned": len(rows),
        "counts": dict(counts),
        "approved": updated,
        "review_ids": blocked,
    }


def decision_summary(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Small helper for reports and the CLI queue summary."""
    counts: Counter[str] = Counter()
    for row in rows:
        counts[str(row.get("decision") or "UNDECIDED").upper()] += 1
    return dict(counts)
