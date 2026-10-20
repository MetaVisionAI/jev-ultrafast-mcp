"""Feishu controls that are plain divs show up only when that strategy is on.

The fixtures are the component shapes a logged-in session paints
(`tests/fixtures/feishu`), not a list of one screen's widgets. They are served
from localhost, so the host match does not fire; `readState({strategy: "feishu"})`
is the override the collector accepts for exactly this. A page that does not ask
for it, and is not a Feishu host, keeps the generic selector.
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
button, input, .navbarMenu, .larkc-checkbox, .larkc-dayPanel-date, ._option,
._selection, .label-btn, .add-new-event-btn, .larkc-pickerToolBar-icon,
.adit-format-button, .close, [data-start-time] {
  display: inline-block; min-width: 24px; min-height: 18px; vertical-align: top;
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
    for path in FIXTURES.glob("*.html"):
        (work / path.name).write_text(LAYOUT + path.read_text(encoding="utf-8"), encoding="utf-8")
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
def test_a_pointer_div_stays_out_of_the_table_on_any_other_site(origin, session):
    session.navigate(f"{origin}/generic.html")

    plain = _read(session)
    assert _has(plain, "button", "点我") is None
    assert _has(plain, "button", "OK") is not None
    # The typeless input is a textbox without any site strategy.
    assert _has(plain, "textbox", "无类型") is not None

    feishu = _read(session, "feishu")
    assert _has(feishu, "button", "点我") is not None, feishu.render()


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_the_calendar_names_days_by_month_and_icons_by_their_own_attributes(origin, session):
    session.navigate(f"{origin}/calendar.html")

    plain = _read(session)
    assert _has(plain, "button", "icon-add") is None
    assert _has(plain, "gridcell", "20", exact=True) is None

    feishu = _read(session, "feishu")
    assert _has(feishu, "button", "icon-add") is not None, feishu.render()
    assert _has(feishu, "button", "LeftOutlined") is not None
    assert _has(feishu, "button", "RightOutlined") is not None
    week = _has(feishu, "button", "周", exact=True)
    assert week is not None and week.checked is True

    twenties = [item for item in feishu.elements if item.name == "27"]
    assert len(twenties) == 2, feishu.render()
    assert any("非本月" in item.context for item in twenties)
    assert any("2026年10月" in item.context and "非本月" not in item.context for item in twenties)

    header = next(item for item in feishu.elements if "2026-10-20" in item.context)
    assert header.role == "gridcell"
    assert "20" in header.name


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_the_event_editor_exposes_fields_by_component_shape(origin, session):
    session.navigate(f"{origin}/event-edit.html")

    plain = _read(session)
    assert _has(plain, "textbox", "添加地点") is not None, plain.render()
    assert _has(plain, "textbox", "添加联系人或群") is not None
    assert _has(plain, "gridcell", "20", exact=True) is None
    assert _has(plain, "option", "14:00") is None
    assert _has(plain, "checkbox", "全天") is None
    assert _has(plain, "combobox", "不重复") is None

    feishu = _read(session, "feishu")
    assert _has(feishu, "textbox", "开始 日期") is not None, feishu.render()
    assert _has(feishu, "textbox", "开始 时间") is not None, feishu.render()
    assert _has(feishu, "textbox", "结束 时间") is not None, feishu.render()
    assert _has(feishu, "textbox", "结束 日期") is not None, feishu.render()

    allday = _has(feishu, "checkbox", "全天")
    assert allday is not None and allday.checked is False
    invited = _has(feishu, "checkbox", "邀请参与者")
    assert invited is not None and invited.checked is True
    listing = _has(feishu, "checkbox", "查看参与者列表")
    assert listing is not None and listing.disabled is True

    for label in ("不重复", "提前5分钟", "忙碌"):
        box = _has(feishu, "combobox", label, exact=True)
        assert box is not None and box.current == label, feishu.render()

    assert _has(feishu, "option", "14:00") is not None
    assert _has(feishu, "option", "15:00") is not None
    selected = _has(feishu, "option", "18:00", exact=True)
    assert selected is not None and selected.checked is True and selected.value == "18:00"
    assert _has(feishu, "option", "联系人") is not None, feishu.render()
    assert _has(feishu, "button", "icon-close") is not None, feishu.render()
    assert _has(feishu, "button", "粗体") is not None, feishu.render()


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_a_visible_popup_inside_a_hidden_portal_is_collected(origin, session):
    session.navigate(f"{origin}/portal.html")
    feishu = _read(session, "feishu")
    assert _has(feishu, "gridcell", "20", exact=True) is not None, feishu.render()
    assert _has(feishu, "gridcell", "99", exact=True) is None


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_a_popup_that_appears_after_a_click_names_the_control_that_opened_it(origin, session):
    session.navigate(f"{origin}/popup.html")
    before = _read(session, "feishu")
    opener = _has(before, "button", "打开", exact=True)
    assert opener is not None, before.render()
    already = _has(before, "option", "已在", exact=True)
    assert already is not None and "↳" not in already.context

    resolved = session.cdp.evaluate(
        f"window.__jevMcp.resolve({json.dumps(opener.ref)})",
        session.page_session, timeout=20,
    )
    assert resolved and resolved.get("ok"), resolved
    session.cdp.evaluate(
        "document.body.insertAdjacentHTML('beforeend',"
        "'<div class=\"_portal\" style=\"display:inline-block\">"
        "<div class=\"_option\" style=\"cursor:pointer\">14:00</div></div>')",
        session.page_session, timeout=20,
    )
    after = _read(session, "feishu")
    opened = _has(after, "option", "14:00", exact=True)
    assert opened is not None, after.render()
    assert f"↳ {opener.ref}" in opened.context
    still = _has(after, "option", "已在", exact=True)
    assert still is not None and "↳" not in still.context


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_a_row_scrolled_out_of_its_list_is_not_called_covered(origin, session):
    """Overflow clipping is not a dialog. A button under another box still is.

    The later hour of a time menu sits below the list's box, so the hit test
    finds the page behind it. Marking that row covered drops it from the
    question, and the model is left with the duration row it can already see.
    """
    session.navigate(f"{origin}/scroll-menu.html")
    feishu = _read(session, "feishu")
    early = _has(feishu, "option", "00:00", exact=True)
    late = _has(feishu, "option", "14:00", exact=True)
    covered = _has(feishu, "button", "被盖住", exact=True)

    assert early is not None and late is not None and covered is not None, feishu.render()
    assert early.occluded is False
    assert late.occluded is False, feishu.render()
    assert covered.occluded is True


@pytest.mark.skipif(not _chrome_available(), reason="no Chromium-family browser found")
def test_what_a_control_is_for_comes_from_the_component_not_its_label(origin, session):
    """The goal loop keys on `kind`. A label in one language would miss the next site.

    A `_selection` that searches as you type is `suggest`; any other one is
    a `picker` set from its menu. The primary button submits; `icon-add`
    creates. Outside the component vocabulary the HTML says the same things.
    """
    session.navigate(f"{origin}/editor-kinds.html")
    feishu = _read(session, "feishu")
    kinds = {element.name: element.kind for element in feishu.elements}

    assert kinds.get("添加联系人或群") == "suggest", feishu.render()
    assert kinds.get("添加主题") == ""
    assert kinds.get("保存") == "submit"
    assert kinds.get("icon-add") == "create"
    pickers = [element for element in feishu.elements
               if element.editable and element.value in {"2026年10月20日", "12:00"}]
    assert len(pickers) == 2 and all(element.kind == "picker" for element in pickers), feishu.render()

    plain = _read(session)
    native = {element.name: element.kind for element in plain.elements}
    assert native.get("日期") == "picker", plain.render()
    assert native.get("城市") == "suggest"
    assert native.get("提交表单") == "submit"
    assert native.get("添加联系人或群") == ""
