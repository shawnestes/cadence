"""Scheduler wiring tests.

These exist because a NameError in the job-registration block crash-looped the
deployed worker while the entire unit suite stayed green: nothing imported or
executed `main()`, so a bad reference in an `add_job` argument was invisible
until systemd restarted the process in a loop. Registration is cheap to
exercise, so exercise it.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SCHEDULER = Path(__file__).resolve().parents[2] / "app" / "workers" / "scheduler.py"

EXPECTED_JOBS = {
    "send_worker",
    "workflow_tick",
    "stats_rollup",
    "postmaster_sync",
}


def test_scheduler_has_no_undefined_settings_reference():
    """`scheduler.main` resolves settings via get_settings() at each use; a bare
    `settings` name is the NameError that crash-looped the worker."""
    tree = ast.parse(SCHEDULER.read_text(encoding="utf-8"))

    assigned = {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    loaded = {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    }

    assert not ({"settings"} & loaded - assigned), (
        "scheduler.py loads a bare `settings` that is never assigned; "
        "use get_settings() inline as the rest of the module does"
    )


def test_every_add_job_argument_resolves(monkeypatch):
    """Build the real scheduler with a stubbed APScheduler, so every argument
    expression in every add_job call is actually evaluated."""
    from app.workers import scheduler as mod

    registered: list[str] = []

    class FakeScheduler:
        def __init__(self, *a, **kw):
            pass

        def add_job(self, func, trigger=None, **kw):
            # Evaluating the call site is the point; record what got through.
            assert callable(func), f"job {kw.get('id')!r} is not callable"
            assert kw.get("id"), "every job needs an explicit id"
            registered.append(kw["id"])

        def get_jobs(self):
            return []

        def start(self):
            pass

        def shutdown(self, wait=True):
            pass

    monkeypatch.setattr(mod, "AsyncIOScheduler", FakeScheduler)
    mod.build_scheduler()

    assert len(registered) == len(set(registered)), (
        f"duplicate job ids: {registered}"
    )
    missing = EXPECTED_JOBS - set(registered)
    assert not missing, f"expected jobs not registered: {missing}"


def test_postmaster_sync_interval_is_positive():
    from app.core.config import get_settings

    assert get_settings().postmaster_sync_interval_seconds > 0
