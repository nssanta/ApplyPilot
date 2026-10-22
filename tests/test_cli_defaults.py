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
