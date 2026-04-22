"""Pure tests for WorkflowDefinition + validate_graph + parse_iso_duration."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.workflow_defs import WorkflowDefinition, parse_iso_duration


def _wf(entry: str, nodes: dict) -> WorkflowDefinition:
    return WorkflowDefinition.model_validate(
        {
            "version": 1,
            "name": "test-wf",
            "vertical": "auto",
            "entry": entry,
            "nodes": nodes,
        }
    )


def test_valid_linear_graph():
    wf = _wf(
        "entry",
        {
            "entry": {"type": "entry", "next": "exit_done"},
            "exit_done": {"type": "exit", "reason": "completed"},
        },
    )
    errors = wf.validate_graph()
    assert errors == []


def test_missing_entry_reports_error():
    wf = _wf(
        "missing",
        {
            "entry": {"type": "entry", "next": "exit_done"},
            "exit_done": {"type": "exit", "reason": "completed"},
        },
    )
    errors = wf.validate_graph()
    assert any("missing" in e for e in errors)


def test_unknown_next_node_reports_error():
    wf = _wf(
        "entry",
        {
            "entry": {"type": "entry", "next": "ghost"},
        },
    )
    errors = wf.validate_graph()
    assert any("ghost" in e for e in errors)


def test_cycle_detection():
    wf = _wf(
        "a",
        {
            "a": {"type": "entry", "next": "b"},
            "b": {"type": "wait", "duration": "P1D", "next": "a"},
        },
    )
    errors = wf.validate_graph()
    assert any("cycle" in e for e in errors)


def test_parse_iso_duration_cases():
    assert parse_iso_duration("P3D") == timedelta(days=3)
    assert parse_iso_duration("PT1H30M") == timedelta(hours=1, minutes=30)
    assert parse_iso_duration("P1W") == timedelta(days=7)
    with pytest.raises(ValueError):
        parse_iso_duration("invalid")


def test_branch_node_nested_and_condition_parses():
    wf = _wf(
        "entry",
        {
            "entry": {"type": "entry", "next": "br"},
            "br": {
                "type": "branch",
                "condition": {
                    "kind": "and",
                    "operands": [
                        {
                            "kind": "event_in_window",
                            "event_type": "open",
                            "since_node": "journey_start",
                            "window": "P1D",
                        },
                        {
                            "kind": "click_url_match",
                            "since_node": "journey_start",
                            "url_contains": "/quote",
                            "window": "P3D",
                        },
                    ],
                },
                "if_true": "exit_yes",
                "if_false": "exit_no",
            },
            "exit_yes": {"type": "exit", "reason": "engaged"},
            "exit_no": {"type": "exit", "reason": "no_engagement"},
        },
    )
    assert wf.validate_graph() == []
    dumped = wf.model_dump()
    round_trip = WorkflowDefinition.model_validate(dumped)
    assert round_trip.nodes["br"].condition.kind == "and"
