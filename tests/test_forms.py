from __future__ import annotations

from applypilot.forms import answer_question, fill_screening_form


def test_answers_use_explicit_facts_and_leave_unknown_personal_questions_empty():
    answers = {"salary": "140000", "english": "A2", "motivation": "AI products"}
    facts = {"experience_years": 3, "skills": ["Python", "Go"]}

    assert answer_question("Ваши зарплатные ожидания?", "text", (), answers, facts) == "140000"
    assert answer_question("English level?", "text", (), answers, facts) == "A2"
    assert answer_question("Why this role?", "text", (), answers, facts) == "AI products"
    assert answer_question("How many years of experience?", "number", (), answers, facts) == "3"
    assert answer_question("Do you have Python experience?", "radio", ["Да", "Нет"], answers, facts) == "Да"
    assert answer_question("How many years of Kubernetes experience?", "number", (), answers, facts) == ""
    assert answer_question("How many years of Python experience?", "number", (), answers, facts) == ""
    assert answer_question("Гражданство?", "text", (), answers, facts) == ""


def test_radio_group_is_collapsed_and_one_option_is_clicked():
    class Field:
        def __init__(self, label):
            self.label = label
            self.clicks = 0

        def get_attribute(self, name):
            return self.label if name in {"aria-label", "value"} else None

        def is_visible(self, timeout=None):
            return True

        def click(self, timeout=None):
            self.clicks += 1

        def locator(self, selector):
            raise AssertionError(selector)

        def inner_text(self, timeout=None):
            return self.label

    class Fields:
        def __init__(self, values):
            self.values = values

        def count(self):
            return len(self.values)

        def nth(self, index):
            return self.values[index]

    class Task:
        def __init__(self):
            self.radio = [Field("Да"), Field("Нет")]

        def inner_text(self, timeout=None):
            return "Do you have Python experience?"

        def locator(self, selector):
            if selector == "input[type='radio']":
                return Fields(self.radio)
            return Fields([])

    class Root:
        def __init__(self):
            self.task = Task()

        def locator(self, selector):
            if selector == "[data-qa='task-body']":
                return Fields([self.task])
            return Fields([])

    root = Root()
    result = fill_screening_form(root, {}, {"skills": ["Python"]})
    assert result.resolved
    assert [field.clicks for field in root.task.radio] == [1, 0]


def test_text_field_is_filled_from_fact_and_unknown_is_reported():
    class Field:
        def __init__(self):
            self.value = ""

        def get_attribute(self, name):
            return "answer" if name == "name" else None

        def is_visible(self, timeout=None):
            return True

        def fill(self, value, timeout=None):
            self.value = value

        def locator(self, selector):
            raise AssertionError(selector)

        def inner_text(self, timeout=None):
            return ""

    class Fields:
        def __init__(self, values):
            self.values = values

        def count(self):
            return len(self.values)

        def nth(self, index):
            return self.values[index]

    class Task:
        def __init__(self):
            self.field = Field()

        def inner_text(self, timeout=None):
            return "How many years of experience?"

        def locator(self, selector):
            if selector == "input[type='number']":
                return Fields([self.field])
            return Fields([])

    class Root:
        def __init__(self):
            self.task = Task()

        def locator(self, selector):
            if selector == "[data-qa='task-body']":
                return Fields([self.task])
            return Fields([])

    root = Root()
    result = fill_screening_form(root, {}, {"experience_years": 3})
    assert result.resolved
    assert root.task.field.value == "3"


def test_operator_answers_for_one_vacancy_win_and_pick_radio_options(tmp_path):
    from applypilot.forms import load_screening_answers

    (tmp_path / "screening-answers.json").write_text(
        '{"42": {"находитесь в РФ": "Да, нахожусь в РФ", "Почему вы": "Своими словами."}}',
        encoding="utf-8",
    )
    per_vacancy = load_screening_answers(tmp_path)
    answers = {"motivation": "generic", "_by_question": per_vacancy["42"]}
    options = ["Да, нахожусь в РФ и собираюсь оставаться здесь", "Нет, нахожусь зарубежом"]
    assert answer_question("Скажите, вы сейчас находитесь в РФ?", "radio", options, answers, {}) == options[0]
    assert answer_question("Почему вы выбрали нас?", "textarea", (), answers, {}) == "Своими словами."
    assert load_screening_answers(tmp_path / "missing") == {}


def test_reopen_form_only_for_never_submitted_needs_form(tmp_path):
    from applypilot.storage import Store

    store = Store(tmp_path / "state.sqlite3")
    store.start_campaign("c", "acc", "ai", {"query": "AI"})
    store.save_campaign_items("c", "acc", "ai", [{"id": "7", "name": "Python", "company": "X",
                                                      "url": "https://hh.ru/vacancy/7"}],
                              status="ok", snapshot_path="")
    item = {"id": "7", "name": "Python", "company": "X", "url": "https://hh.ru/vacancy/7", "score": 50}
    store.record(item, "needs_manual", "unanswered screening questions: Город? [textarea]; Формат? [textarea]",
                 "run", "acc")
    forms = store.pending_forms("acc")
    assert [form["questions"] for form in forms] == [["Город? [textarea]", "Формат? [textarea]"]]
    assert store.reopen_form("acc", "7") is True
    assert store.pending_forms("acc") == []
    assert store.reopen_form("acc", "7") is False
