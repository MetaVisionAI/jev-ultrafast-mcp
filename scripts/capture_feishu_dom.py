#!/usr/bin/env python
"""Capture simplified Feishu DOM for the collection-strategy fixtures.

Connects to an already logged-in Chrome (``http://127.0.0.1:9222`` by default,
or ``JEVMCP_CDP_URL``). Chrome 144+ answers ``/json/version`` with 404, so the
browser WebSocket is read from ``DevToolsActivePort`` in the data directory
(``JEVMCP_ATTACH_PROFILE_DIR``, else the standard Chrome profile).

Opens one new tab, visits the messenger, the week calendar and the event
editor, and writes redacted HTML to ``tests/fixtures/feishu/``. Personal names
and message bodies become placeholders. UI labels (日历, 14:00, 保存) stay,
because the strategy tests assert on them.

    python scripts/capture_feishu_dom.py
    python scripts/capture_feishu_dom.py --base https://tenant.feishu.cn
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jev_ultrafast_mcp.cdp import CdpError, attach_chrome  # noqa: E402

OUT = ROOT / "tests" / "fixtures" / "feishu"
DEFAULT_BASE = "https://tcn5xlqqoqo1.feishu.cn"

# Runs in the page. Returns a simplified HTML string of `root`, or "" when
# the root selector (a visible text label) cannot be found.
SIMPLIFY = r"""
(rootLabel) => {
  const PLACEHOLDER_NAMES = ['许文宝', '杨飞', '张晋伟', '王伟'];
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
  const redact = raw => {
    let text = String(raw).replace(/\s+/g, ' ').trim();
    for (const name of PLACEHOLDER_NAMES) text = text.split(name).join('联系人');
    if (text.length > 40 && !/\d{1,2}:\d{2}/.test(text) && !/20\d{2}/.test(text)) return '占位文本';
    return text;
  };
  const interesting = el => {
    if (!el || el.nodeType !== 1) return false;
    const cursor = getComputedStyle(el).cursor;
    if (cursor === 'pointer') return true;
    if (el.getAttribute('role') || el.onclick) return true;
    const tag = el.tagName;
    if (tag === 'BUTTON' || tag === 'A' || tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
      return true;
    }
    return false;
  };
  const hasText = el => {
    for (const node of el.childNodes) {
      if (node.nodeType === 3 && node.textContent.trim()) return true;
    }
    return false;
  };
  const worth = el => interesting(el) || hasText(el);
  const keepAttr = name => name === 'class' || name === 'role' || name === 'title'
    || name === 'placeholder' || name === 'value' || name.startsWith('aria-') || name.startsWith('data-');
  const openTag = el => {
    const attrs = [];
    for (const attr of el.attributes) {
      if (!keepAttr(attr.name)) continue;
      const value = attr.value.length > 240 ? attr.value.slice(0, 240) : attr.value;
      attrs.push(attr.name + '="' + esc(value) + '"');
    }
    if (getComputedStyle(el).cursor === 'pointer') attrs.push('style="cursor:pointer"');
    if ((el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') && el.value
        && ![...el.attributes].some(attr => attr.name === 'value')) {
      attrs.push('value="' + esc(redact(el.value)) + '"');
    }
    const box = el.getBoundingClientRect();
    attrs.push('data-w="' + Math.round(box.width) + '"');
    attrs.push('data-h="' + Math.round(box.height) + '"');
    return '<' + el.tagName.toLowerCase() + (attrs.length ? ' ' + attrs.join(' ') : '') + '>';
  };
  const render = (el, budget) => {
    if (budget.count > 4000) return '';
    const kids = [...el.children];
    const childHtml = [];
    const texts = [];
    for (const node of el.childNodes) {
      if (node.nodeType === 3) {
        const text = redact(node.textContent);
        if (text) texts.push(esc(text));
      }
    }
    let any = worth(el) || texts.length > 0;
    for (const kid of kids) {
      const html = render(kid, budget);
      if (html) { any = true; childHtml.push(html); }
    }
    if (!any) return '';
    budget.count += 1;
    return openTag(el) + texts.join(' ') + childHtml.join('') + '</' + el.tagName.toLowerCase() + '>';
  };
  const visible = el => {
    const box = el.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  };
  const byText = label => {
    const all = document.querySelectorAll('body *');
    let best = null;
    for (const el of all) {
      if (!visible(el)) continue;
      const text = (el.innerText || '').trim();
      if (text === label || text.startsWith(label)) {
        if (!best || el.contains(best)) best = el;
      }
    }
    return best;
  };
  let root = null;
  if (rootLabel === 'nav') {
    const calendar = byText('日历');
    root = calendar;
    for (let hops = 0; root && hops < 8; hops += 1) {
      const text = root.innerText || '';
      if (text.includes('日历') && text.includes('会议') && text.includes('消息')) break;
      root = root.parentElement;
    }
  } else if (rootLabel === 'calendar') {
    root = document.body;
  } else if (rootLabel === 'event') {
    root = document.body;
  } else if (rootLabel === 'day-popup') {
    root = document.querySelector('.day-select-popup');
  } else if (rootLabel === 'hour-popup') {
    root = document.querySelector('._portal._hour-select_trigger');
  } else if (rootLabel === 'search-popup') {
    const portals = [...document.querySelectorAll('._portal')];
    root = portals.find(el => (el.innerText || '').includes('技术部')) || null;
  } else {
    root = byText(rootLabel);
  }
  if (!root) return '';
  return render(root, { count: 0 });
}
"""


def _profile_dirs() -> list[Path]:
    explicit = os.environ.get("JEVMCP_ATTACH_PROFILE_DIR")
    dirs = []
    if explicit:
        dirs.append(Path(explicit).expanduser())
    dirs.append(Path.home() / "Library/Application Support/Google/Chrome")
    dirs.append(Path.home() / ".jev-ultrafast-mcp/chrome-profile")
    return dirs


def _wait_text(cdp, session: str, needle: str, timeout: float = 25.0) -> str:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = cdp.evaluate(
            "(document.body && document.body.innerText || '').slice(0, 400)",
            session,
        ) or ""
        href = cdp.evaluate("location.href", session) or ""
        if needle in last and "feishu" in href:
            time.sleep(0.8)
            return href
        if "login" in href or "accounts.feishu.cn" in href and "passport" in href:
            raise SystemExit(f"Feishu is not signed in ({href}). Sign in in this Chrome profile and rerun.")
        time.sleep(0.4)
    raise SystemExit(f"Timed out waiting for {needle!r}. Page said: {last[:200]!r}")


def _eval(cdp, session: str, expression: str):
    try:
        return cdp.evaluate(expression, session, timeout=30)
    except CdpError as exc:
        raise SystemExit(f"Page script failed: {exc}") from None


def _click_selector(cdp, session: str, selector: str) -> bool:
    script = """
    (selector) => {
      const el = document.querySelector(selector);
      if (!el) return null;
      el.scrollIntoView({ block: 'center', inline: 'center' });
      const box = el.getBoundingClientRect();
      if (box.width < 2 || box.height < 2) return null;
      return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
    }
    """
    point = _eval(cdp, session, f"({script})({selector!r})")
    if not point:
        return False
    for kind in ("mousePressed", "mouseReleased"):
        cdp.call(
            "Input.dispatchMouseEvent", session_id=session,
            type=kind, x=point["x"], y=point["y"], button="left", clickCount=1,
        )
    time.sleep(0.7)
    return True


def _click_text(cdp, session: str, label: str) -> bool:
    script = """
    (label) => {
      const nodes = document.querySelectorAll('body *');
      for (const el of nodes) {
        const own = [...el.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent.trim()).join('');
        if (own !== label) continue;
        const box = el.getBoundingClientRect();
        if (box.width < 2 || box.height < 2) continue;
        return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
      }
      return null;
    }
    """
    point = _eval(cdp, session, f"({script})({label!r})")
    if not point:
        return False
    for kind in ("mousePressed", "mouseReleased"):
        cdp.call(
            "Input.dispatchMouseEvent", session_id=session,
            type=kind, x=point["x"], y=point["y"], button="left", clickCount=1,
        )
    time.sleep(0.6)
    return True


def _capture(cdp, session: str, root: str) -> str:
    html = _eval(cdp, session, f"({SIMPLIFY})({root!r})")
    if not isinstance(html, str) or len(html) < 40:
        raise SystemExit(f"Capture of {root!r} produced nothing useful ({html!r})")
    return html


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=os.environ.get("FEISHU_BASE", DEFAULT_BASE))
    parser.add_argument("--cdp", default=os.environ.get("JEVMCP_CDP_URL", "http://127.0.0.1:9222"))
    args = parser.parse_args()
    base = args.base.rstrip("/")

    print(f"connecting to {args.cdp}", flush=True)
    cdp = attach_chrome(args.cdp, timeout=30, data_dirs=_profile_dirs(), open_timeout=60)
    try:
        cdp.call("Target.setDiscoverTargets", discover=True)
        created = cdp.call("Target.createTarget", url="about:blank")
        target_id = created["targetId"]
        session = cdp.call("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"]
        cdp.call("Page.enable", session_id=session)
        cdp.call("Runtime.enable", session_id=session)
        try:
            OUT.mkdir(parents=True, exist_ok=True)

            cdp.call("Page.navigate", session_id=session, url=f"{base}/next/messenger/")
            href = _wait_text(cdp, session, "日历")
            (OUT / "messenger.html").write_text(_capture(cdp, session, "nav"), encoding="utf-8")
            print(f"messenger  {href}", flush=True)

            cdp.call("Page.navigate", session_id=session, url=f"{base}/calendar/week")
            href = _wait_text(cdp, session, "14:00")
            (OUT / "calendar.html").write_text(_capture(cdp, session, "calendar"), encoding="utf-8")
            print(f"calendar   {href}", flush=True)

            cdp.call("Page.navigate", session_id=session, url=f"{base}/calendar/eventedit")
            href = _wait_text(cdp, session, "保存")
            # The form and each popup are captured separately and concatenated.
            # Opening the time menu closes the date panel, so one snapshot
            # cannot hold every row the strategy has to see. Nothing is saved.
            form = _capture(cdp, session, "event")
            if not _click_selector(cdp, session, ".date-start .day-select-input"):
                raise SystemExit("Could not open the start-date control")
            day = _capture(cdp, session, "day-popup")
            if not _click_selector(cdp, session, ".date-start ._hour-select input"):
                raise SystemExit("Could not open the start-time control")
            hour = _capture(cdp, session, "hour-popup")
            if not _click_selector(cdp, session, ".participants input"):
                raise SystemExit("Could not focus the attendee search")
            cdp.call("Input.insertText", session_id=session, text="飞")
            time.sleep(1.2)
            search = _capture(cdp, session, "search-popup")
            (OUT / "event-edit.html").write_text(
                form + "\n" + day + "\n" + hour + "\n" + search, encoding="utf-8"
            )
            print(f"event edit {href}", flush=True)
        finally:
            cdp.call("Target.closeTarget", targetId=target_id)
    finally:
        cdp.close()
    for path in sorted(OUT.glob("*.html")):
        print(f"wrote {path.relative_to(ROOT)} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
