"""What turbo mode does when the provider — or the page — misbehaves.

Turbo mode spends money on every step, so its two failure modes are the ones
that cost the most:

  * the *provider* answers something other than the documented contract. The
    decision model is reached through several routes (TypeSafe direct,
    OpenRouter, a gateway), and each can answer with a proxy error page, a
    differently-shaped envelope, or nothing at all. Every one of those is a
    failed decision — no action was executed — and must be reported as such.
    A `KeyError` / `JSONDecodeError` escaping the tool does not say that: the
    host sees a protocol-level crash, cannot tell "the model refused" from
    "the server has a bug", and does not learn that the page is untouched.

  * the *page* keeps invalidating the refs. Re-observing is the right recovery,
    but "bounded" has to mean something specific: bounded per step (one busy
    page mid-goal must not use up the recovery budget of the steps after it)
    and bounded for the whole goal (a page that never settles must not turn
    `max_steps=20` into an unbounded number of billed requests).

No network, no browser, no key: the provider is faked at `policy._post` and the
session at `server._session`.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from jev_ultrafast_mcp import browser, policy, server
from jev_ultrafast_mcp.config import Config
from jev_ultrafast_mcp.observe import Element, Observation


def _observation() -> Observation:
    return Observation(
        url="https://example.test/",
        title="Example",
        text="Example page",
        elements=[Element(ref="e1", role="searchbox", name="Search", editable=True)],
        digest="d",
        text_digest="t",
        page_key="k",
        scroll={"y": 0},
        reachable=1,
        omitted=0,
        overlays=[],
        cross_frames=0,
        cross_frame_srcs=[],
    )


def _cfg() -> Config:
    return dataclasses.replace(Config.from_env(), typesafe_key="test-key")


# ------------------------------------------------------------------ the provider


@pytest.mark.parametrize(
    "payload",
    [
        {},                                              # no envelope at all
        {"answers": {}},                                 # the question we asked is absent
        {"answers": None},                               # answers is the wrong type
        {"answers": {"operation": None}},                # the answer is the wrong type
        {"error": {"message": "rate limited"}},          # an error envelope, HTTP 200
    ],
    ids=["empty", "missing-question", "answers-null", "answer-null", "error-envelope"],
)
def test_a_malformed_answer_is_a_typed_failure_not_a_traceback(monkeypatch, payload):
    """Whatever the route answers, `choose` raises TurboUnavailable — never KeyError/TypeError."""
    monkeypatch.setattr(policy, "_post", lambda url, key, body: payload)

    with pytest.raises(policy.TurboUnavailable):
        policy.choose(_cfg(), _observation(), "search for something", [])


def test_a_malformed_answer_says_which_question_went_unanswered(monkeypatch):
    """The error has to name the gap, or the fix is a guess."""
    monkeypatch.setattr(policy, "_post", lambda url, key, body: {"answers": {}})

    with pytest.raises(policy.TurboUnavailable) as caught:
        policy.choose(_cfg(), _observation(), "search for something", [])

    assert "operation" in str(caught.value)


def _post_answering(choice_for, corrupt=None):
    """A provider that answers every question the server asks, and answers it validly.

    Two questions come back to back -- the operation, then the target for that operation
    -- and their ids are whatever the server offered, so the answers are built from the
    request body rather than written out here. `corrupt` mutates the operation answer,
    which is where a wrong-shaped envelope does its damage.
    """
    def post(url, key, body):
        answers = {}
        for name, question in body["questions"].items():
            ids = sorted(question["criteria"])
            choice = choice_for(name, ids)
            if len(ids) == 1:
                probabilities = {ids[0]: 1.0}
            else:
                share = 0.4 / (len(ids) - 1)
                probabilities = {item: (0.6 if item == choice else share) for item in ids}
            answer = {"choice": choice, "confidence": 0.9, "probabilities": probabilities}
            answers[name] = corrupt(answer) if corrupt and name == "operation" else answer
        return {"answers": answers}
    return post


def _clicking(name, ids):
    return "CLICK" if name == "operation" else ids[0]


def test_the_decision_is_shown_the_text_already_typed_and_what_the_form_holds(monkeypatch):
    """A step is one request. The memory has to travel inside it, or it is gone."""
    captured = {}

    def post(url, key, body):
        captured["body"] = body
        return _post_answering(_clicking)(url, key, body)

    monkeypatch.setattr(policy, "_post", post)
    observation = dataclasses.replace(_observation(), elements=[
        Element(ref="e1", role="textbox", name="添加主题", editable=True, value="研发会议"),
    ])
    history = [{
        "op": "type", "ref": "e1", "target": "添加主题", "ok": True,
        "text": "研发会议", "page_changed": True, "value_after": "研发会议",
    }]

    policy.choose(_cfg(), observation, "fill the title", history)

    state = captured["body"]["state"]
    assert state["recent_actions"] == history
    assert state["filled"] == {"添加主题": "研发会议"}


class _Reads:
    """A page whose successive reads are scripted; the last one repeats."""

    def __init__(self, *pages: Observation):
        self.pages = list(pages)
        self.reads = 0
        self.last = None

    def observe(self):
        self.reads += 1
        page = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        self.last = page
        return page


def test_after_typing_a_name_the_loop_waits_for_its_option_row():
    """The people list is not in the snapshot taken with the keystrokes."""
    field = Element(ref="e1", role="textbox", name="People", editable=True, kind="suggest")
    early = dataclasses.replace(_observation(), elements=[field])
    spinner = dataclasses.replace(early, digest="spinner")
    rows = dataclasses.replace(early, elements=[field, Element(ref="e2", role="option",
                                                               name="杨飞 技术部")])
    page = _Reads(spinner, rows)

    got = server._await_suggestions(page, early, timeout=1.0)

    assert [element.ref for element in got.elements] == ["e1", "e2"]
    assert page.reads == 2, "a spinner is a change, not the rows"


def test_waiting_for_rows_gives_up_without_them():
    field = Element(ref="e1", role="textbox", name="People", editable=True, kind="suggest")
    early = dataclasses.replace(_observation(), elements=[field])
    page = _Reads(early)

    assert server._await_suggestions(page, early, timeout=0.3) is early


def _suggest_page(*extra: Element) -> tuple[Observation, Observation]:
    """A people field before typing, and the same page once its rows arrived."""
    empty = Element(ref="e1", role="textbox", name="People", editable=True, kind="suggest")
    typed = dataclasses.replace(empty, value="杨飞")
    person = Element(ref="e2", role="option", name="杨飞 技术部")
    before = dataclasses.replace(_observation(), elements=[empty, *extra])
    after = dataclasses.replace(_observation(), elements=[typed, person, *extra], digest="rows")
    return before, after


def _scripted_reads(session: "_FakeSession", before: Observation, after: Observation) -> None:
    """Reads show `before` until something has been acted on, then `after`."""
    def observe(**_kwargs):
        session.events.append("observe")
        session.last = after if session.acts else before
        return session.last
    session.page = before
    session.observe = observe


def _goal_with(monkeypatch, session, decisions):
    queue = list(decisions)

    def fake_choose(_cfg, _observation, _goal, _history):
        return queue.pop(0) if queue else {"operation": "DONE", "confidence": 0.9}

    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", fake_choose)
    monkeypatch.setattr(server.policy, "text_for", lambda *_args, **_kwargs: "杨飞")
    monkeypatch.setattr(server, "_session", lambda _name: session)
    return server.browser_goal("invite the person", max_steps=4, verbose=True)


def _type_people() -> dict:
    return {"operation": "TYPE_TEXT", "ref": "e1", "confidence": 0.9, "target": "People"}


def test_one_malformed_decision_is_asked_again(monkeypatch):
    """A single bad envelope used to abandon a form that was already filled in."""
    session = _FakeSession([])
    calls = {"n": 0}

    def fake_choose(_cfg, _observation, _goal, _history):
        calls["n"] += 1
        if calls["n"] == 1:
            raise policy.MalformedAnswer("Decision model returned a malformed answer; no action executed.")
        return {"operation": "DONE", "confidence": 0.9}

    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", fake_choose)
    monkeypatch.setattr(server, "_session", lambda _name: session)

    out = server.browser_goal("finish the form", max_steps=3, verbose=True)

    assert "status: done" in out, out
    assert "Asking again." in out
    assert "turbo: 2 decisions" in out, "the request that failed was still a request"


def test_a_provider_outage_is_not_retried_as_a_flake(monkeypatch):
    session = _FakeSession([])
    calls = {"n": 0}

    def fake_choose(_cfg, _observation, _goal, _history):
        calls["n"] += 1
        raise policy.TurboUnavailable("Decision model unreachable; no action executed.")

    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", fake_choose)
    monkeypatch.setattr(server, "_session", lambda _name: session)

    out = server.browser_goal("finish the form", max_steps=3, verbose=True)

    assert "turbo_unavailable" in out
    assert calls["n"] == 1


def test_submitting_while_a_suggestion_is_open_selects_it_first(monkeypatch):
    """Submit while the person row is on screen stored the event with nobody invited."""
    save = Element(ref="e3", role="button", name="Save", kind="submit")
    session = _FakeSession([_ok(), _ok()])
    _scripted_reads(session, *_suggest_page(save))

    out = _goal_with(monkeypatch, session, [
        _type_people(),
        {"operation": "CLICK", "ref": "e3", "confidence": 0.9, "target": "Save"},
    ])

    assert "status: done" in out, out
    assert session.acts[1][0] == {"op": "click", "ref": "e2"}, session.acts


def test_a_button_that_is_not_submit_is_left_to_the_model(monkeypatch):
    """Only `kind=submit` is redirected; any other click is the model's own call."""
    other = Element(ref="e3", role="button", name="Save draft")
    session = _FakeSession([_ok(), _ok()])
    _scripted_reads(session, *_suggest_page(other))

    out = _goal_with(monkeypatch, session, [
        _type_people(),
        {"operation": "CLICK", "ref": "e3", "confidence": 0.9, "target": "Save draft"},
    ])

    assert "status: done" in out, out
    assert session.acts[1][0] == {"op": "click", "ref": "e3"}, session.acts


def test_a_click_on_the_field_just_typed_selects_the_suggestion(monkeypatch):
    """Typing the name and then clicking the box was closing the list."""
    session = _FakeSession([_ok(), _ok()])
    _scripted_reads(session, *_suggest_page())

    out = _goal_with(monkeypatch, session, [
        _type_people(),
        {"operation": "CLICK", "ref": "e1", "confidence": 0.9, "target": "People"},
    ])

    assert "status: done" in out, out
    assert session.acts[1][0] == {"op": "click", "ref": "e2"}, session.acts
    assert "choosing e2 杨飞 技术部" in out


def test_typing_the_next_name_selects_the_open_suggestion_first(monkeypatch):
    """Typing 张晋伟 over an unchosen 杨飞 left only the second person invited."""
    session = _FakeSession([_ok(), _ok()])
    _scripted_reads(session, *_suggest_page())

    out = _goal_with(monkeypatch, session, [_type_people(), _type_people()])

    assert "status: done" in out, out
    assert session.acts[1][0] == {"op": "click", "ref": "e2"}, session.acts
    assert "choosing e2 杨飞 技术部" in out


def test_finishing_with_a_suggestion_open_selects_it_first(monkeypatch):
    """A DONE with the person's row still open reported a form that was missing them."""
    session = _FakeSession([_ok(), _ok()])
    _scripted_reads(session, *_suggest_page())

    out = _goal_with(monkeypatch, session, [
        _type_people(),
        {"operation": "DONE", "confidence": 0.9},
    ])

    assert "status: done" in out, out
    assert session.acts[1][0] == {"op": "click", "ref": "e2"}, session.acts
    assert "before DONE" in out


def test_clicking_an_empty_suggest_field_types_the_goal_s_value(monkeypatch):
    """The people box was clicked twice and the name was never entered."""
    session = _FakeSession([_ok()])
    _scripted_reads(session, *_suggest_page())

    out = _goal_with(monkeypatch, session, [
        {"operation": "CLICK", "ref": "e1", "confidence": 0.9, "target": "People"},
    ])

    assert "status: done" in out, out
    assert session.acts[0][0]["op"] == "type", session.acts
    assert session.acts[0][0]["text"] == "杨飞"
    assert "typing instead" in out


def test_the_goal_remembers_the_text_and_whether_the_page_moved(monkeypatch):
    """The second decision sees the first step's text, not only that a field was touched."""
    session = _FakeSession([
        {"ok": True, "page_changed": True},
        {"ok": True, "page_changed": False},
    ])
    seen: list[list] = []

    def fake_choose(_cfg, _observation, _goal, history):
        seen.append(list(history))
        if len(seen) == 1:
            return {"operation": "TYPE_TEXT", "ref": "e1", "confidence": 0.9, "target": "Search"}
        if len(seen) == 2:
            return _click()
        return {"operation": "DONE", "confidence": 0.9}

    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", fake_choose)
    monkeypatch.setattr(server.policy, "text_for", lambda *_args, **_kwargs: "研发会议")
    monkeypatch.setattr(server, "_session", lambda _name: session)

    out = server.browser_goal("type the title", max_steps=5, verbose=True)

    assert "status: done" in out, out
    assert seen[0] == []
    assert seen[1][0]["text"] == "研发会议"
    assert seen[1][0]["page_changed"] is True
    assert seen[1][0]["op"] == "type"
    assert seen[2][1]["page_changed"] is False
    assert seen[2][1]["op"] == "click"


def test_a_well_formed_answer_resolves_to_a_target(monkeypatch):
    """The happy path of the paid route, which nothing reached before.

    Every other case in this file stops at the first question, so `_validate`'s body and
    the target resolution under it had never been executed at all -- by the suite or by
    `smoke.py`. `turbo_check.py` would, and CI sets no model key, so it never ran there
    either.
    """
    monkeypatch.setattr(policy, "_post", _post_answering(_clicking))

    decision = policy.choose(_cfg(), _observation(), "search for something", [])

    assert decision["operation"] == "CLICK"
    assert decision["ref"] == "e1"
    assert decision["target"] == "Search"
    assert decision["latency_ms"] >= 0


@pytest.mark.parametrize(
    "probabilities",
    [[0.9, 0.1], "0.9", None, 1],
    ids=["list", "string", "null", "number"],
)
def test_a_wrong_shaped_probabilities_is_a_typed_failure_not_a_traceback(monkeypatch, probabilities):
    """`.values()` is not free on whatever the route sent back.

    A list, a string or null raises AttributeError, and `_validate` caught only
    KeyError/TypeError/ValueError -- so a proxy answering with a differently-shaped
    envelope crashed the tool instead of reporting that nothing had been executed, which
    is the one outcome the caller cannot tell apart from a bug in this server.
    """
    monkeypatch.setattr(policy, "_post", _post_answering(
        _clicking, corrupt=lambda answer: {**answer, "probabilities": probabilities}))

    with pytest.raises(policy.TurboUnavailable):
        policy.choose(_cfg(), _observation(), "search for something", [])


def _without(key):
    def mutate(answer):
        return {item: value for item, value in answer.items() if item != key}
    return mutate


def _not_the_argmax(answer):
    """A distribution over the real ids in which the chosen one is not the largest."""
    ids = sorted(answer["probabilities"])
    other = next(item for item in ids if item != answer["choice"])
    probabilities = {item: 0.1 / (len(ids) - 1) for item in ids}
    probabilities[other] = 0.9
    return {**answer, "probabilities": probabilities}


def _scaled(factor):
    def mutate(answer):
        return {**answer, "probabilities": {item: value * factor
                                            for item, value in answer["probabilities"].items()}}
    return mutate


@pytest.mark.parametrize("mutate", [
    _without("choice"),
    _without("confidence"),
    lambda answer: {**answer, "confidence": 2},
    lambda answer: {**answer, "choice": "NOT_AN_ID"},
    lambda answer: {**answer, "probabilities": {}},
    _not_the_argmax,
    _scaled(0.5),
], ids=["no-choice", "no-confidence", "confidence-out-of-range", "choice-not-offered",
        "no-ids", "not-the-argmax", "sum-not-1"])
def test_an_inconsistent_answer_is_refused(monkeypatch, mutate):
    """The consistency rules the guard exists for, reachable only through it."""
    monkeypatch.setattr(policy, "_post", _post_answering(_clicking, corrupt=mutate))

    with pytest.raises(policy.TurboUnavailable) as caught:
        policy.choose(_cfg(), _observation(), "search for something", [])

    assert "malformed answer" in str(caught.value)
    assert "no action executed" in str(caught.value)


class _Body:
    """A 200 response whose body is not JSON — a proxy or gateway error page."""

    status_code = 200
    is_error = False

    def json(self):
        raise json.JSONDecodeError("Expecting value", "<html>502 Bad Gateway</html>", 0)


class _Client:
    def post(self, *args, **kwargs):
        return _Body()


def test_a_non_json_body_is_a_typed_failure_not_a_traceback(monkeypatch):
    monkeypatch.setattr(policy, "CLIENT", _Client())

    with pytest.raises(policy.TurboUnavailable) as caught:
        policy._post("https://example.test/decisions", "test-key", {})

    assert "JSON" in str(caught.value)
    assert "no action executed" in str(caught.value)


def test_the_tool_reports_a_turbo_failure_under_one_prefix():
    """`turbo_unavailable:` is the vocabulary the tool documents; keep it for every path."""
    assert server._error(policy.TurboUnavailable("no key")).startswith("turbo_unavailable:")


# --------------------------------------------------------------------- the page


class _FakeSession:
    """The parts of `browser.Session` the goal loop touches, scripted."""

    def __init__(self, act_results: list[dict]):
        self._act = list(act_results)
        self.history: list = []
        self.last: Observation | None = None
        self.acts: list[list[dict]] = []
        self.resets = 0
        self._streak = 0
        # Order matters for the handoff: a goal that navigates has to do it
        # before it reads the page, or it plans against the page it just left.
        self.events: list[str] = []
        # Scripted answers to "has the page stopped fetching?". Empty means
        # "yes", which is what a session that did not navigate reports.
        self.idle: list[bool] = []
        self.idle_questions = 0
        # The page the session is driving. `follow_new_tab` replaces it, which
        # is what lets the next decision see the tab it just followed.
        self.page = _observation()
        self.target_id = "current"
        self.followed: list[dict] = []

    def navigate(self, url: str) -> None:
        self.events.append(f"navigate {url}")

    def reset_progress(self) -> None:
        self.resets += 1
        self._streak = 0

    def page_is_idle(self) -> bool:
        self.idle_questions += 1
        return self.idle.pop(0) if self.idle else True

    def observe(self, **_kwargs) -> Observation:
        self.events.append("observe")
        self.last = self.page
        return self.last

    def follow_new_tab(self, observation: Observation) -> dict | None:
        """The same rule as `Session.follow_new_tab`: only a tab this page opened.

        Scripted by the observation the goal hands in, not by a side channel,
        so a test that wants a follow has to put the tab on `new_tabs` with
        this session's `target_id` as its opener. Anything else is declined,
        which is the case the second test exists to pin.
        """
        opened = [
            tab for tab in observation.new_tabs
            if tab.get("opener") == self.target_id and tab.get("target_id")
        ]
        if not opened:
            return None
        chosen = opened[-1]
        self.followed.append(chosen)
        self._streak = 0
        landed = chosen.get("observation")
        if landed is not None:
            self.page = landed
        return chosen

    def evaluate_js(self, _expression: str):
        # A real session evaluates JS against the page. Refusing loudly here
        # keeps a `js` check from passing against a fixture that never ran it.
        raise AssertionError("the scripted session does not evaluate JS")

    def act(self, ops, **_kwargs) -> dict:
        self.acts.append(ops)
        scripted = self._act.pop(0) if self._act else {"ok": True}
        step = {"op": ops[0]["op"], "ok": scripted.get("ok", True), "ms": 1}
        if not step["ok"]:
            step["error"] = scripted["error"]
        payload = {"ops": [step], "ok": step["ok"], "steps": len(self.acts)}
        if not step["ok"]:
            payload["view"] = "= no change"
        # Mirror the real session rather than letting a test assert `stuck` into
        # existence: the loop has to read the same contract the browser writes,
        # so the fake counts the streak the way `browser.Session.act` does and
        # derives `stuck` from the shared limit.
        changed = scripted.get("page_changed", True)
        self._streak = 0 if changed else self._streak + 1
        payload["page_changed"] = changed
        # A click that opens a tab does not change the page it was clicked on.
        # The goal reads `tab.last` for the follow, so the opened tab has to be
        # on that observation or the loop never hears about it.
        if scripted.get("new_tab"):
            self.last = dataclasses.replace(self.page, new_tabs=[scripted["new_tab"]])
        if scripted.get("then_page") is not None:
            self.page = scripted["then_page"]
        if self._streak >= browser.NO_CHANGE_LIMIT:
            payload["stuck"] = (
                f"{self._streak} consecutive actions changed nothing. "
                "Do not retry the same ref: re-read the observation, look for a "
                "covering dialog, or change strategy."
            )
        return payload


def _stale() -> dict:
    return {"ok": False, "error": "detached"}


def _ok() -> dict:
    return {"ok": True}


def _still() -> dict:
    """A step that succeeded and left the page exactly as it was."""
    return {"ok": True, "page_changed": False}


def _wait() -> dict:
    return {"operation": "WAIT", "ref": None, "confidence": 0.6}


def _checked_in() -> Observation:
    """The page after a check-in: the button is gone, the proof is in the text."""
    return dataclasses.replace(_observation(),
                               text="Daily Rewards  Streak 5  Points 130  Checked in today")


def _drive(monkeypatch, session: _FakeSession, decisions: list[dict], max_steps: int = 20,
           verify: list[dict] | None = None, url: str = ""):
    """Run `browser_goal` against a scripted session and decision sequence."""
    seen: list[dict] = []

    def fake_choose(_cfg, _observation, _goal, _history):
        seen.append(_observation)
        if not decisions:
            return {"operation": "DONE", "ref": None, "confidence": 1.0}
        nxt = decisions.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", fake_choose)
    monkeypatch.setattr(server, "_session", lambda _name: session)
    return server.browser_goal("do the thing", url=url, max_steps=max_steps, verify=verify,
                               verbose=True), seen


def _click() -> dict:
    return {"operation": "CLICK", "ref": "e1", "confidence": 0.9}


def test_a_declined_field_does_not_end_the_goal(monkeypatch):
    """The helper answering null used to abort a goal that had other moves.

    The field stops being offered for typing. The next decision can click, and
    the run is not reported as turbo_unavailable.
    """
    session = _FakeSession([_ok()])
    calls = {"n": 0}

    def fake_choose(_cfg, observation, _goal, _history):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"operation": "TYPE_TEXT", "ref": "e1", "confidence": 0.9, "target": "Search"}
        assert observation.by_ref["e1"].editable is False, "the declined field was offered again"
        if calls["n"] == 2:
            return _click()
        return {"operation": "DONE", "confidence": 0.9}

    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", fake_choose)
    monkeypatch.setattr(server.policy, "text_for",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            policy.TextDeclined("nothing to type into e1 Search")))
    monkeypatch.setattr(server, "_session", lambda _name: session)

    out = server.browser_goal("do the thing", max_steps=5, verbose=True)

    assert "turbo_unavailable" not in out, out
    assert "status: done" in out, out
    assert session.acts[0][0]["op"] == "click"
    assert calls["n"] == 3


def test_the_handoff_opens_the_page_before_it_plans(monkeypatch):
    """`url` is what makes the handoff complete: one call, no pre-opened page.

    The order is the substance, not a detail. A goal that reads the page before
    navigating plans against whatever the session happened to be showing -- and
    on a session that has never opened anything that is `about:blank`, so the
    model is asked to work out a task from an empty page.
    """
    session = _FakeSession([_ok()])
    decisions = [_click(), {"operation": "DONE", "confidence": 0.9}]

    out, _seen = _drive(monkeypatch, session, decisions, url="https://example.test/checkin")

    assert session.events[0] == "navigate https://example.test/checkin", (
        f"the goal must open the page before it reads it, got {session.events[:3]}")
    assert session.events[1] == "observe", session.events[:3]
    assert "status: done" in out, out


def test_a_goal_without_a_url_runs_on_the_page_it_is_given(monkeypatch):
    """Continuing from an earlier page is why `url` is optional, not required."""
    session = _FakeSession([_ok()])

    _drive(monkeypatch, session, [{"operation": "DONE", "confidence": 0.9}])

    assert session.events[0] == "observe", session.events[:3]
    assert not [e for e in session.events if e.startswith("navigate")], session.events[:3]


def _drive_until(monkeypatch, session: _FakeSession, choose):
    """Run a goal whose next action depends on the page it is looking at."""
    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", choose)
    monkeypatch.setattr(server, "_session", lambda _name: session)
    return server.browser_goal("open the calendar", max_steps=6, verbose=True)


def _click_until_calendar(_cfg, observation, _goal, _history):
    """Click the same control while still on the page that has not moved."""
    if observation.url.endswith("/calendar/week"):
        return {"operation": "DONE", "ref": None, "confidence": 0.9}
    return _click()


def test_a_goal_follows_a_tab_the_page_just_opened(monkeypatch):
    """One click, one new tab, and the next decision is about that tab.

    The page that was clicked does not change -- the week view arrives
    somewhere else -- so without the follow the same control is clicked until
    the no-change streak calls the goal stuck, and every click opens another
    copy. Following is what stops the second click.
    """
    calendar = dataclasses.replace(_observation(), url="https://feishu.test/calendar/week",
                                   title="本周")
    session = _FakeSession([])
    opened = {
        "target_id": "cal",
        "url": calendar.url,
        "opener": session.target_id,
        "observation": calendar,
    }
    session._act = [{**_still(), "new_tab": opened}] + [_still()] * 5

    out = _drive_until(monkeypatch, session, _click_until_calendar)

    assert len(session.acts) == 1, session.acts
    assert [tab["target_id"] for tab in session.followed] == ["cal"]
    assert "followed new tab https://feishu.test/calendar/week" in out, out
    assert "stopped: no progress" not in out, out
    assert "status: done" in out, out


def test_a_goal_follows_a_tab_the_previous_read_already_reported(monkeypatch):
    """A tab reported before the goal starts is still the page to drive.

    `new_tabs` is a diff, and the read that reported it already consumed the
    diff. The first read of this goal would otherwise agree with itself and
    hand the model the page that did the opening, which is how the same
    control gets clicked again.
    """
    calendar = dataclasses.replace(_observation(), url="https://feishu.test/calendar/week",
                                   title="本周")
    session = _FakeSession([])
    session.last = dataclasses.replace(_observation(), new_tabs=[{
        "target_id": "cal",
        "url": calendar.url,
        "opener": session.target_id,
        "observation": calendar,
    }])

    out = _drive_until(monkeypatch, session, _click_until_calendar)

    assert session.acts == [], session.acts
    assert [tab["target_id"] for tab in session.followed] == ["cal"]
    assert "followed new tab https://feishu.test/calendar/week" in out, out
    assert "status: done" in out, out


def test_a_goal_does_not_follow_a_tab_it_did_not_open(monkeypatch):
    """A tab whose opener is some other page is the user's, not this click's.

    Declining it leaves the goal on the page that did not change, and that
    page then goes stuck the way any page that ignores its clicks does. The
    point of the assertion is the absence: no follow, and no second calendar.
    """
    session = _FakeSession([])
    foreign = {"target_id": "other", "url": "https://github.com/pulls", "opener": "someone-else"}
    session._act = [{**_still(), "new_tab": foreign}] + [_still()] * 5

    out = _drive_until(monkeypatch, session, _click_until_calendar)

    assert session.followed == [], session.followed
    assert "followed new tab" not in out, out
    assert "stopped: no progress" in out, out
    assert len(session.acts) == browser.NO_CHANGE_LIMIT, session.acts


def test_a_stale_ref_does_not_spend_the_next_step_s_budget(monkeypatch):
    """Four steps each hitting one stale ref all recover.

    The per-step budget has to reset, or a page that invalidates a ref once per
    step exhausts the goal's recovery on step four and fails a goal that was
    working — which is exactly how the fix for `detached` regressed in spirit
    while looking, from the source, like a bounded retry.
    """
    session = _FakeSession([_stale(), _ok()] * 4)
    # Recovering costs a decision: the step that went stale is attempted again,
    # so four successful steps need eight decisions.
    decisions = [_click() for _ in range(8)] + [{"operation": "DONE", "confidence": 0.9}]

    out, _seen = _drive(monkeypatch, session, decisions)

    assert "status: done" in out, out
    assert "steps: 4" in out, out
    assert out.count("re-observing") == 4, "each step got its own recovery"

    # Each stale ref was re-observed and then re-tried: 4 stale + 4 real steps.
    assert len(session.acts) == 8


def test_a_page_that_never_settles_ends_the_step(monkeypatch):
    """One step that stays stale is bounded by the per-step budget, not by max_steps."""
    session = _FakeSession([_stale()] * 50)

    out, seen = _drive(monkeypatch, session, [_click()] * 50)

    assert "failed:detached" in out, out
    assert len(seen) == server.STALE_REF_RETRIES + 1, "3 recoveries, then the step ends"


def test_recovery_cannot_multiply_the_request_count(monkeypatch):
    """A goal-wide ceiling keeps `max_steps` a bound on billed requests.

    Per-step recovery alone multiplies the worst case by `STALE_REF_RETRIES`, so
    a page that is stale on every step would turn `max_steps=3` into 12 requests.
    """
    session = _FakeSession(([_stale()] * 3 + [_ok()]) * 20)

    out, seen = _drive(monkeypatch, session, [_click()] * 50, max_steps=3)

    assert len(seen) <= 2 * 3, f"{len(seen)} requests for max_steps=3"
    assert "failed:" in out, out


PROOF = [{"type": "text_contains", "text": "Checked in today"}]


def test_the_page_beats_a_blocked_model_when_it_proves_the_goal(monkeypatch):
    """An assertion is a fact; `BLOCKED` is an opinion. The fact wins.

    This is not a rare disagreement, it is the ordinary shape of a goal whose
    last action removes what it acted on. Clicking a check-in button makes the
    button disappear, so the model -- correctly, given what it can see --
    reports that it has nothing to act on, on a goal that in fact succeeded.
    """
    session = _FakeSession([_ok()])
    monkeypatch.setattr(session, "observe", lambda **_kwargs: _checked_in())

    out, _seen = _drive(monkeypatch, session,
                        [_click(), {"operation": "BLOCKED", "ref": None, "confidence": 1.0}],
                        verify=PROOF)

    assert "status: done" in out, out
    assert "verified: PASS" in out, out
    assert "the assertion wins" in out, out


def test_a_blocked_model_stays_blocked_when_the_page_does_not_prove_it(monkeypatch):
    """The converse, so the rule above cannot be satisfied by optimism alone.

    The first BLOCKED is re-read (see the mounting tests below), so a genuinely
    blocked goal has to be blocked on the second answer too: the re-read buys a
    second look, not a different verdict.
    """
    session = _FakeSession([_ok()])
    blocked = {"operation": "BLOCKED", "ref": None, "confidence": 1.0}

    out, seen = _drive(monkeypatch, session, [dict(blocked), dict(blocked)], verify=PROOF)

    assert "status: blocked" in out, out
    assert "verified: FAIL" in out, out
    assert len(seen) == 2, "the first BLOCKED is re-read, the second is believed"


def test_a_model_that_says_done_over_an_unproven_page_is_not_reported_as_done(monkeypatch):
    """The same rule pointing the other way, and the direction that costs more.

    The rule above upgrades `BLOCKED` to `done` when the page proves the goal.
    This is the converse: a model that reports DONE over a page that does not
    prove it has not finished the goal, it has run out of ideas. Measured on a
    real daily check-in, twice -- `status: done` after four steps, and `status:
    done` at step 0 with the element it had been told to click not even on the
    page -- while the points balance had not moved either time. A caller that
    reads only `status` stops on an unfinished task, which is the one error
    `verify` exists to prevent.
    """
    session = _FakeSession([_ok()])

    out, _seen = _drive(monkeypatch, session,
                        [_click(), {"operation": "DONE", "confidence": 0.9}], verify=PROOF)

    assert "status: done" not in out, out
    assert "status: unconfirmed" in out, out
    assert "verified: FAIL" in out, out
    assert "the assertion wins" in out, out


def test_done_without_a_verify_is_still_taken_at_its_word(monkeypatch):
    """With no assertion there is no second opinion, so the summary stands.

    Pins the scope of the rule above: it fires on `verify` disagreeing with
    `done`, not on `done` being doubted in general.
    """
    session = _FakeSession([_ok()])

    out, _seen = _drive(monkeypatch, session,
                        [_click(), {"operation": "DONE", "confidence": 0.9}])

    assert "status: done" in out, out
    assert "verified:" not in out, out


# ------------------------------------------------------ the page has stopped moving


def test_a_page_that_stops_changing_ends_the_goal_instead_of_burning_max_steps(monkeypatch):
    """`act` counts how long the page has stood still; the loop has to read it.

    Measured on a real daily check-in: the submit succeeded, the page never
    changed again, and the model spent four WAITs discovering it. The run then
    reported "stopped: hit max_steps=6", which reads as "still working" when the
    truth is "there is nothing left to do". The count was already being kept --
    the loop simply dropped it, so the only bound on a stalled goal was the
    caller's step budget.
    """
    session = _FakeSession([_ok()] + [_still()] * 5)

    out, _seen = _drive(monkeypatch, session, [_click()] + [_wait()] * 5, max_steps=20)

    assert "status: stopped: no progress" in out, out
    assert "hit max_steps" not in out, out
    # Stopped once the page had been still for the shared limit -- not at the
    # end of the budget, and not one step earlier than the limit allows.
    assert len(session.acts) == browser.NO_CHANGE_LIMIT + 1, len(session.acts)


def test_a_goal_that_stalls_after_succeeding_is_confirmed_by_its_assertion(monkeypatch):
    """Stopping early must not turn a finished goal into a failed one.

    The commonest way to stall is to have finished: the last action removed the
    very thing it acted on. The observation is refreshed *before* the break for
    this reason -- `verify` judges the page as it stands now, so the assertion
    still gets to say the goal was met.
    """
    session = _FakeSession([_ok()] + [_still()] * 5)
    monkeypatch.setattr(session, "observe", lambda **_kwargs: _checked_in())

    out, _seen = _drive(monkeypatch, session, [_click()] + [_wait()] * 5,
                        max_steps=20, verify=PROOF)

    assert "status: done" in out, out
    assert "verified: PASS" in out, out
    assert "the assertion wins" in out, out


def test_the_stall_count_does_not_carry_from_one_goal_into_the_next(monkeypatch):
    """The streak belongs to a run, not to the session that serves it.

    The count is only ever incremented, so a goal handed a session a previous
    goal left near the limit would call itself stuck before it had acted once.
    """
    session = _FakeSession([_still(), _ok()])
    session._streak = browser.NO_CHANGE_LIMIT - 1  # what the previous goal left behind

    out, _seen = _drive(monkeypatch, session, [_click(), _click()], max_steps=2)

    assert "no progress" not in out, out
    assert session.resets == 1, "a goal starts its stall count from zero"


# ------------------------------------------------------ the page is not ready yet


def _thin() -> Observation:
    """The page as it reads before its JavaScript has mounted the header."""
    return dataclasses.replace(_observation(), elements=[])


def _mounted() -> Observation:
    """The same page once the header — and the menu the goal is after — is there."""
    return dataclasses.replace(
        _observation(),
        elements=[Element(ref="e9", role="button", name="Today's tasks")])


def test_the_first_read_waits_for_the_page_to_stop_growing(monkeypatch):
    """`load` is not "rendered": the header arrives afterwards.

    The observer's own settle pass only waits while there is *nothing*
    actionable at all, so on a content-rich page it answers immediately and
    misses exactly the late half — on 1point3acres the signed-in state and the
    daily-task menu. A goal handed that table can only answer BLOCKED.
    """
    session = _FakeSession([])
    reads = iter([_thin(), _mounted(), _mounted()])
    monkeypatch.setattr(session, "observe", lambda **_kwargs: next(reads, _mounted()))

    settled = server._first_read(session)

    assert [element.ref for element in settled.elements] == ["e9"], (
        "the first read must be the settled page, not the one that answered first")


def test_a_shell_that_is_still_fetching_is_not_settled_by_agreement(monkeypatch):
    """Still is not the same as finished.

    A client-rendered app's shell holds the same few elements for as long as its
    bundle takes to arrive, so "two reads agreed" settles on a page with no
    controls on it. Measured on cloudstudio.net's user centre: three elements
    across every poll, `BLOCKED` twice on the same page, and the rendered
    version one second away. Agreement is evidence only once the page has also
    stopped fetching.
    """
    session = _FakeSession([])
    reads = iter([_thin(), _thin(), _thin(), _thin(), _mounted(), _mounted()])
    monkeypatch.setattr(session, "observe", lambda **_kwargs: next(reads, _mounted()))
    session.idle = [False, False, False, True]

    settled = server._first_read(session)

    assert [element.ref for element in settled.elements] == ["e9"], (
        "the shell was accepted because it was still, not because it was finished")
    assert session.idle_questions == 4, (
        f"the page was asked {session.idle_questions} times whether it had finished")


def test_a_page_that_never_stops_fetching_costs_one_budget_and_no_more(monkeypatch):
    """A page that never drains must not hold the goal open.

    `page_is_idle` answers "may I stop?", so a site whose requests never finish
    has to end in a read of whatever is on screen rather than in a wait that
    outlives the budget.
    """
    monkeypatch.setattr(server.CONFIG, "settle_timeout", 0.3)
    monkeypatch.setattr(server.CONFIG, "settle_poll_ms", 20)
    session = _FakeSession([])
    reads = 0

    def counting(**_kwargs):
        nonlocal reads
        reads += 1
        return _thin()

    monkeypatch.setattr(session, "observe", counting)
    session.idle = [False] * 500

    settled = server._first_read(session)

    assert settled.elements == []
    assert reads <= 20, f"{reads} reads for one page that never went quiet"


def test_a_blocked_first_answer_is_re_read_before_it_is_believed(monkeypatch):
    """BLOCKED on the first read is usually the page, not the goal.

    The menu is not missing, it is late. Believing the first answer ends a goal
    that would have worked one second later, so the loop spends one re-read
    before it accepts "nothing to act on".
    """
    session = _FakeSession([_ok()])
    reads = iter([_thin(), _thin(), _mounted(), _mounted(), _mounted()])
    monkeypatch.setattr(session, "observe", lambda **_kwargs: next(reads, _mounted()))

    out, seen = _drive(monkeypatch, session,
                       [{"operation": "BLOCKED", "ref": None, "confidence": 0.6},
                        {"operation": "CLICK", "ref": "e9", "confidence": 0.9},
                        {"operation": "DONE", "confidence": 0.9}])

    assert "re-reading" in out, out
    assert "status: done" in out, out
    assert [element.ref for element in seen[0].elements] == [], (
        "the first look was the page that had not mounted yet")
    assert [element.ref for element in seen[1].elements] == ["e9"], (
        "the re-read found the menu, which is what unblocked the goal")


def test_the_re_read_is_bounded_so_max_steps_still_bounds_the_bill(monkeypatch):
    """A page that never mounts must not turn BLOCKED into a retry loop."""
    session = _FakeSession([])
    monkeypatch.setattr(session, "observe", lambda **_kwargs: _thin())
    blocked = {"operation": "BLOCKED", "ref": None, "confidence": 1.0}

    out, seen = _drive(monkeypatch, session, [dict(blocked)] * 10)

    assert "status: blocked" in out, out
    assert len(seen) == 2, f"{len(seen)} requests for one BLOCKED answer"


def test_a_provider_failure_mid_goal_keeps_what_the_goal_already_did(monkeypatch):
    """The trace must survive a provider that dies on step three.

    Losing the steps already taken is the failure the host cannot debug: it
    knows the goal failed, not that it had already clicked twice and was one
    step from done.
    """
    session = _FakeSession([_ok(), _ok()])
    decisions = [_click(), _click(), policy.TurboUnavailable("Decision model unreachable")]

    out, _seen = _drive(monkeypatch, session, decisions)

    assert "turbo_unavailable" in out, out
    assert "1. CLICK e1" in out and "2. CLICK e1" in out, out


# --------------------------------------------------------------- what it cost

@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"total_tokens": 500, "prompt_tokens": 400, "completion_tokens": 100}, 500),
        ({"prompt_tokens": 400, "completion_tokens": 100}, 500),
        ({"input_tokens": 400, "output_tokens": 100}, 500),
        ({"totalTokens": 500}, 500),
        ({}, 0),
        (None, 0),
        ("500", 0),
        ({"total_tokens": True}, 0),
        ({"cached_tokens": 12}, 0),
    ],
    ids=["total-wins", "prompt-completion", "input-output", "camel", "empty", "none", "not-a-dict",
         "bool-is-not-a-count", "unknown-key"],
)
def test_the_token_count_does_not_double_count_a_total(usage, expected):
    """Routes name the same two numbers differently, and some add a total.

    Summing every key that contains "token" would add a total to its own parts
    and report twice what the goal spent — an inflated number in the one line a
    reader uses to judge whether delegating is worth it.
    """
    assert server._tokens(usage) == expected


def test_a_goal_reports_what_the_handoff_cost(monkeypatch):
    """The claim is that delegating saves the caller turns; this is the bill.

    Both halves matter: how many decisions the server made on the caller's
    behalf, and how much of the wall time was the model versus the page. A
    server that spends the caller's money owes them the count.
    """
    session = _FakeSession([_ok(), _ok()])
    decisions = [
        {"operation": "CLICK", "ref": "e1", "confidence": 0.9, "latency_ms": 300,
         "usage": {"prompt_tokens": 400, "completion_tokens": 20}},
        {"operation": "CLICK", "ref": "e1", "confidence": 0.9, "latency_ms": 300,
         "usage": {"total_tokens": 500, "prompt_tokens": 400, "completion_tokens": 100}},
        {"operation": "DONE", "ref": None, "confidence": 0.9, "latency_ms": 250,
         "usage": {"prompt_tokens": 300, "completion_tokens": 10}},
    ]

    out, _seen = _drive(monkeypatch, session, decisions)

    assert "turbo: 3 decisions" in out, out
    assert "1,230 tokens" in out, out
    assert "0.8s model" in out, out


def test_a_goal_that_never_reached_the_model_reports_no_cost(monkeypatch):
    """A refused plan costs nothing, and must not print a budget that implies otherwise."""
    session = _FakeSession([])
    monkeypatch.setattr(server.policy, "available", lambda _cfg: False)
    monkeypatch.setattr(server, "_session", lambda _name: session)

    out = server.browser_goal("do the thing", verbose=True)

    assert "turbo_unavailable" in out, out
    assert "turbo: " not in out, out


# ------------------------------------------------- which new tab gets followed


def _page(target_id: str, url: str, opener: str = "") -> dict:
    return {"targetId": target_id, "type": "page", "url": url, "title": target_id,
            "openerId": opener}


class _TargetList:
    """The CDP calls `follow_new_tab` makes, and no others that matter.

    `document.readyState` answers "complete" so the wait after a switch returns
    at once; the helper version answers the current one so the page is not
    asked to install it. Neither is what the test is about.
    """

    def __init__(self, infos: list[dict]) -> None:
        self.infos = infos
        self.events: list[dict] = []
        self.closed: list[str] = []

    def call(self, method: str, timeout: float | None = None, session_id: str | None = None,
             **params: object) -> dict:
        if method == "Target.createTarget":
            return {"targetId": "messenger"}
        if method == "Target.attachToTarget":
            return {"sessionId": "s-" + str(params["targetId"])}
        if method == "Target.getTargets":
            return {"targetInfos": self.infos}
        if method == "Target.closeTarget":
            self.closed.append(str(params.get("targetId")))
        return {}

    def evaluate(self, expression: str, session_id: str | None = None, **kwargs: object):
        if expression == "document.readyState":
            return "complete"
        if "__jevMcp" in expression:
            return browser.HELPER_VERSION
        return None


def _driving(infos: list[dict]) -> tuple[browser.Session, _TargetList]:
    driver = _TargetList(infos)
    session = browser.Session("test", Config(), driver)
    session.start()
    return session, driver


def _tabs_of(session: browser.Session) -> dict[str, dict]:
    return {tab["target_id"]: tab for tab in session._refresh_tabs()}


def test_opener_id_is_what_says_which_tab_this_page_opened():
    """The latest tab this page opened is the one to drive; the rest are not.

    `openerId` is the only thing that distinguishes them. Taking the last new
    tab regardless would follow a tab the user opened a moment later, and
    taking the first would follow a calendar the previous click already
    opened instead of the one this click just did. The page we leave is not
    closed: it is still the user's.
    """
    session, driver = _driving([
        _page("messenger", "https://feishu.test/next/messenger/"),
        _page("user-tab", "https://github.com/pulls"),
        _page("cal-1", "https://feishu.test/calendar/week", opener="messenger"),
        _page("cal-2", "https://security.feishu.cn/link/safety", opener="messenger"),
        _page("foreign", "https://feishu.test/calendar/week", opener="not-us"),
    ])
    tabs = _tabs_of(session)
    assert tabs["cal-2"]["opener"] == "messenger"
    assert tabs["user-tab"]["opener"] == ""
    session._no_change_streak = browser.NO_CHANGE_LIMIT

    # `foreign` is last on purpose: a follower that ignores the opener takes it.
    followed = session.follow_new_tab(dataclasses.replace(
        _observation(), new_tabs=[tabs["cal-1"], tabs["user-tab"], tabs["cal-2"], tabs["foreign"]]
    ))

    assert followed is not None
    assert followed["target_id"] == "cal-2"
    assert session.target_id == "cal-2"
    assert session._no_change_streak == 0
    assert driver.closed == []


def test_a_followed_tab_is_not_handed_over_while_it_is_blank():
    """`window.open` reports the tab before the address exists.

    The blank document loads instantly, so a follower that returns on the load
    event hands the model `about:blank` and the goal answers BLOCKED one
    navigation away from the page the click was for.
    """
    session, driver = _driving([
        _page("messenger", "https://feishu.test/next/messenger/"),
        _page("cal", "about:blank", opener="messenger"),
    ])
    session.cfg = dataclasses.replace(session.cfg, settle_timeout=1.0, settle_poll_ms=20)
    reads = {"n": 0}
    original = driver.call

    def call(method, timeout=None, session_id=None, **params):
        if method == "Target.getTargets":
            reads["n"] += 1
            if reads["n"] >= 3:
                driver.infos[1]["url"] = "https://feishu.test/calendar/week"
        return original(method, timeout=timeout, session_id=session_id, **params)

    driver.call = call
    tabs = _tabs_of(session)

    followed = session.follow_new_tab(dataclasses.replace(
        _observation(), new_tabs=[tabs["cal"]]
    ))

    assert followed is not None
    assert followed["url"] == "https://feishu.test/calendar/week"
    assert reads["n"] >= 3


def test_a_tab_this_page_did_not_open_is_not_followed():
    """No matching opener means there is nothing to follow, not a tab to guess at."""
    session, _driver = _driving([
        _page("messenger", "https://feishu.test/next/messenger/"),
        _page("user-tab", "https://github.com/pulls"),
    ])
    tabs = _tabs_of(session)

    assert session.follow_new_tab(dataclasses.replace(_observation(), new_tabs=[tabs["user-tab"]])) is None
    assert session.target_id == "messenger"


def test_an_occluded_click_is_reread_and_the_goal_continues(monkeypatch):
    """A cover is the page changing, not the goal ending.

    The click is refused because a dialog landed on top of the target. The next
    read is that dialog, and the goal acts on it instead of stopping.
    """
    dialog = dataclasses.replace(
        _observation(),
        text="向参与者发送日程邀请？",
        elements=[Element(ref="e2", role="button", name="发送")],
    )
    session = _FakeSession([
        {"ok": False, "error": "occluded", "page_changed": False, "then_page": dialog},
        _ok(),
    ])
    sent = {"done": False}

    def choose(_cfg, observation, _goal, _history):
        if sent["done"]:
            return {"operation": "DONE", "ref": None, "confidence": 0.9}
        if any(element.name == "发送" for element in observation.elements):
            sent["done"] = True
            return {"operation": "CLICK", "ref": "e2", "target": "发送", "confidence": 0.9}
        return {"operation": "CLICK", "ref": "e1", "target": "时间", "confidence": 0.9}

    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", choose)
    monkeypatch.setattr(server, "_session", lambda _name: session)

    out = server.browser_goal("send the invite", max_steps=6, verbose=True)

    assert "re-observing (occluded)" in out, out
    assert "status: done" in out, out
    assert [ops[0]["ref"] for ops in session.acts] == ["e1", "e2"]


def test_the_same_ref_occluded_twice_ends_the_goal(monkeypatch):
    """One re-read per ref. A second miss is a real failure, not another wait."""
    session = _FakeSession([
        {"ok": False, "error": "occluded", "page_changed": False},
        {"ok": False, "error": "occluded", "page_changed": False},
    ])

    def choose(_cfg, _observation, _goal, _history):
        return {"operation": "CLICK", "ref": "e1", "target": "时间", "confidence": 0.9}

    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", choose)
    monkeypatch.setattr(server, "_session", lambda _name: session)

    out = server.browser_goal("send the invite", max_steps=6, verbose=True)

    assert out.count("re-observing (occluded)") == 1, out
    assert "failed:occluded" in out, out
    assert len(session.acts) == 2


def test_the_trace_marks_whether_the_page_changed(monkeypatch):
    """A repeated click that did nothing has to be visible without inferring it."""
    session = _FakeSession([_ok(), _still()])
    decisions = [
        {"operation": "CLICK", "ref": "e1", "confidence": 0.9},
        {"operation": "CLICK", "ref": "e1", "confidence": 0.9},
        {"operation": "DONE", "confidence": 0.9},
    ]

    out, _seen = _drive(monkeypatch, session, decisions)

    assert "(0ms model / 1ms browser) Δ" in out, out
    assert "(0ms model / 1ms browser) =" in out, out


def test_a_picker_click_carries_its_kind(monkeypatch):
    """The click wait depends on kind, which the decision does not repeat."""
    session = _FakeSession([_ok()])
    session.page = dataclasses.replace(_observation(), elements=[
        Element(ref="e1", role="textbox", name="开始 时间", editable=True, kind="picker", value="14:00"),
    ])

    def choose(_cfg, _observation, _goal, _history):
        if not session.acts:
            return {"operation": "CLICK", "ref": "e1", "confidence": 0.9}
        return {"operation": "DONE", "confidence": 0.9}

    monkeypatch.setattr(server.policy, "available", lambda _cfg: True)
    monkeypatch.setattr(server.policy, "choose", choose)
    monkeypatch.setattr(server, "_session", lambda _name: session)

    server.browser_goal("set the clock", max_steps=4, verbose=True)

    assert session.acts[0][0]["kind"] == "picker"
