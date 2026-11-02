"""Regression: background command jobs must wait for owner CAPTCHA in visible Chrome."""
from applypilot.cli import _allow_manual_captcha


def test_visible_background_without_tty_is_interactive():
    assert _allow_manual_captcha(
        run_headless=False, visible_requested=True, has_tty=False, env_interactive=False
    )


def test_headless_is_never_interactive_even_with_override():
    assert not _allow_manual_captcha(
        run_headless=True, visible_requested=True, has_tty=True, env_interactive=True
    )


def test_background_without_visible_or_env_does_not_wait():
    assert not _allow_manual_captcha(
        run_headless=False, visible_requested=False, has_tty=False, env_interactive=False
    )


def test_explicit_env_can_opt_in_for_visible_process():
    assert _allow_manual_captcha(
        run_headless=False, visible_requested=False, has_tty=False, env_interactive=True
    )
