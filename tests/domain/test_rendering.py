"""Tests for pure rendering domain module (no DB)."""

from __future__ import annotations

import pytest

from app.domain.rendering import MissingMergeTag, render


def test_render_substitutes_tags():
    assert render("Hello {{name}}", {"name": "Shawn"}) == "Hello Shawn"


def test_render_dotted():
    assert (
        render("State: {{attrs.state}}", {"attrs": {"state": "TX"}})
        == "State: TX"
    )


def test_render_missing_raises():
    with pytest.raises(MissingMergeTag):
        render("Hi {{missing}}", {"name": "Shawn"})


def test_render_whitespace_tolerant():
    assert render("Hi {{ name }}", {"name": "Shawn"}) == "Hi Shawn"
