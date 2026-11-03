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
    with store.connect() as conn:
        assert conn.execute("SELECT status FROM attempts WHERE vacancy_id='7'").fetchone()[0] == "prepared"
    assert store.reopen_form("acc", "7") is False


def test_day_outcomes_count_unique_vacancies_per_status(tmp_path):
    from applypilot.storage import Store, now

    store = Store(tmp_path / "state.sqlite3")
    for vacancy_id, status in (("1", "success"), ("2", "success"), ("3", "unknown")):
        store.record({"id": vacancy_id, "name": "Python", "url": f"https://hh.ru/vacancy/{vacancy_id}"},
                     status, "", "run", "acc")
    day = now()[:10]
    assert store.day_outcomes(day) == {"hh.ru": {"success": 2, "unknown": 1}}
    assert store.day_outcomes("2000-01-01") == {}


def test_contact_accreditation_and_employment_questions_use_private_answers():
    answers = {"telegram": "@handle", "it_accreditation": "Нет, не критична.",
               "employment_type": "Подходит оформление по ТК РФ."}
    assert answer_question("Укажите, пожалуйста, ваш ник в TG", "textarea", (), answers, {}) == "@handle"
    assert answer_question("Критична ли для вас IT-аккредитация?", "radio", ["Да", "Нет"], answers, {}) == "Нет"
    assert answer_question("Подходит ли вам оформление по ТК РФ?", "textarea", (), answers, {}) == (
        "Подходит оформление по ТК РФ.")


def test_hh_radio_group_shares_name_and_reads_label_text():
    """Live HH markup: <label data-qa=cell><input type=radio name=task_1 value=307900452>…Да</label>."""

    class Many:
        def __init__(self, values):
            self.values = values

        @property
        def first(self):
            return self.values[0]

        def count(self):
            return len(self.values)

        def nth(self, index):
            return self.values[index]

    class Label:
        def __init__(self, text):
            self.text, self.clicks = text, 0

        def inner_text(self, timeout=None):
            return self.text

        def click(self, timeout=None):
            self.clicks += 1

    class Radio:
        def __init__(self, value, text):
            self.value, self.label = value, Label(text)

        def get_attribute(self, name):
            return {"name": "task_1", "value": self.value}.get(name)

        def is_visible(self, timeout=None):
            return True

        def locator(self, selector):
            assert selector == "xpath=ancestor::label[1]"
            return Many([self.label])

    class Task:
        radios = (Radio("307900452", "Да"), Radio("307900453", "Нет"), Radio("open", "Свой вариант"))

        def inner_text(self, timeout=None):
            return "Критична ли для вас IT-аккредитация? Да Нет Свой вариант"

        def locator(self, selector):
            if selector == "[data-qa='task-question']":
                return Many([type("Q", (), {"inner_text": lambda self, timeout=None:
                                            "Критична ли для вас IT-аккредитация?"})()])
            return Many(self.radios if selector == "input[type='radio']" else [])

    class Root:
        def locator(self, selector):
            assert selector == "[data-qa='task-body']"
            return Many([Task()])

    root = Root()
    result = fill_screening_form(root, {"it_accreditation": "Нет, не критична."}, {})
    assert result.resolved
    assert result.fields[0].question == "Критична ли для вас IT-аккредитация?"
    assert result.fields[0].options == ("Да", "Нет", "Свой вариант")
    assert [radio.label.clicks for radio in Task.radios] == [0, 1, 0]


def test_reopen_form_restores_ready_for_an_already_approved_vacancy(tmp_path):
    from applypilot.storage import Store

    store = Store(tmp_path / "state.sqlite3")
    store.start_campaign("c", "acc", "ai", {"query": "AI"})
    store.save_campaign_items("c", "acc", "ai", [{"id": "8", "name": "Python", "company": "X",
                                                 "url": "https://hh.ru/vacancy/8"}],
                              status="ok", snapshot_path="")
    with store.connect() as conn:
        conn.execute("UPDATE vacancy_track_scores SET review_state='APPROVED' WHERE vacancy_id='8'")
    store.record({"id": "8", "name": "Python", "url": "https://hh.ru/vacancy/8"}, "needs_manual",
                 "unanswered screening questions: Город? [textarea]", "run", "acc")
    assert store.reopen_form("acc", "8") is True
    with store.connect() as conn:
        state = conn.execute("SELECT application_state FROM vacancies WHERE vacancy_id='8'").fetchone()[0]
    assert state == "READY_TO_APPLY"
