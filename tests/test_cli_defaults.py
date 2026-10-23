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
