"""`browser_request_help`: the mechanics of handing one step to a person.

When to ask is the agent's call, made from the skill's
`references/help-and-recovery.md`; nothing here detects a login. What is
pinned is that asking behaves: a malformed request is refused before anyone is
interrupted, nobody is asked when nobody can see the page, the panel survives a
navigation, a flickering success does not end the request, and every way out
removes the panel.
"""

from collections import deque

import pytest

from jev_ultrafast_mcp import help as H
from jev_ultrafast_mcp import server as S
from jev_ultrafast_mcp.browser import HELP_BINDING, Session
from jev_ultrafast_mcp.config import Config


class FakePage:
    """A CDP double whose page state is advanced by a script, one step per poll."""

    def __init__(self, script=None, url="https://example.com/login"):
        self.events: deque = deque(maxlen=400)
        self.calls: list[str] = []
        self.url = url
        self.present = False
        self.answer = None
        self.results: list = []
        self.installs = 0
        self.removed = 0
        self.polls = 0
        self.navigating = False
        self.script = script or (lambda page, n: None)
        self.token = None

    def call(self, method, session_id=None, timeout=None, **params):
        self.calls.append(method)
        return {}

    def evaluate(self, expression, session_id, *, await_promise=False, timeout=None):
        if "attachShadow" in expression:
            self.installs += 1
            self.present = True
            self.token = expression.split('"token": "', 1)[1].split('"', 1)[0]
            return {"resolved": [], "unresolved": []}
        if "delete window.__jevHelpAnswer" in expression:
            self.removed += 1
            self.present = False
            return True
        if "const answer = window.__jevHelpAnswer" in expression:
            if self.installs:
                self.script(self, self.polls)
                self.polls += 1
            if self.navigating:
                return None
            return {"url": self.url, "present": self.present,
                    "answer": self.answer, "results": list(self.results)}
        if "__jevMcp.resolve(" in expression:
            return {"ok": True, "x": 10, "y": 20}
        return 1

    def press(self, action, note="", via_binding=False):
        answer = {"token": self.token, "action": action, "note": note}
        if via_binding:
            import json
            self.events.append({"method": "Runtime.bindingCalled", "sessionId": "page",
                                "params": {"name": HELP_BINDING, "payload": json.dumps(answer)}})
        else:
            self.answer = answer


def _session(page, **cfg):
    config = Config(mode="attach", cdp_url="http://127.0.0.1:9222", **cfg)
    return Session("t", config, page, target_id="T", page_session="page")


def _request(**kwargs):
    defaults = {"prompt": "please sign in", "timeout_ms": 2000}
    defaults.update(kwargs)
    return H.parse(**defaults)


# ------------------------------------------------------------------ validation


@pytest.mark.parametrize("kwargs, message", [
    ({"prompt": ""}, "prompt is required"),
    ({"timeout_ms": 10}, "timeout_ms"),
    ({"timeout_ms": 31 * 60 * 1000}, "timeout_ms"),
    ({"completion": {"any": [{"url_contains": "/a"}], "all": []}}, "not both"),
    ({"completion": {"any": [{"nope": "x"}]}}, "unknown completion condition"),
    ({"completion": {"any": [{"url_contains": "/a", "text_exists": "b"}]}}, "exactly one key"),
    ({"completion": {"any": [{"text_exists": str(i)} for i in range(9)]}}, "at most 8"),
    ({"completion": {"any": [{"url_matches": "a" * 129}]}}, "128"),
    ({"completion": {"any": [{"url_matches": "(unclosed"}]}}, "not a valid regular expression"),
    ({"completion": {"stable_for_ms": -1}}, "stable_for_ms"),
    ({"targets": [f"e{i}" for i in range(9)]}, "at most 8"),
])
def test_a_malformed_request_is_refused_before_anyone_is_asked(kwargs, message):
    with pytest.raises(ValueError, match=message):
        _request(**kwargs)


def test_targets_split_into_refs_and_selectors_and_empty_patterns_impose_nothing():
    request = _request(targets=["e3", "#login .qr"],
                       completion={"all": [{"url_matches": ""}, {"selector_missing": "#login"}]})
    assert request.refs == ["e3"]
    assert request.selectors == ["#login .qr"]
    assert request.mode == "all"
    assert request.conditions == [("selector_missing", "#login")]


def test_the_tool_reports_invalid_params_without_touching_the_browser(monkeypatch):
    def boom(_name):
        raise AssertionError("the browser must not be reached for a malformed request")
    monkeypatch.setattr(S, "_session", boom)
    answer = S.browser_request_help("sign in", completion={"any": [{"bogus": "x"}]})
    assert answer.startswith("invalid_params:")


# -------------------------------------------------------------------- disabled


def test_nobody_is_asked_when_the_switch_is_off():
    page = FakePage()
    result = _session(page, request_help=False).request_help(_request())
    assert result == {"outcome": "disabled", "reason": "JEVMCP_REQUEST_HELP is off"}
    assert page.installs == 0 and not page.calls


def test_nobody_is_asked_when_the_browser_is_headless():
    page = FakePage()
    config = Config(mode="launch", headless=True)
    result = Session("t", config, page, target_id="T", page_session="page").request_help(_request())
    assert result["outcome"] == "disabled"
    assert "headless" in result["reason"]
    assert page.installs == 0


def test_an_attached_browser_is_never_treated_as_headless():
    assert H.disabled_reason(Config(mode="attach", headless=True)) == ""


def test_the_tool_says_not_to_ask_again_when_disabled(monkeypatch):
    page = FakePage()
    monkeypatch.setattr(S, "_session", lambda _name: _session(page, request_help=False))
    answer = S.browser_request_help("sign in")
    assert answer.startswith("help disabled: JEVMCP_REQUEST_HELP is off")
    assert "Do not ask again" in answer


# --------------------------------------------------------------------- buttons


@pytest.mark.parametrize("action", ["continued", "cancelled"])
def test_a_button_press_ends_the_request_and_removes_the_panel(action):
    def user(page, n):
        if n == 2:
            page.press(action, note="done on my phone")
    page = FakePage(user)
    result = _session(page).request_help(_request(), poll_ms=1)
    assert result["outcome"] == action
    assert result["note"] == "done on my phone"
    assert page.removed == 1
    assert {"Target.activateTarget", "Page.bringToFront", "Runtime.addBinding"} <= set(page.calls)


def test_an_answer_through_the_binding_counts_too():
    def user(page, n):
        if n == 1:
            page.press("continued", via_binding=True)
    page = FakePage(user)
    assert _session(page).request_help(_request(), poll_ms=1)["outcome"] == "continued"


def test_an_answer_from_another_request_is_ignored():
    def user(page, n):
        if n == 0:
            page.answer = {"token": "someone-else", "action": "continued"}
    page = FakePage(user)
    request = _request()
    request.timeout_ms = 60
    assert _session(page).request_help(request, poll_ms=5)["outcome"] == "timed_out"


# ------------------------------------------------------------------ navigation


def test_the_panel_is_put_back_after_a_navigation_which_alone_completes_nothing():
    def user(page, n):
        if n == 1:
            page.navigating = True
        if n == 2:
            page.navigating, page.present, page.url = False, False, "https://example.com/home"
        if n == 4:
            page.press("continued")
    page = FakePage(user)
    result = _session(page).request_help(_request(), poll_ms=1)
    assert result["outcome"] == "continued"
    assert result["reinjected"] == 1
    assert page.installs == 2


# ------------------------------------------------------------------ completion


def test_conditions_must_hold_for_stable_for_ms_before_completing():
    request = _request(completion={"any": [{"selector_missing": "#login"}], "stable_for_ms": 40})
    flicker = iter([True, False, True, False])

    def user(page, n):
        page.results = [next(flicker, True)]
    page = FakePage(user)
    result = _session(page).request_help(request, poll_ms=5)
    assert result["outcome"] == "completed"
    assert result["completed_by"] == "system"
    assert page.polls > 5, "a flicker through the success state must not complete the request"


def test_url_conditions_are_matched_on_the_server():
    request = _request(completion={"all": [{"url_contains": "/home"},
                                           {"url_matches": r"^https://example\.com/"}],
                                   "stable_for_ms": 0})

    def user(page, n):
        if n == 3:
            page.url = "https://example.com/home"
    page = FakePage(user)
    result = _session(page).request_help(request, poll_ms=1)
    assert result["outcome"] == "completed"
    assert page.polls >= 4


def test_an_invalid_selector_is_refused_before_the_panel_is_shown():
    request = _request(completion={"any": [{"selector_exists": "##bad"}]})
    page = FakePage()
    page.results = ["invalid"]
    with pytest.raises(ValueError, match="invalid CSS selectors"):
        _session(page).request_help(request)
    assert page.installs == 0


# --------------------------------------------------------------------- timeout


def test_the_deadline_ends_the_request_and_removes_the_panel():
    request = _request()
    request.timeout_ms = 50
    page = FakePage()
    result = _session(page).request_help(request, poll_ms=5)
    assert result["outcome"] == "timed_out"
    assert result["elapsed_ms"] >= 50
    assert page.removed == 1


def _chrome_available() -> bool:
    from jev_ultrafast_mcp.config import find_chrome
    try:
        find_chrome(None)
        return True
    except RuntimeError:
        return False


@pytest.fixture
def live(monkeypatch, tmp_path):
    """A real headless Chrome, with the headless refusal lifted so the panel can be exercised."""
    if not _chrome_available():
        pytest.skip("no Chromium-family browser found")
    from jev_ultrafast_mcp.browser import BrowserManager
    monkeypatch.setattr(H, "disabled_reason", lambda cfg: "")
    manager = BrowserManager(Config(headless=True, state_dir=tmp_path))
    try:
        yield manager.session("help")
    finally:
        manager.shutdown()


PAGE = ("data:text/html,<button id=login>Sign in</button><script>"
        "%s</script>")


@pytest.mark.parametrize("button, outcome", [("button.done", "continued"),
                                             ("button:not(.done)", "cancelled")])
def test_live_the_panel_buttons_answer_through_the_binding(live, button, outcome):
    script = ("setTimeout(()=>{const r=document.getElementById('__jev_help').shadowRoot;"
              "r.querySelector('textarea').value='ok';"
              f"r.querySelector('{button}').click()}},600)")
    live.navigate(PAGE % script)
    live.observe()
    result = live.request_help(_request(targets=["e1", "#login", "#missing"]))
    assert result["outcome"] == outcome
    assert result["note"] == "ok"
    assert result["resolved_targets"] == ["e1", "#login"]
    assert result["unresolved_targets"] == ["#missing"]
    assert live._safe_eval("!!document.getElementById('__jev_help')") is False


def test_live_completion_waits_for_a_stable_signal(live):
    live.navigate(PAGE % "setTimeout(()=>document.getElementById('login').remove(),700)")
    result = live.request_help(_request(
        completion={"any": [{"selector_missing": "#login"}], "stable_for_ms": 500}))
    assert result["outcome"] == "completed"
    assert result["elapsed_ms"] >= 1200


def test_live_the_panel_comes_back_after_the_page_navigates(live, tmp_path):
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    site = tmp_path / "site"
    site.mkdir()
    (site / "login.html").write_text(
        "<p>login</p><script>setTimeout(()=>{location.href='/home.html'},400)</script>")
    (site / "home.html").write_text("<p>welcome home</p>")
    handler = partial(SimpleHTTPRequestHandler, directory=str(site))
    handler.log_message = lambda *args: None
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        live.navigate(f"http://127.0.0.1:{server.server_port}/login.html")
        result = live.request_help(_request(
            completion={"any": [{"text_exists": "welcome home"}], "stable_for_ms": 1200}))
    finally:
        server.shutdown()
    assert result["outcome"] == "completed"
    assert result["reinjected"] >= 1


def test_refs_are_outlined_by_their_position_on_the_page():
    def user(page, n):
        page.press("continued")
    page = FakePage(user)
    seen = {}
    original = page.evaluate

    def spy(expression, session_id, **kw):
        if "attachShadow" in expression:
            seen["spec"] = expression
        return original(expression, session_id, **kw)
    page.evaluate = spy
    _session(page).request_help(_request(targets=["e3"]), poll_ms=1)
    assert '"points": [{"x": 10, "y": 20, "label": "e3"}]' in seen["spec"]
