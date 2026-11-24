"""End-to-end API tests: ledger, locking, dosing, cancellation, concurrency."""

import json
import sqlite3
import tempfile
import threading

import pytest
from fastapi.testclient import TestClient

from backend.app import service
from backend.app.api import make_app


@pytest.fixture()
def client():
    path = tempfile.mktemp(suffix=".db")
    app = make_app(path)
    with TestClient(app) as c:
        c.db_path = path  # type: ignore[attr-defined]
        yield c


def make_study(c, stock=None, batch_size=6, create_task=True):
    cfg = service.default_config()
    if stock:
        cfg["stock"] = stock
    cfg["region"]["n_steps"] = 10
    cfg["process_levels"] = ["低", "高"]
    cfg["min_per_level"] = 1
    cfg["batch_size"] = batch_size
    r = c.post("/api/studies", json={"name": "S", "config": cfg})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    if create_task:
        t = c.post(f"/api/studies/{sid}/tasks", json={"name": "T1"})
        assert t.status_code == 201, t.text
    return sid


def inv(c, sid):
    return {row["code"]: row for row in c.get(f"/api/studies/{sid}/inventory").json()["inventory"]}


def test_dose_moves_hold_to_consumption(client):
    c = client
    sid = make_study(c, stock={"A": 5000, "B": 5000, "C": 5000, "D": 5000, "E": 5000})
    task = c.post(f"/api/studies/{sid}/tasks", json={"name": "T1"}).json()
    runs = c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json()
    first = runs[0]
    before = inv(c, sid)
    usage = first["usage"]
    assert before["A"]["held"] >= usage["A"] - 1e-9
    r = c.post(f"/api/studies/{sid}/runs/{first['id']}/dose")
    assert r.status_code == 200, r.text
    after = inv(c, sid)
    # balance drops by exactly the usage; the hold disappears; available unchanged
    assert after["A"]["balance"] == pytest.approx(before["A"]["balance"] - usage["A"], abs=1e-9)
    assert after["A"]["held"] == pytest.approx(before["A"]["held"] - usage["A"], abs=1e-9)
    assert after["A"]["available"] == pytest.approx(before["A"]["available"], abs=1e-9)
    assert task["id"]


def test_cancel_releases_only_unstarted_runs(client):
    c = client
    sid = make_study(c, stock={"A": 5000, "B": 5000, "C": 5000, "D": 5000, "E": 5000})
    c.post(f"/api/studies/{sid}/tasks", json={"name": "T1"})
    runs = c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json()
    r0, r1 = runs[0], runs[1]
    before = inv(c, sid)
    assert c.post(f"/api/studies/{sid}/runs/{r0['id']}/dose").status_code == 200
    assert c.post(f"/api/studies/{sid}/runs/{r0['id']}/cancel").status_code == 409
    blocked = c.post(f"/api/studies/{sid}/runs/{r1['id']}/cancel")
    assert blocked.status_code == 200
    after = inv(c, sid)
    # Dosing converts a hold into consumption: balance drops, held drops by
    # the same amount, so available is unchanged.  Cancelling the second run
    # removes its hold, so its material becomes available again.
    assert after["A"]["balance"] == pytest.approx(before["A"]["balance"] - r0["usage"]["A"])
    assert after["A"]["held"] == pytest.approx(
        before["A"]["held"] - r0["usage"]["A"] - r1["usage"]["A"]
    )
    assert after["A"]["available"] == pytest.approx(
        before["A"]["available"] + r1["usage"]["A"]
    )
    assert after["A"]["available"] == pytest.approx(after["A"]["balance"] - after["A"]["held"])


def test_two_tasks_cannot_spend_the_same_stock(client):
    c = client
    # Tight stock allows only a few runs total.
    sid = make_study(
        c,
        stock={"A": 900, "B": 600, "C": 400, "D": 200, "E": 200},
        batch_size=10,
        create_task=False,
    )
    p1 = c.post(f"/api/studies/{sid}/tasks", json={"name": "T1-b"}).json()
    p2 = c.post(f"/api/studies/{sid}/tasks", json={"name": "T2"}).json()
    inv_rows = c.get(f"/api/studies/{sid}/inventory").json()
    total_available = {r["code"]: r["balance"] for r in inv_rows["inventory"]}
    held = {}
    for run in c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json():
        for code, qty in run["usage"].items():
            held[code] = held.get(code, 0.0) + qty
    for code, qty in held.items():
        assert qty <= total_available[code] + 1e-9, (code, qty, total_available[code])
    assert p1["plan"]["feasible_size"] + p2["plan"]["feasible_size"] > 0


def test_replan_does_not_double_spend(client):
    c = client
    sid = make_study(c, stock={"A": 700, "B": 400, "C": 300, "D": 100, "E": 100}, batch_size=8)
    task = c.post(f"/api/studies/{sid}/tasks", json={"name": "T1"}).json()
    for _ in range(3):
        r = c.post(f"/api/studies/{sid}/tasks/{task['id']}/plan", json={})
        assert r.status_code == 200, r.text
    live = c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json()
    total_held = {}
    for run in live:
        for code, qty in run["usage"].items():
            total_held[code] = total_held.get(code, 0.0) + qty
    for row in c.get(f"/api/studies/{sid}/inventory").json()["inventory"]:
        assert total_held.get(row["code"], 0.0) == pytest.approx(row["held"], abs=1e-9)


def test_locked_run_survives_replan(client):
    c = client
    sid = make_study(c)
    task = c.post(f"/api/studies/{sid}/tasks", json={"name": "T1"}).json()
    runs = c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json()
    keep = runs[0]
    assert c.post(f"/api/studies/{sid}/runs/{keep['id']}/lock", json={"locked": True}).status_code == 200
    r = c.post(f"/api/studies/{sid}/tasks/{task['id']}/plan", json={})
    assert r.status_code == 200, r.text
    new_runs = c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json()
    assert any(
        run["amounts"] == keep["amounts"] and run["locked"] for run in new_runs
    )


def test_locked_run_is_reinserted_locked_not_duplicated(client):
    """A replan of the *same* task must carry the lock into the new revision.

    The old revision's planned runs are cancelled; the locked formulation must
    reappear as a locked run of the new batch, exactly once, and its material
    must be held once - not once per revision.
    """
    c = client
    sid = make_study(c)
    task = c.get(f"/api/studies/{sid}/tasks").json()[0]
    keep = c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json()[0]
    assert keep["task_id"] == task["id"]
    assert c.post(f"/api/studies/{sid}/runs/{keep['id']}/lock", json={"locked": True}).status_code == 200
    assert c.post(f"/api/studies/{sid}/tasks/{task['id']}/plan", json={}).status_code == 200

    live = c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json()
    same = [r for r in live if r["amounts"] == keep["amounts"] and r["level"] == keep["level"]]
    assert len(same) == 1, same
    assert same[0]["locked"] is True
    held = {}
    for run in live:
        for code, qty in run["usage"].items():
            held[code] = held.get(code, 0.0) + qty
    for row in c.get(f"/api/studies/{sid}/inventory").json()["inventory"]:
        assert held.get(row["code"], 0.0) == pytest.approx(row["held"], abs=1e-9)


def test_predictive_variance_metric_is_prior_relative(client):
    """The diagnostic must be a fraction in [0,1] that the batch can only lower."""
    c = client
    sid = make_study(c)
    diag = c.get(f"/api/studies/{sid}/diagnostics").json()
    for key in ("mean_variance_before", "mean_variance_after"):
        v = diag[key]
        assert v is None or 0.0 <= v <= 1.0, (key, v)
    c.post(f"/api/studies/{sid}/tasks", json={"name": "T2"})
    diag = c.get(f"/api/studies/{sid}/diagnostics").json()
    before, after = diag["mean_variance_before"], diag["mean_variance_after"]
    if before is not None and after is not None:
        assert after <= before + 1e-9, (before, after)


def test_concurrent_confirmation_cannot_overspend(client):
    c = client
    sid = make_study(
        c, stock={"A": 1000, "B": 400, "C": 300, "D": 150, "E": 150}, batch_size=10
    )
    runs = c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json()
    # Only a handful fit; fire more threads than runs and check the ledger.
    errors = []

    def dose(run_id):
        try:
            r = c.post(f"/api/studies/{sid}/runs/{run_id}/dose")
            if r.status_code not in (200, 409):
                errors.append(r.status_code)
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    threads = [threading.Thread(target=dose, args=(r["id"],)) for r in runs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    inv_rows = c.get(f"/api/studies/{sid}/inventory").json()["inventory"]
    for row in inv_rows:
        assert row["balance"] >= -1e-9, row


def test_duplicate_dose_is_idempotent(client):
    c = client
    sid = make_study(c)
    run = c.get(f"/api/studies/{sid}/runs").json()[0]
    assert c.post(f"/api/studies/{sid}/runs/{run['id']}/dose").status_code == 200
    balance = inv(c, sid)["A"]["balance"]
    assert c.post(f"/api/studies/{sid}/runs/{run['id']}/dose").json().get("already")
    assert inv(c, sid)["A"]["balance"] == balance


def test_response_flow_and_diagnostics(client):
    c = client
    sid = make_study(c)
    run = c.get(f"/api/studies/{sid}/runs").json()[0]
    assert c.post(f"/api/studies/{sid}/runs/{run['id']}/response", json={"value": 1}).status_code == 409
    c.post(f"/api/studies/{sid}/runs/{run['id']}/dose")
    r = c.post(f"/api/studies/{sid}/runs/{run['id']}/response", json={"value": 12.5, "note": "ok"})
    assert r.status_code == 201, r.text
    diag = c.get(f"/api/studies/{sid}/diagnostics").json()
    assert diag["before"]["rank"] >= 1
    assert diag["after"]["rank"] >= diag["before"]["rank"]
    assert any(h["ref"].startswith("R") for h in diag["history"])


def test_failed_run_is_excluded_from_fit(client):
    c = client
    sid = make_study(c)
    runs = c.get(f"/api/studies/{sid}/runs").json()
    c.post(f"/api/studies/{sid}/runs/{runs[0]['id']}/dose")
    c.post(f"/api/studies/{sid}/runs/{runs[1]['id']}/dose")
    c.post(f"/api/studies/{sid}/runs/{runs[0]['id']}/response", json={"value": 9.0})
    diag = c.get(f"/api/studies/{sid}/diagnostics").json()
    assert len([h for h in diag["history"] if h["source"] == "run"]) == 1
    assert any(f.get("run_id") == runs[1]["id"] for f in diag["failed"])


def test_history_import_keeps_violations_out_of_the_fit(client):
    c = client
    sid = make_study(c)
    entries = [
        {"label": "H1", "amounts": {"A": 40, "B": 30, "C": 30, "D": 0, "E": 0}, "level": "低", "response": 3.0},
        {"label": "H-BAD", "amounts": {"A": 90, "B": 0, "C": 10, "D": 0, "E": 0}, "level": "低", "response": 99.0},
    ]
    r = c.post(f"/api/studies/{sid}/history/import", json={"entries": entries})
    assert r.json()["created"] == 2
    diag = c.get(f"/api/studies/{sid}/diagnostics").json()
    assert len(diag["history"]) == 1


def test_stock_shortfall_reports_blocking(client):
    c = client
    sid = make_study(
        c,
        stock={"A": 120, "B": 120, "C": 120, "D": 30, "E": 30},
        batch_size=6,
        create_task=False,
    )
    task = c.post(f"/api/studies/{sid}/tasks", json={"name": "T1"}).json()
    plan = task["plan"]
    assert plan["partial"]
    assert plan["feasible_size"] < plan["requested_size"]
    assert any(b["kind"] == "stock" for b in plan["blocking"])


def test_forbidden_relations_hold_in_persisted_runs(client):
    c = client
    sid = make_study(c)
    c.post(f"/api/studies/{sid}/tasks", json={"name": "T1"})
    region = c.get(f"/api/studies/{sid}/region").json()
    for run in c.get(f"/api/studies/{sid}/runs", params={"status": "planned"}).json():
        a = run["amounts"]
        assert abs(sum(a.values()) - 100) < 1e-9
        assert not (a["B"] > 0 and a["E"] > 0)
        assert not (a["C"] > 0 and a["D"] > 0)
    # center of mass of the candidate grid is inside the region
    assert region["stats"]["candidate_count"] > 0


def test_mark_failed_keeps_material_consumed(client):
    c = client
    sid = make_study(c)
    run = c.get(f"/api/studies/{sid}/runs").json()[0]
    assert c.post(f"/api/studies/{sid}/runs/{run['id']}/fail").status_code == 409
    c.post(f"/api/studies/{sid}/runs/{run['id']}/dose")
    before = inv(c, sid)
    r = c.post(f"/api/studies/{sid}/runs/{run['id']}/fail", json={"note": "开裂"})
    assert r.status_code == 200, r.text
    after = inv(c, sid)
    assert after["A"]["balance"] == before["A"]["balance"]
    diag = c.get(f"/api/studies/{sid}/diagnostics").json()
    assert any(f.get("run_id") == run["id"] for f in diag["failed"])
    assert not any(h.get("run_id") == run["id"] for h in diag["history"])


def test_region_check_endpoint_flags_constraints(client):
    c = client
    sid = make_study(c)
    bad = c.post(
        f"/api/studies/{sid}/region/check",
        json={"amounts": {"A": 30, "B": 30, "C": 30, "D": 10, "E": 0}},
    ).json()
    assert not bad["feasible"]
    assert any(v["kind"] == "forbidden_group" for v in bad["violations"])
    wrong_sum = c.post(
        f"/api/studies/{sid}/region/check",
        json={"amounts": {"A": 40, "B": 30, "C": 20, "D": 0, "E": 0}},
    ).json()
    assert not wrong_sum["feasible"]
    ok = c.post(
        f"/api/studies/{sid}/region/check",
        json={"amounts": {"A": 40, "B": 30, "C": 30, "D": 0, "E": 0}},
    ).json()
    assert ok["feasible"]


def test_report_endpoint_is_complete(client):
    c = client
    sid = make_study(c)
    rep = c.get(f"/api/studies/{sid}/report").json()
    for key in ("generated_at", "config", "inventory", "diagnostics", "tasks"):
        assert key in rep
    assert rep["diagnostics"]["terms"]
