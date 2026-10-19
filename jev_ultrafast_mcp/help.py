"""Handing one step of a task to the person at the browser.

The server never decides that a page needs a human -- the agent does, from what
the observation shows (see `skills/.../references/help-and-recovery.md`). What
lives here is the mechanics of asking: validating the request, the panel the
user sees, and the conditions that let a request finish without a click.

The JavaScript is evaluated per request instead of being part of the observer
helper, so the page carries none of it unless a request is in progress, and the
helper's version (and its copy in the Chrome extension) are untouched.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .config import Config

CONDITION_KINDS = (
    "url_contains", "url_matches",
    "selector_exists", "selector_missing",
    "text_exists", "text_missing",
)
URL_KINDS = ("url_contains", "url_matches")
MAX_CONDITIONS = 8
MAX_URL_PATTERN = 128
MAX_TARGETS = 8
DEFAULT_TIMEOUT_MS = 5 * 60 * 1000
MAX_TIMEOUT_MS = 30 * 60 * 1000
MIN_TIMEOUT_MS = 1000
DEFAULT_STABLE_MS = 1000
MAX_STABLE_MS = 60 * 1000
DEFAULT_TITLE = "Agent 需要你的帮助"
REF = re.compile(r"^e\d+$")


@dataclass
class HelpRequest:
    prompt: str
    title: str = DEFAULT_TITLE
    refs: list[str] = field(default_factory=list)
    selectors: list[str] = field(default_factory=list)
    mode: str = "any"
    conditions: list[tuple[str, str]] = field(default_factory=list)
    stable_ms: int = DEFAULT_STABLE_MS
    timeout_ms: int = DEFAULT_TIMEOUT_MS


def parse(prompt: object, title: object = "", targets: object = None,
          completion: object = None, timeout_ms: object = DEFAULT_TIMEOUT_MS) -> HelpRequest:
    """Validate a request before anything is shown to the user.

    A request refused here costs nothing; one refused after the panel is up has
    interrupted a person for a malformed condition.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt is required: say what the user should do and when to press the button")
    if len(prompt) > 2000:
        raise ValueError("prompt is longer than 2000 characters")
    if title is None:
        title = ""
    if not isinstance(title, str) or len(title) > 120:
        raise ValueError("title must be a string of at most 120 characters")
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int):
        raise ValueError("timeout_ms must be an integer number of milliseconds")
    if not MIN_TIMEOUT_MS <= timeout_ms <= MAX_TIMEOUT_MS:
        raise ValueError(f"timeout_ms must be between {MIN_TIMEOUT_MS} and {MAX_TIMEOUT_MS}")

    request = HelpRequest(prompt=prompt.strip(), title=title.strip() or DEFAULT_TITLE,
                          timeout_ms=timeout_ms)

    targets = targets or []
    if isinstance(targets, str):
        targets = [targets]
    if not isinstance(targets, list) or len(targets) > MAX_TARGETS:
        raise ValueError(f"targets must be a list of at most {MAX_TARGETS} refs or CSS selectors")
    for target in targets:
        if not isinstance(target, str) or not target.strip() or len(target) > 200:
            raise ValueError("each target must be a ref like 'e3' or a CSS selector")
        target = target.strip()
        (request.refs if REF.match(target) else request.selectors).append(target)

    completion = completion or {}
    if not isinstance(completion, dict):
        raise ValueError('completion must be an object such as {"any": [{"url_contains": "/home"}]}')
    unknown = set(completion) - {"any", "all", "stable_for_ms"}
    if unknown:
        raise ValueError(f"completion has unknown keys: {sorted(unknown)}")
    if "any" in completion and "all" in completion:
        raise ValueError("completion takes either `any` or `all`, not both")
    mode = "all" if "all" in completion else "any"
    items = completion.get(mode) or []
    if not isinstance(items, list):
        raise ValueError(f"completion.{mode} must be a list of conditions")
    for item in items:
        if not isinstance(item, dict) or len(item) != 1:
            raise ValueError("each completion condition is an object with exactly one key, "
                             f"one of {', '.join(CONDITION_KINDS)}")
        (kind, value), = item.items()
        if kind not in CONDITION_KINDS:
            raise ValueError(f"unknown completion condition {kind!r}; "
                             f"use one of {', '.join(CONDITION_KINDS)}")
        if kind == "url_matches" and value in (None, ""):
            continue  # an empty pattern imposes no condition
        if not isinstance(value, str) or not value or len(value) > 500:
            raise ValueError(f"{kind} needs a non-empty string value")
        if kind == "url_matches":
            if len(value) > MAX_URL_PATTERN:
                raise ValueError(f"url_matches is limited to {MAX_URL_PATTERN} characters")
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"url_matches is not a valid regular expression: {exc}") from None
        request.conditions.append((kind, value))
    if len(request.conditions) > MAX_CONDITIONS:
        raise ValueError(f"completion allows at most {MAX_CONDITIONS} conditions in total")
    request.mode = mode

    stable = completion.get("stable_for_ms", DEFAULT_STABLE_MS)
    if isinstance(stable, bool) or not isinstance(stable, int) or not 0 <= stable <= MAX_STABLE_MS:
        raise ValueError(f"stable_for_ms must be an integer between 0 and {MAX_STABLE_MS}")
    request.stable_ms = stable
    return request


def disabled_reason(cfg: Config) -> str:
    """Why nobody can be asked, or "" when someone can."""
    if not cfg.request_help:
        return "JEVMCP_REQUEST_HELP is off"
    if cfg.mode != "attach" and cfg.headless:
        return "the browser is headless, so there is no window for anyone to see"
    return ""


def met(request: HelpRequest, state: dict) -> bool:
    """Whether the completion conditions hold on the page `state` describes."""
    if not request.conditions:
        return False
    url = str(state.get("url") or "")
    page = state.get("results") or []
    results = []
    for index, (kind, value) in enumerate(request.conditions):
        if kind == "url_contains":
            results.append(value in url)
        elif kind == "url_matches":
            results.append(bool(re.search(value, url)))
        else:
            results.append(index < len(page) and page[index] is True)
    return all(results) if request.mode == "all" else any(results)


def invalid_selectors(request: HelpRequest, state: dict) -> list[str]:
    page = state.get("results") or []
    return [value for index, (_, value) in enumerate(request.conditions)
            if index < len(page) and page[index] == "invalid"]


# ---------------------------------------------------------------- page side

HOST_ID = "__jev_help"

_INSTALL = r"""
(spec => {
  const old = document.getElementById(%(host)s);
  if (old) { if (old.__jevStop) old.__jevStop(); old.remove(); }
  const boxes = [], resolved = [], unresolved = [];
  for (const p of spec.points) {
    const e = document.elementFromPoint(p.x, p.y);
    if (e) { boxes.push([e, p.label]); resolved.push(p.label); } else unresolved.push(p.label);
  }
  for (const sel of spec.selectors) {
    let e = null;
    try { e = document.querySelector(sel); } catch (_) {}
    if (e) { boxes.push([e, sel]); resolved.push(sel); } else unresolved.push(sel);
  }
  const host = document.createElement('div');
  host.id = %(host)s;
  host.dataset.token = spec.token;
  host.style.cssText = 'all:initial;position:fixed;inset:0;pointer-events:none;z-index:2147483647';
  const root = host.attachShadow({ mode: 'open' });
  root.innerHTML = `<style>
    .ring{position:fixed;border:3px solid #ff6a00;border-radius:6px;
      box-shadow:0 0 0 4px rgba(255,106,0,.25);pointer-events:none}
    .panel{position:fixed;right:16px;bottom:16px;width:320px;pointer-events:auto;background:#fff;color:#222;
      font:14px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
      border-radius:10px;box-shadow:0 8px 30px rgba(0,0,0,.25);padding:14px 16px;border-top:4px solid #ff6a00}
    .title{font-weight:600;margin-bottom:6px}
    .prompt{white-space:pre-wrap;margin-bottom:10px}
    textarea{width:100%%;box-sizing:border-box;min-height:44px;font:inherit;margin-bottom:10px;resize:vertical}
    .row{display:flex;gap:8px;justify-content:flex-end}
    button{font:inherit;border-radius:6px;padding:6px 12px;cursor:pointer;
      border:1px solid #ccc;background:#f5f5f5}
    button.done{background:#ff6a00;border-color:#ff6a00;color:#fff}
  </style>`;
  const rings = boxes.map(([, label]) => {
    const r = document.createElement('div');
    r.className = 'ring'; r.title = label; root.appendChild(r); return r;
  });
  const place = () => boxes.forEach(([e], i) => {
    const b = e.getBoundingClientRect(), r = rings[i].style;
    r.left = (b.left - 4) + 'px'; r.top = (b.top - 4) + 'px';
    r.width = (b.width + 2) + 'px'; r.height = (b.height + 2) + 'px';
    r.display = e.isConnected && b.width && b.height ? 'block' : 'none';
  });
  place();
  const timer = setInterval(place, 250);
  host.__jevStop = () => clearInterval(timer);
  const panel = document.createElement('div');
  panel.className = 'panel';
  const title = document.createElement('div'); title.className = 'title'; title.textContent = spec.title;
  const prompt = document.createElement('div'); prompt.className = 'prompt'; prompt.textContent = spec.prompt;
  const note = document.createElement('textarea'); note.placeholder = '备注（可选）';
  const row = document.createElement('div'); row.className = 'row';
  const send = action => {
    const answer = { token: spec.token, action, note: note.value };
    window.__jevHelpAnswer = answer;
    try {
      if (typeof window[spec.binding] === 'function') window[spec.binding](JSON.stringify(answer));
    } catch (_) {}
    panel.textContent = action === 'continued' ? '已交还控制权' : '已取消';
  };
  const buttons = [['取消请求', 'cancelled', ''], ['完成并交还控制权', 'continued', 'done']];
  for (const [label, action, cls] of buttons) {
    const b = document.createElement('button');
    b.textContent = label; if (cls) b.className = cls;
    b.addEventListener('click', ev => { ev.stopPropagation(); send(action); });
    row.appendChild(b);
  }
  panel.append(title, prompt, note, row);
  root.appendChild(panel);
  (document.body || document.documentElement).appendChild(host);
  return { resolved, unresolved };
})(%(spec)s)
"""

_POLL = r"""
(spec => {
  const host = document.getElementById(%(host)s);
  const answer = window.__jevHelpAnswer;
  const text = () => (document.body ? document.body.innerText : '') || '';
  const results = spec.conditions.map(([kind, value]) => {
    if (kind === 'text_exists') return text().includes(value);
    if (kind === 'text_missing') return !text().includes(value);
    if (kind === 'selector_exists' || kind === 'selector_missing') {
      let e;
      try { e = document.querySelector(value); } catch (_) { return 'invalid'; }
      return kind === 'selector_exists' ? !!e : !e;
    }
    return null;
  });
  return {
    url: location.href,
    present: !!host && host.dataset.token === spec.token,
    answer: answer && answer.token === spec.token ? answer : null,
    results,
  };
})(%(spec)s)
"""

_REMOVE = r"""
(() => {
  const host = document.getElementById(%(host)s);
  if (host) { if (host.__jevStop) host.__jevStop(); host.remove(); }
  delete window.__jevHelpAnswer;
  return true;
})()
"""


def install_js(request: HelpRequest, token: str, binding: str, points: list[dict]) -> str:
    spec = {"token": token, "binding": binding, "title": request.title, "prompt": request.prompt,
            "points": points, "selectors": request.selectors}
    return _INSTALL % {"host": json.dumps(HOST_ID), "spec": json.dumps(spec, ensure_ascii=False)}


def poll_js(request: HelpRequest, token: str) -> str:
    spec = {"token": token, "conditions": [list(c) for c in request.conditions]}
    return _POLL % {"host": json.dumps(HOST_ID), "spec": json.dumps(spec, ensure_ascii=False)}


def remove_js() -> str:
    return _REMOVE % {"host": json.dumps(HOST_ID)}
