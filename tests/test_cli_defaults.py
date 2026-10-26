"""Mass AI queue defaults must not silently revert to pilot-sized batches."""

from applypilot.cli import build_parser


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
