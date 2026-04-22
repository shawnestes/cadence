"""Tests for /api/workflows."""

from __future__ import annotations

import uuid


def _valid_definition(name: str = "test-wf") -> dict:
    return {
        "version": 1,
        "name": name,
        "vertical": "auto",
        "entry": "entry",
        "nodes": {
            "entry": {"type": "entry", "next": "exit_done"},
            "exit_done": {"type": "exit", "reason": "completed"},
        },
    }


def _cycle_definition() -> dict:
    return {
        "version": 1,
        "name": "test-cycle",
        "vertical": "auto",
        "entry": "a",
        "nodes": {
            "a": {"type": "entry", "next": "b"},
            "b": {"type": "wait", "duration": "P1D", "next": "a"},
        },
    }


def _workflow_body(**overrides) -> dict:
    body = {
        "name": f"test-wf-{uuid.uuid4()}",
        "tenant": "otto",
        "vertical": "auto",
        "definition": _valid_definition(),
        "entry_segment": None,
    }
    body.update(overrides)
    return body


async def test_create_workflow_returns_201(client, auth_headers):
    resp = await client.post("/api/workflows", json=_workflow_body(), headers=auth_headers)
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["id"]
    assert data["status"] == "draft"
    assert data["version"] == 1


async def test_create_invalid_definition_returns_400(client, auth_headers):
    body = _workflow_body(definition=_cycle_definition())
    resp = await client.post("/api/workflows", json=body, headers=auth_headers)
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    text = str(detail).lower()
    assert "cycle" in text


async def test_patch_invalid_definition_returns_400(client, auth_headers):
    create = await client.post(
        "/api/workflows", json=_workflow_body(), headers=auth_headers
    )
    assert create.status_code == 201
    wid = create.json()["id"]
    resp = await client.patch(
        f"/api/workflows/{wid}",
        json={"definition": _cycle_definition()},
        headers=auth_headers,
    )
    assert resp.status_code == 400
    assert "cycle" in str(resp.json()["detail"]).lower()


async def test_patch_definition_bumps_version(client, auth_headers):
    create = await client.post(
        "/api/workflows", json=_workflow_body(), headers=auth_headers
    )
    wid = create.json()["id"]
    assert create.json()["version"] == 1

    new_def = _valid_definition(name="test-wf-v2")
    new_def["nodes"]["exit_done"]["reason"] = "renamed"

    resp = await client.patch(
        f"/api/workflows/{wid}",
        json={"definition": new_def},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["version"] == 2


async def test_list_workflows_filters_by_status(client, auth_headers):
    a = await client.post("/api/workflows", json=_workflow_body(), headers=auth_headers)
    b = await client.post("/api/workflows", json=_workflow_body(), headers=auth_headers)
    assert a.status_code == 201 and b.status_code == 201

    act = await client.post(
        f"/api/workflows/{a.json()['id']}/activate", headers=auth_headers
    )
    assert act.status_code == 200

    resp = await client.get(
        "/api/workflows", params={"status": "active"}, headers=auth_headers
    )
    assert resp.status_code == 200
    ids = {w["id"] for w in resp.json()["workflows"]}
    assert a.json()["id"] in ids
    assert b.json()["id"] not in ids


async def test_activate_pause_archive(client, auth_headers):
    create = await client.post(
        "/api/workflows", json=_workflow_body(), headers=auth_headers
    )
    wid = create.json()["id"]

    act = await client.post(f"/api/workflows/{wid}/activate", headers=auth_headers)
    assert act.status_code == 200
    assert act.json()["status"] == "active"

    pause = await client.post(f"/api/workflows/{wid}/pause", headers=auth_headers)
    assert pause.status_code == 200
    assert pause.json()["status"] == "paused"

    arch = await client.post(f"/api/workflows/{wid}/archive", headers=auth_headers)
    assert arch.status_code == 200
    assert arch.json()["status"] == "archived"


async def test_workflows_require_auth(client):
    resp = await client.post("/api/workflows", json=_workflow_body())
    assert resp.status_code == 401
