from applypilot.providers.yandex import YandexProvider


def test_yandex_retains_mandatory_list_requirements_and_experience():
    html = """<main><div class="CareerHeader_example"><h1>LLM Engineer</h1>
    <span>От 5 лет</span></div><p>Build assistants.</p>
    <h2>Requirements</h2><ul><li>Production Kubernetes</li>
    <li><p>Model fine-tuning required</p></li></ul></main>"""
    provider = YandexProvider()
    row = provider._parse_detail(html, "https://yandex.ru/jobs/vacancies/12345")
    assert row["native_id"] == "12345"
    assert row["experience"] == "От 5 лет"
    assert "Production Kubernetes" in row["description"]
    assert row["description"].count("Model fine-tuning required") == 1
    assert "От 5 лет" in row["description"]
    assert provider._native_id("https://yandex.ru/jobs/vacancies/llm-role-12345") == "12345"
    assert provider._native_id("https://yandex.ru/jobs/other-12345") == ""


def test_yandex_scan_enrichment_uses_remaining_budget_and_keeps_listings():
    from pathlib import Path
    from types import SimpleNamespace

    listing = (Path(__file__).parent / "fixtures/public/yandex_genai_remote_list.html").read_text()

    class Session:
        def __init__(self):
            self.urls = []

        def get(self, url, **kwargs):
            self.urls.append(url)
            if '?' in url:
                text = listing
            elif url.endswith('3001'):
                text = '<main><h1>AI Engineer</h1><p>Build assistants</p><ul><li>Required Kubernetes</li></ul></main>'
            else:
                return SimpleNamespace(status_code=429, text='limited')
            return SimpleNamespace(status_code=200, text=text)

    session = Session()
    items, segments = YandexProvider().discover({
        'session': session, 'request_budget': 3, 'details_limit': 3,
    })
    assert len(session.urls) == 3
    assert len(items) == 3
    assert 'Required Kubernetes' in items[0]['description']
    assert items[0]['description_status'] == 'ok'
    assert items[1]['description_status'] == 'snippet'
    assert segments[-1].status == 'truncated'
    assert sum(s.requests for s in segments) == 3

    failed = Session()
    items, segments = YandexProvider().discover({
        'session': failed, 'request_budget': 5, 'details_limit': 3,
    })
    assert len(items) == 3
    assert segments[-1].status == 'partial'


def test_redirects_consume_budget_and_cannot_leave_yandex():
    from types import SimpleNamespace

    from applypilot.providers.yandex import _Budget, _fetch

    class Redirects:
        def __init__(self, location):
            self.location = location
            self.calls = 0

        def get(self, url, **kwargs):
            assert kwargs['allow_redirects'] is False
            self.calls += 1
            return SimpleNamespace(status_code=302, headers={'Location': self.location}, text='')

    session = Redirects('/jobs/vacancies/role-12345')
    budget = _Budget(2)
    assert budget.take()
    _, status, _, used = _fetch({'session': session}, 'https://yandex.ru/jobs/vacancies/12345', 1, budget)
    assert status == 'truncated'
    assert used == session.calls == budget.used == 2

    external = Redirects('https://untrusted.example/apply')
    _, status, _, used = _fetch({'session': external}, 'https://yandex.ru/jobs/vacancies/12345', 1)
    assert status == 'failed'
    assert used == external.calls == 1
