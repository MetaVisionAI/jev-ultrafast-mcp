"""How `JEVMCP_WINDOW` becomes a viewport.

A headless browser has no window, so an unset value stays 1280x860. A visible
window, and a browser we only attach to, fills the window instead: locking it
to a fixed box is what makes the page look like it sits in a frame.
"""

from __future__ import annotations

from jev_ultrafast_mcp.config import DEFAULT_WINDOW, Config, parse_window


def test_an_unset_window_fills_a_visible_or_attached_browser(monkeypatch):
    monkeypatch.delenv("JEVMCP_WINDOW", raising=False)
    monkeypatch.delenv("JEVMCP_MODE", raising=False)

    monkeypatch.setenv("JEVMCP_HEADLESS", "0")
    assert Config.from_env().window is None

    monkeypatch.setenv("JEVMCP_HEADLESS", "1")
    monkeypatch.setenv("JEVMCP_MODE", "attach")
    assert Config.from_env().window is None


def test_an_unset_window_stays_fixed_when_there_is_no_window(monkeypatch):
    monkeypatch.delenv("JEVMCP_WINDOW", raising=False)
    monkeypatch.delenv("JEVMCP_MODE", raising=False)
    monkeypatch.setenv("JEVMCP_HEADLESS", "1")

    assert Config.from_env().window == DEFAULT_WINDOW


def test_an_explicit_size_locks_the_viewport_even_when_the_window_is_visible(monkeypatch):
    monkeypatch.setenv("JEVMCP_HEADLESS", "0")
    monkeypatch.setenv("JEVMCP_WINDOW", "1920x1080")

    assert Config.from_env().window == (1920, 1080)


def test_auto_fills_the_window_even_headless():
    assert parse_window("auto", headless=True, mode="launch") is None
    assert parse_window("100%", headless=True, mode="launch") is None


def test_a_malformed_window_falls_back_instead_of_refusing():
    assert parse_window("wide", headless=True, mode="launch") == DEFAULT_WINDOW
    assert parse_window("0x860", headless=False, mode="launch") == DEFAULT_WINDOW
