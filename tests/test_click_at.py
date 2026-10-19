"""`browser_click_at` turns a point on a screenshot into one click.

The caller reads the picture and names a fraction of it. The server does not
look at the image, so the only things it can get wrong on its own are clicking
a point the caller did not name, and clicking through a confirmation rule.
"""

from jev_ultrafast_mcp import server as S


class _Tab:
    def __init__(self, size=(1600, 1000)):
        self.size = size
        self.points: list[tuple[float, float]] = []
        self.observed = 0

    def _viewport_size(self):
        return self.size

    def click_at(self, x, y):
        self.points.append((x, y))

    def observe(self):
        self.observed += 1
        return "observation"


def _install(monkeypatch, tab: _Tab) -> None:
    monkeypatch.setattr(S, "_session", lambda _name: tab)
    monkeypatch.setattr(S, "_view", lambda _observation, **_kwargs: "element table")


def test_a_fraction_of_the_viewport_is_where_the_click_lands(monkeypatch):
    tab = _Tab()
    _install(monkeypatch, tab)

    answer = S.browser_click_at(0.5, 0.25)

    assert tab.points == [(800, 250)], tab.points
    assert tab.observed == 1
    assert answer.startswith("clicked at 800,250"), answer
    assert "element table" in answer


def test_a_point_outside_the_picture_is_refused_before_any_click(monkeypatch):
    tab = _Tab()
    _install(monkeypatch, tab)

    answer = S.browser_click_at(1.2, 0.5)

    assert answer.startswith("invalid_request:"), answer
    assert tab.points == []
    assert tab.observed == 0


def test_a_confirmation_label_is_not_clicked_until_the_caller_confirms(monkeypatch):
    tab = _Tab()
    _install(monkeypatch, tab)

    refused = S.browser_click_at(0.5, 0.5, label="Delete account")
    assert refused.startswith("needs_confirmation:"), refused
    assert "Nothing was clicked." in refused
    assert tab.points == []

    accepted = S.browser_click_at(0.5, 0.5, label="Delete account", confirm=True)
    assert tab.points == [(800, 500)], tab.points
    assert accepted.startswith("clicked at 800,500"), accepted
