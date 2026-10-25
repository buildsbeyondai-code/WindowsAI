"""Static accessibility checks on the dashboard page (WCAG 2.1 AA).

The browser suites prove the page works; these prove it stays usable without a
mouse or perfect eyesight: text colours meet 4.5:1 on every panel colour, each
control has an accessible name, and keyboard focus is visible.
"""
import re
from html.parser import HTMLParser
from pathlib import Path

PAGE = Path(__file__).resolve().parents[1] / "src" / "sysperf" / "static" / "index.html"
HTML = PAGE.read_text(encoding="utf-8")


def _tokens() -> dict:
    root = re.search(r":root\s*\{(.*?)\}", HTML, re.S).group(1)
    return dict(re.findall(r"--([\w-]+)\s*:\s*(#[0-9a-fA-F]{6})", root))


def _lum(hex_: str) -> float:
    c = [int(hex_[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_lum(a), _lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_contrast_helper_matches_known_values():
    assert round(contrast("#000000", "#ffffff"), 1) == 21.0
    assert round(contrast("#777777", "#ffffff"), 1) == 4.5


def test_text_colours_meet_aa_on_every_background():
    t = _tokens()
    for fg in ("ink", "muted", "accent", "green", "amber", "red", "yellow"):
        for bg in ("bg", "panel", "panel2"):
            ratio = contrast(t[fg], t[bg])
            assert ratio >= 4.5, f"--{fg} on --{bg} is {ratio:.2f}:1"


class _Controls(HTMLParser):
    def __init__(self):
        super().__init__()
        self.labels_for, self.inputs, self.buttons = set(), [], []
        self._button, self._in_label = None, 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "label":
            self._in_label += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") != "hidden":
            self.inputs.append({**a, "_wrapped": self._in_label > 0})
        if tag == "button":
            self._button = {"attrs": a, "text": ""}

    def handle_data(self, data):
        if self._button is not None:
            self._button["text"] += data

    def handle_endtag(self, tag):
        if tag == "label" and self._in_label:
            self._in_label -= 1
        if tag == "button" and self._button is not None:
            self.buttons.append(self._button)
            self._button = None


def _controls():
    p = _Controls()
    p.feed(HTML)
    return p


def test_every_static_button_has_an_accessible_name():
    p = _controls()
    assert p.buttons
    for b in p.buttons:
        a = b["attrs"]
        assert b["text"].strip() or a.get("aria-label") or a.get("title"), a


def test_every_static_form_field_is_labelled():
    p = _controls()
    for a in p.inputs:
        named = (a["_wrapped"] or a.get("aria-label") or a.get("title")
                 or a.get("id") in p.labels_for)
        assert named, a


def test_keyboard_focus_is_visible_and_motion_can_be_reduced():
    assert re.search(r":focus-visible[^{]*\{[^}]*outline", HTML)
    assert "prefers-reduced-motion" in HTML
    assert '<html lang="en">' in HTML
