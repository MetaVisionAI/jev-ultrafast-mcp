"""Feishu controls that are plain divs show up only when that strategy is on.

The fixtures are simplified DOM from a logged-in session
(`scripts/capture_feishu_dom.py`). They are served from localhost, so the host
match does not fire; `readState({strategy: "feishu"})` is the override the
collector accepts for exactly this. A page that does not ask for it, and is
not a Feishu host, keeps the generic selector.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jev_ultrafast_mcp.browser import BrowserManager
from jev_ultrafast_mcp.config import Config, find_chrome
from jev_ultrafast_mcp.observe import Element, Observation

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "feishu"

LAYOUT = """<!doctype html><meta charset="utf-8"><style>
.navbarMenu, .add-new-event-btn, .larkc-dayPanel-date, .calendar-timeline-slot,
._option, .attendee-search-item, button, input {
  display: inline-block; min-width: 20px; min-height: 16px;
}
</style>
"""


def _chrome_available() -> bool:
    try:
        find_chrome(None)
        return True
    except RuntimeError:
        return False


def _has(observation: Observation, role: str, name: str, *, exact: bool = False) -> Element | None:
    needle = name.lower()
    for element in observation.elements:
        if element.role != role:
            continue
        label = element.name.lower()
        if label == needle or (not exact and needle in label):
            return element
    return None


@pytest.fixture(scope="module")
def origin():
    work = Path(tempfile.mkdtemp(prefix="jev-feishu-"))
    for name in ("messenger.html", "calendar.html", "event-edit.html"):
        (work / name).write_text(LAYOUT + (FIXTURES / name).read_text(encoding="utf-8"), encoding="utf-8")
    # Feishu mounts the day panel in a portal that is `visibility: hidden`,
    # with the panel itself `visibility: visible`. A cell that does not
    # override the portal must stay out of the table.
    (work / "portal.html").write_text(LAYOUT + """
<div class="_portal" style="visibility:hidden">
  <div class="day-select-popup" style="visibility:visible">
    <div class="larkc-dayPanel-date">20</div>
  </div>
  <div class="larkc-dayPanel-date">99</div>
</div>
""", encoding="utf-8")
    handler = partial(SimpleHTTPRequestHandler, directory=str(work))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base
    httpd.shutdown()
    shutil.rmtree(work, ignore_errors=True)


@pytest.fixture(scope="module")
def session(origin):
    if not _chrome_available():
        pytest.skip("no Chromium-family browser found")
    profile = Path(tempfile.mkdtemp(prefix="jev-feishu-profile-"))
    cfg = Config(headless=True, profile_dir=profile, window=(1280, 860))
    manager = BrowserManager(cfg)
    tab = manager.session("feishu")
    yield tab
    manager.shutdown()
    shutil.rmtree(profile, ignore_errors=True)


def _read(tab, strategy: str | None = None) -> Observation:
    tab._ensure_helper()
    options: dict = {"maxActions": 250, "maxText": 500, "includeText": False}
    if strategy:
        options["strategy"] = strategy
    raw = tab.cdp.evaluate(
        f"window.__jevMcp.readState({json.dumps(options)})",
        tab.page_session, timeout=20,
    )
    assert raw, "the page produced no snapshot"
    return Observation.from_raw(json.loads(raw))


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_the_sidebar_is_invisible_until_the_feishu_strategy_is_on(origin, session):
    session.navigate(f"{origin}/messenger.html")

    plain = _read(session)
    assert _has(plain, "button", "日历") is None
    assert plain.strategy == ""

    feishu = _read(session, "feishu")
    calendar = _has(feishu, "button", "日历")
    assert calendar is not None, feishu.render()
    assert feishu.strategy == "feishu"
    assert "strategy=feishu" in feishu.render()

    session.last = feishu
    session._fresh = True
    clicked = session.act([{"op": "click", "ref": calendar.ref}], observe_after=False)
    assert clicked["ok"], clicked


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_the_calendar_create_button_and_time_labels_need_the_strategy(origin, session):
    session.navigate(f"{origin}/calendar.html")

    plain = _read(session)
    assert _has(plain, "button", "创建日程") is None
    assert _has(plain, "gridcell", "14:00") is None

    feishu = _read(session, "feishu")
    assert _has(feishu, "button", "创建日程") is not None, feishu.render()
    assert _has(feishu, "gridcell", "14:00") is not None


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_the_event_editor_exposes_the_day_the_times_and_the_attendee(origin, session):
    session.navigate(f"{origin}/event-edit.html")

    plain = _read(session)
    assert _has(plain, "gridcell", "20", exact=True) is None
    assert _has(plain, "option", "14:00") is None
    assert _has(plain, "option", "15:00") is None
    assert _has(plain, "option", "联系人") is None
    # The field is an <input> with no type attribute. The generic collector
    # trusts that attribute, so the row appears only under the Feishu strategy.
    assert _has(plain, "textbox", "添加联系人或群") is None

    feishu = _read(session, "feishu")
    assert _has(feishu, "gridcell", "20", exact=True) is not None, "day cell 20 (10月20日) missing"
    assert _has(feishu, "option", "14:00") is not None
    assert _has(feishu, "option", "15:00") is not None
    assert _has(feishu, "textbox", "添加联系人或群") is not None
    assert _has(feishu, "option", "联系人") is not None, feishu.render()


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_a_visible_popup_inside_a_hidden_portal_is_collected(origin, session):
    session.navigate(f"{origin}/portal.html")
    feishu = _read(session, "feishu")
    assert _has(feishu, "gridcell", "20", exact=True) is not None, feishu.render()
    assert _has(feishu, "gridcell", "99", exact=True) is None
