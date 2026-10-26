"""Mass AI queue defaults must not silently revert to pilot-sized batches."""

from applypilot.cli import build_parser


def test_go_defaults_to_all_sources_and_full_cycle() -> None:
    args = build_parser().parse_args(["go", "--track", "ai_core"])
    assert args.sources == "all"
    assert args.scan_only is False
    assert args.once is False
    assert args.visible is False
    assert args.days == 30
    assert args.max_cycles == 100
    assert args.max_passes == 1


def test_go_accepts_ad_hoc_query_and_target() -> None:
    args = build_parser().parse_args([
        "go", "--query", "AI агент", "--query", "LLM engineer",
        "--salary-from", "120000", "--remote", "--target", "50",
        "--sources", "hh.ru,career.habr.com",
    ])
    assert args.query == ["AI агент", "LLM engineer"]
    assert args.salary_from == 120000
    assert args.remote is True
    assert args.target == 50
    assert args.sources == "hh.ru,career.habr.com"


def test_queue_links_parser() -> None:
    args = build_parser().parse_args([
        "queue", "links", "--track", "ai_core", "--source", "dreamjob.ru", "--limit", "5",
    ])
    assert args.queue_action == "links"
    assert args.source == "dreamjob.ru"
    assert args.limit == 5


def test_go_can_be_scan_only_or_visible() -> None:
    scan = build_parser().parse_args(["go", "--scan-only", "--sources", "hh.ru"])
    assert scan.scan_only is True
    assert scan.sources == "hh.ru"
    visible = build_parser().parse_args(["go", "--visible", "--once"])
    assert visible.visible is True
    assert visible.once is True


def test_queue_apply_defaults_to_bulk_limit() -> None:
    args = build_parser().parse_args(["queue", "apply", "--track", "ai_core", "--dry-run"])
    assert args.limit == 100


def test_queue_apply_accepts_explicit_bulk_limit() -> None:
    args = build_parser().parse_args([
        "queue", "apply", "--track", "ai_core", "--run", "--visible", "--limit", "100",
    ])
    assert args.limit == 100
    assert args.visible is True


def test_queue_apply_accepts_exact_ids_for_a_single_hh_pilot() -> None:
    args = build_parser().parse_args([
        "queue", "apply", "--track", "ai", "--source", "hh.ru",
        "--id", "123", "--id", "456", "--limit", "2", "--dry-run",
    ])
    assert args.id == ["123", "456"]
    assert args.limit == 2


def test_campaign_preserves_track_sort_mode_unless_overridden() -> None:
    parser = build_parser()
    configured = parser.parse_args([
        "run", "campaign", "--track", "ai_core",
    ])
    explicit = parser.parse_args([
        "run", "campaign", "--track", "ai_core", "--sort-mode", "newest",
    ])
    assert configured.sort_mode is None
    assert explicit.sort_mode == "newest"


def test_campaign_cycle_controls_are_explicit() -> None:
    args = build_parser().parse_args([
        "run", "campaign", "--track", "ai_core", "--apply", "--allow-live",
        "--until-exhausted", "--max-cycles", "7",
    ])
    assert args.until_exhausted is True
    assert args.max_cycles == 7

    from applypilot.cli import _campaign_cycle_argv, _campaign_report_attempts

    child = _campaign_cycle_argv([
        "run", "campaign", "--track", "ai_core", "--until-exhausted",
        "--cycles", "4", "--max-cycles", "9", "--apply", "--allow-live",
    ])
    assert child == [
        "run", "campaign", "--track", "ai_core", "--apply", "--allow-live",
    ]
    assert _campaign_report_attempts({"live": {
        "hh.ru": {"summary": {"counts": {"success": 2, "needs_manual": 1}}},
    }}) == {"attempted": 3, "confirmed": 2, "needs_manual": 1, "unknown": 0}


def test_campaign_cycle_runner_stops_only_when_search_and_queue_are_exhausted(tmp_path, monkeypatch) -> None:
    from applypilot import cli

    args = cli.build_parser().parse_args([
        "run", "campaign", "--track", "ai_core", "--apply", "--allow-live",
        "--until-exhausted", "--max-cycles", "5",
    ])
    calls: list[list[str]] = []

    def fake_main(argv):
        calls.append(list(argv))
        report = tmp_path / f"report-{len(calls)}.json"
        counts = {"success": 1} if len(calls) == 1 else {}
        report.write_text(cli.json.dumps({
            "sources": ["hh.ru"],
            "scan": {"sources": [{"source": "hh.ru", "status": "ok"}]},
            "queue": {"hh.ru": {"ready": 1 if len(calls) == 1 else 0}},
            "live": {"hh.ru": {"summary": {"counts": counts}}},
        }))
        print(f"private_report: {report}")
        return 0

    monkeypatch.setattr(cli, "main", fake_main)
    code = cli._run_campaign_cycles(None, args, [
        "run", "campaign", "--track", "ai_core", "--apply", "--allow-live",
        "--until-exhausted", "--max-cycles", "5",
    ])

    assert code == 0
    assert len(calls) == 2
    assert "--cycles" not in calls[0]
    assert "--until-exhausted" not in calls[0]


def test_campaign_can_explicitly_run_headless() -> None:
    args = build_parser().parse_args([
        "run", "campaign", "--track", "ai_core", "--headless",
    ])
    assert args.headless is True
    assert args.visible is False


def test_habr_form_audit_and_live_pacing_arguments() -> None:
    audit = build_parser().parse_args([
        "sources", "form-audit", "--track", "ai_core", "--source", "career.habr.com",
        "--view", "review", "--limit", "25", "--delay-seconds", "0.5", "--json",
    ])
    assert audit.limit == 25 and audit.delay_seconds == 0.5 and audit.json
    live = build_parser().parse_args([
        "sources", "dispatch-apply", "--track", "ai_core",
        "--source", "career.habr.com", "--allow-live", "--enable-habr",
        "--habr-min-interval", "3",
    ])
    assert live.allow_live and live.enable_habr
    assert live.habr_min_interval == 3


def test_geekjob_audit_and_single_pilot_default_safe() -> None:
    audit = build_parser().parse_args([
        'sources', 'form-audit', '--track', 'ai_core',
        '--source', 'geekjob.ru', '--view', 'review', '--limit', '40', '--json',
    ])
    assert audit.source == 'geekjob.ru'
    assert audit.limit == 40
    pilot = build_parser().parse_args([
        'sources', 'geekjob-pilot', '--track', 'ai_core',
        '--id', '6ab0dcc140fca7e4f5078639', '--json',
    ])
    assert pilot.allow_live is False
    assert pilot.confirm_id == ''
    assert pilot.letter_file is None


def test_yandex_read_only_form_audit_arguments() -> None:
    args = build_parser().parse_args([
        "sources", "form-audit", "--track", "ai_core",
        "--source", "yandex.ru/jobs", "--view", "review", "--limit", "10",
        "--json",
    ])
    assert args.source == "yandex.ru/jobs"
    assert args.limit == 10
    assert args.json is True
