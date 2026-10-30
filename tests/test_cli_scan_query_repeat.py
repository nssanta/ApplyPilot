"""Repeated scan --query flags must stay whole phrases, never individual characters."""
from applypilot.cli import _apply_scan_overrides, build_parser


def test_scan_accepts_multiple_query_phrases() -> None:
    args = build_parser().parse_args([
        "scan", "--track", "ai", "--query", "Python backend",
        "--query", "LLM Engineer", "--query", "AI-агенты",
    ])
    assert args.query == ["Python backend", "LLM Engineer", "AI-агенты"]


def test_scan_override_does_not_split_query_into_characters() -> None:
    args = build_parser().parse_args([
        "scan", "--query", "Python backend", "--query", "LLM Engineer",
    ])
    result = _apply_scan_overrides(
        {"queries": ["old"], "groups": [{"queries": ["old"]}]}, args,
    )
    assert result["groups"][0]["queries"] == ["Python backend", "LLM Engineer"]
    assert all(len(q) > 1 for q in result["groups"][0]["queries"])


def test_scan_one_query_is_a_one_item_list() -> None:
    args = build_parser().parse_args(["scan", "--query", "AI Product Engineer"])
    result = _apply_scan_overrides({"queries": ["old"]}, args)
    assert result["queries"] == ["AI Product Engineer"]