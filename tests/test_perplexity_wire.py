"""The request body on Perplexity's Decisions API, and the answers that come back.

Perplexity takes the same `{model, state, questions}` envelope as TypeSafe but answers a
non-string instruction or option description with a 400. The questions are still built once,
in TypeSafe's shape; these tests pin that the perplexity route flattens exactly those two
fields to text, keeps every option key (the refs the answer is checked against), and leaves
the TypeSafe body alone.

No network: the provider is faked at `policy._post`, and a fake `CLIENT` stands in for
the HTTP layer where the status handling itself is under test.
"""

from __future__ import annotations

import dataclasses

import pytest

from jev_ultrafast_mcp import policy
from jev_ultrafast_mcp.config import Config
from jev_ultrafast_mcp.observe import Element, Observation


def _observation() -> Observation:
    return Observation(
        url="https://example.test/",
        title="Example",
        text="Example page",
        elements=[
            Element(ref="e1", role="searchbox", name="Search", editable=True, value="shoes"),
            Element(ref="e2", role="button", name="Go"),
            Element(ref="e3", role="link", name="Help"),
        ],
        digest="d",
        text_digest="t",
        page_key="k",
        scroll={"y": 0},
        reachable=3,
        omitted=0,
        overlays=[],
        cross_frames=0,
        cross_frame_srcs=[],
    )


def _cfg(provider: str) -> Config:
    model = "pplx-decider-latest" if provider == "perplexity" else "jev-latest"
    return dataclasses.replace(Config.from_env(), provider=provider, typesafe_key="test-key",
                               typesafe_model=model)


def _capturing(sent: list, choose_ref=lambda ids: ids[0]):
    """A provider that records each body and answers every question validly."""
    def post(url, key, body):
        sent.append(body)
        answers = {}
        for name, question in body["questions"].items():
            ids = sorted(question["criteria"])
            choice = "CLICK" if name == "operation" else choose_ref(ids)
            answers[name] = {"choice": choice, "confidence": 0.9,
                             "probabilities": {item: (1.0 if item == choice else 0.0) for item in ids}}
        return {"answers": answers, "model": body["model"]}
    return post


def test_the_perplexity_body_carries_only_strings_where_the_api_requires_them(monkeypatch):
    sent: list = []
    monkeypatch.setattr(policy, "_post", _capturing(sent))

    policy.choose(_cfg("perplexity"), _observation(), "search for shoes", [])

    body = sent[0]
    assert body["model"] == "pplx-decider-latest"
    for name, question in body["questions"].items():
        assert isinstance(question["instructions"], str), name
        for key, option in question["criteria"].items():
            assert option is None or isinstance(option, str), (name, key, option)
    target = body["questions"]["click_target"]
    assert set(target["criteria"]) <= {"e1", "e2", "e3"}, "option keys are still the refs"
    assert "search for shoes" in target["instructions"], "the goal survives the flattening"


def test_an_option_description_leads_with_the_element_and_drops_empty_fields():
    flat = policy._option_text({"element": "btn Go", "current_value": None, "context": "header"})

    assert flat == "btn Go | context: header"


def test_the_typesafe_body_is_sent_unchanged(monkeypatch):
    sent: list = []
    monkeypatch.setattr(policy, "_post", _capturing(sent))

    policy.choose(_cfg("typesafe"), _observation(), "search for shoes", [])

    question = sent[0]["questions"]["operation"]
    assert isinstance(question["instructions"], dict), "structured instructions stay structured"
    target = sent[0]["questions"]["click_target"]
    assert all(isinstance(option, dict) for option in target["criteria"].values())


def test_a_perplexity_answer_validates_and_selects_the_element(monkeypatch):
    sent: list = []
    monkeypatch.setattr(policy, "_post", _capturing(sent, choose_ref=lambda ids: ids[-1]))

    decision = policy.choose(_cfg("perplexity"), _observation(), "open help", [])

    offered = sorted(sent[0]["questions"]["click_target"]["criteria"])
    assert decision["operation"] == "CLICK"
    assert decision["ref"] == offered[-1]
    assert decision["model"] == "pplx-decider-latest"


class _Response:
    def __init__(self, status: int, payload=None, headers=None):
        self.status_code = status
        self.is_error = status >= 400
        self.headers = headers or {}
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not JSON")
        return self._payload


class _Client:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = 0

    def post(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_a_400_reports_the_provider_s_own_message(monkeypatch):
    error = {"error": {"message": "questions.operation.criteria.e1: expected string",
                       "type": "invalid_request_error"}}
    monkeypatch.setattr(policy, "CLIENT", _Client(_Response(400, error)))

    with pytest.raises(policy.TurboUnavailable) as caught:
        policy._post("https://api.perplexity.ai/v1/decisions", "test-key", {})

    message = str(caught.value)
    assert "HTTP 400" in message
    assert "expected string" in message
    assert "No action executed" in message


def test_a_429_waits_as_long_as_retry_after_asks(monkeypatch):
    slept: list = []
    monkeypatch.setattr(policy.time, "sleep", slept.append)
    client = _Client(_Response(429, {}, {"retry-after": "2"}), _Response(200, {"answers": {}}))
    monkeypatch.setattr(policy, "CLIENT", client)

    result = policy._post("https://api.perplexity.ai/v1/decisions", "test-key", {})

    assert result == {"answers": {}}
    assert slept == [2.0]
    assert client.calls == 2


def test_a_long_retry_after_is_capped(monkeypatch):
    slept: list = []
    monkeypatch.setattr(policy.time, "sleep", slept.append)
    client = _Client(_Response(429, {}, {"retry-after": "120"}), _Response(200, {}))
    monkeypatch.setattr(policy, "CLIENT", client)

    policy._post("https://api.perplexity.ai/v1/decisions", "test-key", {})

    assert slept == [policy.MAX_RETRY_AFTER]


def test_a_402_off_openrouter_does_not_blame_openrouter(monkeypatch):
    monkeypatch.setattr(policy, "CLIENT", _Client(_Response(402, {})))

    with pytest.raises(policy.TurboUnavailable) as caught:
        policy._post("https://api.perplexity.ai/v1/decisions", "test-key", {})

    assert "OpenRouter" not in str(caught.value)
