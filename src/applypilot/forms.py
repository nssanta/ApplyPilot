"""Generic screening-form reader and answer resolver.

This module does not know any particular vacancy.  It reads the form geometry
(text, textarea, number, select, radio, checkbox), resolves an answer only from
explicit private facts, and returns every unresolved question for the owner.
It never invents a skill, degree, language level or work authorization.
"""

# ruff: noqa: BLE001, S110, S112

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from .scoring import DEFAULT_SCORING

YES_WORDS = {"yes", "да", "true", "есть", "имею", "готов", "готова", "согласен", "согласна"}
NO_WORDS = {"no", "нет", "false", "не имею", "не готов", "не готова", "не согласен", "не согласна"}

ANSWER_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("salary", ("зарплат", "доход", "оклад", "compensation", "salary")),
    ("english", ("english", "английск", "уровень владения языком")),
    ("motivation", ("почему", "мотивац", "why", "заинтерес", "interest")),
    ("citizenship", ("гражданств", "citizenship")),
    ("work_authorization", ("разрешение на работу", "work authorization", "право на работу")),
    ("relocation", ("релокац", "переезд", "relocation")),
    ("start_date", ("когда можете начать", "start date", "дата выхода", "notice period")),
    ("remote", ("удален", "удалён", "remote")),
    ("experience_years", ("сколько лет", "years of experience", "опыт работы", "стаж")),
    ("education", ("образование", "degree", "education")),
    ("current_location", ("город", "location", "место жительства")),
)

TECH_TERMS = {
    str(value).casefold()
    for group in ("hot", "high", "medium")
    for value in DEFAULT_SCORING.get(group, ())
} | {
    "kubernetes", "k8s", "python", "java", "golang", "go", "typescript",
    "javascript", "react", "docker", "postgresql", "mongodb", "redis",
}

FACT_KEYS = {
    "salary", "english", "english_level", "motivation", "citizenship",
    "work_authorization", "relocation", "start_date", "remote",
    "experience_years", "education", "current_location", "skills",
}


@dataclass(frozen=True)
class FormField:
    field_type: str
    question: str
    options: tuple[str, ...] = ()
    required: bool = False
    name: str = ""
    locator: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class FormFillResult:
    resolved: bool
    unresolved: tuple[str, ...] = ()
    filled: tuple[str, ...] = ()
    fields: tuple[FormField, ...] = ()


def normalize(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").casefold()).strip()


def _mentions(term: str, text: str) -> bool:
    value = normalize(term)
    if not value:
        return False
    if re.fullmatch(r"[a-z0-9а-яё]+", value) and len(value) <= 4:
        return bool(re.search(rf"(?<![\w]){re.escape(value)}(?![\w])", text))
    return value in text


def _option_text(locator: Any) -> str:
    for attribute in ("aria-label", "value"):
        try:
            value = locator.get_attribute(attribute)
        except Exception:
            value = None
        if value and str(value).strip():
            return str(value).strip()
    try:
        parent = locator.locator("xpath=..")
        if parent.count():
            text = " ".join(parent.first.inner_text(timeout=1000).split())
            if text:
                return text
    except Exception:
        pass
    try:
        return " ".join(str(locator.inner_text(timeout=1000)).split())
    except Exception:
        return ""


def _is_required(locator: Any) -> bool:
    try:
        required = locator.get_attribute("required")
        if required is not None:
            return True
    except Exception:
        pass
    try:
        return bool(locator.get_attribute("aria-required") == "true")
    except Exception:
        return False


def _field_from_locator(locator: Any, field_type: str, question: str,
                        options: Iterable[str] = ()) -> FormField:
    name = ""
    try:
        name = str(locator.get_attribute("name") or "")
    except Exception:
        pass
    return FormField(
        field_type=field_type, question=question,
        options=tuple(dict.fromkeys(str(value) for value in options if str(value).strip())),
        required=_is_required(locator), name=name, locator=locator,
    )


def extract_fields(root: Any) -> list[FormField]:
    """Read HH-style screening task blocks into a source-agnostic field list."""
    try:
        tasks = root.locator("[data-qa='task-body']")
        task_count = tasks.count()
    except Exception:
        return []
    fields: list[FormField] = []
    for index in range(task_count):
        task = tasks.nth(index)
        try:
            question = " ".join(task.inner_text(timeout=1000).split())
        except Exception:
            question = f"question {index + 1}"
        seen_names: set[str] = set()
        for field_type, selector in (
            ("radio", "input[type='radio']"),
            ("checkbox", "input[type='checkbox']"),
            ("select", "select"),
            ("textarea", "textarea"),
            ("number", "input[type='number']"),
            ("text", "input[type='text']"),
            ("text", "input:not([type])"),
        ):
            try:
                found = task.locator(selector)
                count = found.count()
            except Exception:
                continue
            for position in range(count):
                locator = found.nth(position)
                try:
                    if not locator.is_visible(timeout=1000):
                        continue
                except Exception:
                    pass
                name = ""
                try:
                    name = str(locator.get_attribute("name") or "")
                except Exception:
                    pass
                if name and name in seen_names:
                    continue
                if name:
                    seen_names.add(name)
                options: list[str] = []
                if field_type in {"radio", "checkbox"}:
                    option = _option_text(locator)
                    if option:
                        options.append(option)
                elif field_type == "select":
                    try:
                        option_locator = locator.locator("option")
                        options = [
                            " ".join(option_locator.nth(i).inner_text(timeout=500).split())
                            for i in range(option_locator.count())
                        ]
                    except Exception:
                        options = []
                fields.append(_field_from_locator(locator, field_type, question, options))
    # Collapse radio/checkbox controls that share a group name into one
    # answerable field with all options and locators.
    consolidated: list[FormField] = []
    grouped: dict[tuple[str, str, str], FormField] = {}
    for candidate_field in fields:
        if candidate_field.field_type not in {"radio", "checkbox"}:
            consolidated.append(candidate_field)
            continue
        key = (candidate_field.question, candidate_field.name or candidate_field.question,
               candidate_field.field_type)
        current = grouped.get(key)
        if current is None:
            grouped[key] = candidate_field
            consolidated.append(candidate_field)
            continue
        index = consolidated.index(current)
        locators = current.locator if isinstance(current.locator, list) else [current.locator]
        if candidate_field.locator is not None:
            locators = [*locators, candidate_field.locator]
        merged = FormField(
            field_type=current.field_type, question=current.question,
            options=tuple(dict.fromkeys([*current.options, *candidate_field.options])),
            required=current.required or candidate_field.required, name=current.name,
            locator=locators,
        )
        grouped[key] = merged
        consolidated[index] = merged
    return consolidated


def _pick_option(answer: str, options: Iterable[str]) -> str:
    answer_norm = normalize(answer)
    if not answer_norm:
        return ""
    option_list = [str(value) for value in options if str(value).strip()]
    for option in option_list:
        if normalize(option) == answer_norm:
            return option
    for option in option_list:
        if answer_norm in normalize(option) or normalize(option) in answer_norm:
            return option
    if answer_norm in YES_WORDS:
        for option in option_list:
            if normalize(option) in YES_WORDS:
                return option
    if answer_norm in NO_WORDS:
        for option in option_list:
            if normalize(option) in NO_WORDS:
                return option
    return ""


def _fact(answers: Mapping[str, Any], facts: Mapping[str, Any], key: str) -> str:
    value = facts.get(key)
    if value is None:
        value = answers.get(key)
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value if str(item).strip())
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value or "").strip()


def answer_question(question: str, field_type: str, options: Iterable[str],
                    answers: Mapping[str, Any] | None = None,
                    facts: Mapping[str, Any] | None = None) -> str:
    """Return an explicit answer or an empty string for owner review."""
    answers = answers or {}
    facts = facts or {}
    text = normalize(question)

    skills = facts.get("skills") or answers.get("skills") or []
    if isinstance(skills, str):
        skills = [value.strip() for value in re.split(r"[,;/]", skills) if value.strip()]
    skill_mentions = [str(skill) for skill in skills if _mentions(str(skill), text)]
    tech_mentions = [term for term in TECH_TERMS if _mentions(term, text)]
    if any(token in text for token in ("опыт", "experience", "стаж", "years")):
        # A named technology is only answerable when it is present in the
        # private skill list.  Do not turn "3 years of Kubernetes" into the
        # general experience number.
        if tech_mentions and not skill_mentions:
            return ""
        if skill_mentions:
            if field_type in {"radio", "select"}:
                return _pick_option(_fact(answers, facts, "yes") or "yes", options)
            if field_type == "checkbox":
                return "yes"
            # A concrete number of years for a named skill is only valid when
            # the private profile has a skill-specific fact.  The general
            # experience number must not be silently reused.
            if any(token in text for token in ("сколько лет", "how many years")):
                skill_years = facts.get("skill_years") or answers.get("skill_years") or {}
                if isinstance(skill_years, Mapping):
                    for skill in skill_mentions:
                        value = skill_years.get(skill) or skill_years.get(skill.casefold())
                        if value:
                            return str(value)
                return ""
            return ""

    for key, aliases in ANSWER_ALIASES:
        if any(alias in text for alias in aliases):
            value = _fact(answers, facts, key)
            if value:
                if field_type in {"radio", "select"}:
                    return _pick_option(value, options)
                if field_type == "checkbox":
                    return value
                return value

    skills = facts.get("skills") or answers.get("skills") or []
    if isinstance(skills, str):
        skills = [value.strip() for value in re.split(r"[,;/]", skills) if value.strip()]
    for skill in skills:
        if normalize(skill) and normalize(skill) in text:
            # A skill named in the question is present in the private evidence.
            if field_type in {"radio", "select"}:
                answer = _fact(answers, facts, "yes")
                return _pick_option(answer or "yes", options)
            if field_type == "checkbox":
                return "yes"
            return _fact(answers, facts, "experience_years") or _fact(answers, facts, "yes")
    if any(value in text for value in ("опыт", "experience", "стаж", "years")):
        years = _fact(answers, facts, "experience_years")
        if years:
            if field_type in {"text", "textarea", "number"}:
                return years
            return _pick_option(years, options)
    return ""


def _fill_field(field: FormField, answer: str) -> bool:
    if not answer:
        return False
    locator = field.locator
    if locator is None:
        return False
    try:
        if field.field_type in {"radio", "checkbox"}:
            option = _pick_option(answer, field.options)
            if not option:
                return False
            locators = locator if isinstance(locator, (list, tuple)) else [locator]
            for candidate in locators:
                if normalize(_option_text(candidate)) == normalize(option):
                    candidate.click(timeout=8000)
                    return True
            return False
        if field.field_type == "select":
            option = _pick_option(answer, field.options)
            if not option:
                return False
            try:
                locator.select_option(label=option, timeout=8000)
            except Exception:
                locator.select_option(value=option, timeout=8000)
            return True
        locator.fill(answer, timeout=8000)
        return True
    except Exception:
        return False


def fill_screening_form(root: Any, answers: Mapping[str, Any] | None,
                        facts: Mapping[str, Any] | None = None) -> FormFillResult:
    fields = extract_fields(root)
    if not fields:
        return FormFillResult(False, ("screening questions could not be read",))
    unresolved: list[str] = []
    filled: list[str] = []
    for candidate_field in fields:
        answer = answer_question(
            candidate_field.question, candidate_field.field_type,
            candidate_field.options, answers, facts,
        )
        if not answer or not _fill_field(candidate_field, answer):
            suffix = f" [{candidate_field.field_type}"
            if candidate_field.options:
                suffix += ": " + " / ".join(candidate_field.options[:8])
            suffix += "]"
            unresolved.append(f"{candidate_field.question[:240]}{suffix}")
        else:
            filled.append(candidate_field.question[:120])
    return FormFillResult(
        resolved=not unresolved,
        unresolved=tuple(unresolved),
        filled=tuple(filled),
        fields=tuple(fields),
    )
