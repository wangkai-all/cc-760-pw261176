"""REST API for the mixture experiment designer."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db, service
from .mixture import (
    CandidateCapExceeded,
    RegionError,
    blend_violations,
    enumerate_region,
    region_from_config,
)

class StudyIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    config: dict | None = None



class HistoryIn(BaseModel):
    label: str
    amounts: dict[str, float]
    level: str | None = None
    response: float | None = None
    note: str = ""



class TaskIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    design: dict | None = None



class PlanIn(BaseModel):
    design: dict | None = None

DB_PATH = os.environ.get("FORMULAB_DB", str(db.DB_PATH))
FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend"


def make_app(db_path: str | None = None) -> FastAPI:
    global DB_PATH
    if db_path:
        DB_PATH = db_path
    db.init_db(DB_PATH)
    app = FastAPI(title="FormuLab", version="1.0")

    @app.exception_handler(service.ApiError)
    def _api_error(_: Request, exc: service.ApiError):
        return JSONResponse(status_code=exc.status, content=exc.payload())

    @app.exception_handler(RegionError)
    def _region_error(_: Request, exc: RegionError):
        return JSONResponse(
            status_code=422, content={"error": str(exc), "detail": exc.blocking}
        )

    @app.exception_handler(CandidateCapExceeded)
    def _cap_error(_: Request, exc: CandidateCapExceeded):
        return JSONResponse(
            status_code=422, content={"error": str(exc), "detail": exc.blocking}
        )

    @app.exception_handler(KeyError)
    def _missing(_: Request, exc: KeyError):
        return JSONResponse(status_code=404, content={"error": "资源不存在"})

    # ------------------------------------------------------------------ studies


    @app.get("/api/health")
    def health():
        return {"ok": True, "db": DB_PATH}

    @app.post("/api/demo")
    def create_demo(force: bool = False):
        from .seed import build_demo

        return build_demo(force=force)

    @app.get("/api/studies")
    def list_studies():
        with db.read_conn(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT s.*, (SELECT COUNT(*) FROM tasks t WHERE t.study_id = s.id) AS task_count "
                "FROM studies s ORDER BY s.id"
            ).fetchall()
            return [dict(r) for r in rows]

    @app.post("/api/studies", status_code=201)
    def create_study(body: StudyIn):
        config = service._clean_config(body.config or service.default_config())
        with db.transaction(DB_PATH) as conn:
            cur = conn.execute(
                "INSERT INTO studies (name, config_json, created_at) VALUES (?, ?, ?)",
                (body.name, json.dumps(config, ensure_ascii=False), db.utcnow()),
            )
            study_id = cur.lastrowid
            for code, qty in config["stock"].items():
                conn.execute(
                    "INSERT INTO stock_entries (study_id, component_code, delta, kind, note, created_at) "
                    "VALUES (?, ?, ?, 'receipt', '初始库存', ?)",
                    (study_id, code, float(qty), db.utcnow()),
                )
        return {"id": study_id}

    @app.get("/api/studies/{study_id}")
    def get_study(study_id: int):
        with db.read_conn(DB_PATH) as conn:
            return service.study_snapshot(conn, study_id)

    @app.put("/api/studies/{study_id}/config")
    def put_config(study_id: int, body: dict):
        cleaned = service._clean_config(body)
        with db.transaction(DB_PATH) as conn:
            current = db.load_config(conn, study_id)
            # Receipts are ledger entries; changing stock adds an adjustment
            # entry for the difference instead of rewriting history.
            if "stock" in body:
                for comp in cleaned["components"]:
                    code = comp["code"]
                    now = db.ledger_balance(conn, study_id).get(code, 0.0)
                    target = float(cleaned["stock"].get(code, now))
                    if abs(target - now) > 1e-9:
                        conn.execute(
                            "INSERT INTO stock_entries (study_id, component_code, delta, kind, note, created_at) "
                            "VALUES (?, ?, ?, 'adjustment', '库存调整', ?)",
                            (study_id, code, target - now, db.utcnow()),
                        )
            db.save_config(conn, study_id, cleaned)
            study = db.get_study(conn, study_id)
            _ = current, study
        return {"ok": True}

    @app.get("/api/studies/{study_id}/region")
    def get_region(study_id: int, include_vertices: bool = True, limit: int = 800):
        with db.read_conn(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
        spec = region_from_config(config["components"], config["region"])
        result = enumerate_region(spec, include_vertices=include_vertices)
        pts = result.candidates[:limit]
        return {
            "codes": list(spec.codes),
            "names": list(spec.names),
            "total": spec.total,
            "n_steps": spec.n_steps,
            "lower": list(spec.lower),
            "upper": list(spec.upper),
            "forbidden_pairs": [[spec.codes[i] for i in p] for p in spec.forbidden_pairs],
            "forbidden_groups": [[spec.codes[i] for i in g] for g in spec.forbidden_groups],
            "stats": result.stats,
            "blocking": result.blocking,
            "points": [
                {
                    "amounts": {c: a for c, a in zip(spec.codes, cand.amounts)},
                    "source": cand.source,
                }
                for cand in pts
            ],
            "truncated": len(result.candidates) > len(pts),
        }

    @app.post("/api/studies/{study_id}/region/check")
    def check_region(study_id: int, body: dict):
        with db.read_conn(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
        spec = region_from_config(config["components"], config["region"])
        raw = body.get("amounts", {})
        try:
            amounts = [float(raw[c]) for c in spec.codes]
        except (KeyError, TypeError, ValueError):
            raise service.ApiError(400, "配方必须为每个成分提供数值")
        violations = blend_violations(amounts, spec)
        return {"feasible": not violations, "violations": violations, "sum": sum(amounts)}

    @app.get("/api/studies/{study_id}/inventory")
    def inventory(study_id: int):
        with db.read_conn(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
            inv = db.inventory(conn, study_id, config)
            ledger = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM stock_entries WHERE study_id = ? ORDER BY id DESC LIMIT 50",
                    (study_id,),
                )
            ]
            holds = []
            for r in conn.execute(
                "SELECT r.*, t.name AS task_name FROM runs r JOIN tasks t ON t.id = r.task_id "
                "WHERE r.study_id = ? AND r.status = 'planned' ORDER BY r.id",
                (study_id,),
            ):
                holds.append(
                    {
                        "run_id": r["id"],
                        "task_id": r["task_id"],
                        "task_name": r["task_name"],
                        "usage": json.loads(r["usage_json"]),
                        "level": r["level"],
                        "ordinal": r["ordinal"],
                    }
                )
            return {"inventory": inv, "ledger": ledger, "holds": holds}

    @app.post("/api/studies/{study_id}/inventory/receipt", status_code=201)
    def receipt(study_id: int, body: dict):
        code = str(body.get("component_code", ""))
        try:
            delta = float(body.get("delta"))
        except (TypeError, ValueError):
            raise service.ApiError(400, "delta 必须是数值")
        if delta <= 0:
            raise service.ApiError(400, "入库数量必须为正")
        with db.transaction(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
            codes = {c["code"] for c in config["components"]}
            if code not in codes:
                raise service.ApiError(404, f"未知成分 {code}")
            conn.execute(
                "INSERT INTO stock_entries (study_id, component_code, delta, kind, note, created_at) "
                "VALUES (?, ?, ?, 'receipt', ?, ?)",
                (study_id, code, delta, str(body.get("note", "入库")), db.utcnow()),
            )
            return {"ok": True, "inventory": db.inventory(conn, study_id, config)}

    # ------------------------------------------------------------------ history

    @app.get("/api/studies/{study_id}/history")
    def get_history(study_id: int):
        with db.read_conn(DB_PATH) as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM history WHERE study_id = ? ORDER BY id", (study_id,)
                )
            ]
            responses = db.response_rows(conn, study_id)
            failed = db.failed_runs(conn, study_id)
        for r in rows:
            r["amounts"] = json.loads(r["amounts_json"])
        return {"entries": rows, "responses": responses, "failed": failed}


    @app.post("/api/studies/{study_id}/history", status_code=201)
    def add_history(study_id: int, body: HistoryIn):
        with db.transaction(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
            spec = region_from_config(config["components"], config["region"])
            amounts = service._amounts_for(spec, body.amounts)
            if body.level is not None and body.level not in config["process_levels"]:
                raise service.ApiError(400, f"未知加工档位 {body.level}")
            violations = blend_violations(amounts, spec)
            conn.execute(
                "INSERT INTO history (study_id, label, amounts_json, level, response, note, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    study_id,
                    body.label,
                    json.dumps({c: a for c, a in zip(spec.codes, amounts)}),
                    body.level,
                    body.response,
                    body.note,
                    "completed" if body.response is not None else "failed",
                    db.utcnow(),
                ),
            )
            return {"ok": True, "violations": violations, "recorded": len(amounts)}

    @app.post("/api/studies/{study_id}/history/import")
    def import_history(study_id: int, body: dict):
        entries = body.get("entries")
        if not isinstance(entries, list) or not entries:
            raise service.ApiError(400, "entries 必须是非空数组")
        created = 0
        rejected = []
        with db.transaction(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
            spec = region_from_config(config["components"], config["region"])
            for idx, entry in enumerate(entries):
                try:
                    amounts = service._amounts_for(spec, entry["amounts"])
                    level = entry.get("level")
                    if level is not None and level not in config["process_levels"]:
                        raise ValueError(f"未知档位 {level}")
                except (KeyError, TypeError, ValueError) as exc:
                    rejected.append({"index": idx, "reason": str(exc)})
                    continue
                response = entry.get("response")
                violations = blend_violations(amounts, spec)
                conn.execute(
                    "INSERT INTO history (study_id, label, amounts_json, level, response, note, status, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        study_id,
                        str(entry.get("label", f"H{idx + 1}")),
                        json.dumps({c: a for c, a in zip(spec.codes, amounts)}),
                        level,
                        None if response is None else float(response),
                        str(entry.get("note", "")),
                        "completed" if response is not None else "failed",
                        db.utcnow(),
                    ),
                )
                created += 1
                if violations:
                    rejected.append(
                        {"index": idx, "reason": "配方违反区域约束（已记录，不会用于设计）", "violations": violations}
                    )
            return {"created": created, "rejected": rejected}

    @app.delete("/api/studies/{study_id}/history/{entry_id}")
    def delete_history(study_id: int, entry_id: int):
        with db.transaction(DB_PATH) as conn:
            cur = conn.execute(
                "DELETE FROM history WHERE id = ? AND study_id = ?", (entry_id, study_id)
            )
            if cur.rowcount == 0:
                raise service.ApiError(404, "历史记录不存在")
        return {"ok": True}

    # ------------------------------------------------------------------ tasks


    @app.get("/api/studies/{study_id}/tasks")
    def list_tasks(study_id: int):
        with db.read_conn(DB_PATH) as conn:
            db.get_study(conn, study_id)
            rows = []
            for t in conn.execute(
                "SELECT * FROM tasks WHERE study_id = ? ORDER BY id", (study_id,)
            ):
                latest = conn.execute(
                    "SELECT * FROM batches WHERE task_id = ? ORDER BY revision DESC LIMIT 1",
                    (t["id"],),
                ).fetchone()
                batch = None
                if latest is not None:
                    counts = {
                        status: n
                        for status, n in conn.execute(
                            "SELECT status, COUNT(*) FROM runs WHERE batch_id = ? GROUP BY status",
                            (latest["id"],),
                        )
                    }
                    batch = {
                        "id": latest["id"],
                        "revision": latest["revision"],
                        "requested_size": latest["requested_size"],
                        "feasible_size": latest["feasible_size"],
                        "partial": bool(latest["partial"]),
                        "status_counts": counts,
                    }
                rows.append(
                    {**dict(t), "design": json.loads(t["design_json"]), "latest_batch": batch}
                )
            return rows

    @app.post("/api/studies/{study_id}/tasks", status_code=201)
    def create_task(study_id: int, body: TaskIn):
        with db.transaction(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
            design = {
                "model": (body.design or {}).get("model", config["model"]),
                "include_process": (body.design or {}).get(
                    "include_process", config["include_process"]
                ),
                "batch_size": int(
                    (body.design or {}).get("batch_size", config["batch_size"])
                ),
                "min_per_level": int(
                    (body.design or {}).get("min_per_level", config["min_per_level"])
                ),
                "allow_replicates": bool(
                    (body.design or {}).get("allow_replicates", config["allow_replicates"])
                ),
            }
            if design["model"] not in ("linear", "quadratic"):
                raise service.ApiError(400, "model 必须是 linear 或 quadratic")
            if design["batch_size"] < 1:
                raise service.ApiError(400, "批量必须至少为 1")
            cur = conn.execute(
                "INSERT INTO tasks (study_id, name, design_json, status, created_at) "
                "VALUES (?, ?, ?, 'open', ?)",
                (study_id, body.name, json.dumps(design, ensure_ascii=False), db.utcnow()),
            )
            task_id = cur.lastrowid
            planned = service.plan_task(conn, study_id, task_id)
        return {"id": task_id, "plan": planned}

    @app.get("/api/studies/{study_id}/tasks/{task_id}")
    def get_task(study_id: int, task_id: int):
        with db.read_conn(DB_PATH) as conn:
            return service.task_detail(conn, study_id, task_id)


    @app.post("/api/studies/{study_id}/tasks/{task_id}/plan")
    def replan(study_id: int, task_id: int, body: PlanIn | None = None):
        with db.transaction(DB_PATH) as conn:
            result = service.replace_open_revision(
                conn, study_id, task_id, body.design if body else None
            )
        return result

    # ------------------------------------------------------------------ runs

    @app.get("/api/studies/{study_id}/runs")
    def list_runs(study_id: int, status: str | None = None):
        with db.read_conn(DB_PATH) as conn:
            q = (
                "SELECT r.*, t.name AS task_name, resp.value AS response "
                "FROM runs r JOIN tasks t ON t.id = r.task_id "
                "LEFT JOIN responses resp ON resp.run_id = r.id WHERE r.study_id = ?"
            )
            params: list[Any] = [study_id]
            if status:
                q += " AND r.status = ?"
                params.append(status)
            q += " ORDER BY r.id"
            rows = []
            for r in conn.execute(q, params):
                d = service._run_dict(r)
                d["task_name"] = r["task_name"]
                d["response"] = r["response"]
                rows.append(d)
            return rows

    @app.post("/api/studies/{study_id}/runs/{run_id}/lock")
    def lock_run(study_id: int, run_id: int, body: dict):
        locked = bool(body.get("locked", True))
        with db.transaction(DB_PATH) as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ? AND study_id = ?", (run_id, study_id)
            ).fetchone()
            if row is None:
                raise service.ApiError(404, "试验不存在")
            if row["status"] != "planned":
                raise service.ApiError(409, "只有已计划、未投料的试验可以锁定/解锁")
            conn.execute("UPDATE runs SET locked = ? WHERE id = ?", (1 if locked else 0, run_id))
        return {"ok": True, "locked": locked}

    @app.post("/api/studies/{study_id}/runs/{run_id}/dose")
    def dose_run(study_id: int, run_id: int):
        """Confirm dosing: move the hold into a ledger consumption atomically."""
        with db.transaction(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ? AND study_id = ?", (run_id, study_id)
            ).fetchone()
            if row is None:
                raise service.ApiError(404, "试验不存在")
            if row["status"] == "dosed":
                return {"ok": True, "already": True}
            if row["status"] != "planned":
                raise service.ApiError(409, f"试验状态为 {row['status']}，不能重复投料")
            usage = json.loads(row["usage_json"])
            balance = db.ledger_balance(conn, study_id)
            short = [
                {
                    "kind": "stock",
                    "component": code,
                    "required": qty,
                    "balance": balance.get(code, 0.0),
                    "message": f"{code} 账面库存不足",
                }
                for code, qty in usage.items()
                if balance.get(code, 0.0) + 1e-9 < float(qty)
            ]
            if short:
                raise service.ApiError(409, "账面库存不足，投料被拒绝", short)
            now = db.utcnow()
            for code, qty in usage.items():
                conn.execute(
                    "INSERT INTO stock_entries (study_id, component_code, delta, kind, run_id, note, created_at) "
                    "VALUES (?, ?, ?, 'dose', ?, ?, ?)",
                    (study_id, code, -float(qty), run_id, f"投料 R{run_id}", now),
                )
            conn.execute(
                "UPDATE runs SET status = 'dosed', dosed_at = ? WHERE id = ?", (now, run_id)
            )
            return {
                "ok": True,
                "run_id": run_id,
                "inventory": db.inventory(conn, study_id, config),
            }

    @app.post("/api/studies/{study_id}/runs/{run_id}/cancel")
    def cancel_run(study_id: int, run_id: int):
        with db.transaction(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ? AND study_id = ?", (run_id, study_id)
            ).fetchone()
            if row is None:
                raise service.ApiError(404, "试验不存在")
            if row["status"] == "cancelled":
                return {"ok": True, "already": True}
            if row["status"] != "planned":
                raise service.ApiError(
                    409, "已投料的试验不能取消（原料已从台账扣除）"
                )
            conn.execute(
                "UPDATE runs SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
                (db.utcnow(), run_id),
            )
            return {
                "ok": True,
                "released": json.loads(row["usage_json"]),
                "inventory": db.inventory(conn, study_id, config),
            }

    @app.post("/api/studies/{study_id}/runs/{run_id}/response", status_code=201)
    def record_response(study_id: int, run_id: int, body: dict):
        try:
            value = float(body.get("value"))
        except (TypeError, ValueError):
            raise service.ApiError(400, "响应值必须是数值")
        with db.transaction(DB_PATH) as conn:
            config = db.load_config(conn, study_id)
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ? AND study_id = ?", (run_id, study_id)
            ).fetchone()
            if row is None:
                raise service.ApiError(404, "试验不存在")
            if row["status"] == "cancelled":
                raise service.ApiError(409, "已取消的试验不能录入响应")
            if row["status"] == "planned":
                raise service.ApiError(409, "试验尚未投料，不能录入响应")
            existing = conn.execute(
                "SELECT * FROM responses WHERE run_id = ?", (run_id,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE responses SET value = ?, note = ? WHERE run_id = ?",
                    (value, str(body.get("note", "")), run_id),
                )
            else:
                conn.execute(
                    "INSERT INTO responses (run_id, value, note, created_at) VALUES (?, ?, ?, ?)",
                    (run_id, value, str(body.get("note", "")), db.utcnow()),
                )
            conn.execute(
                "UPDATE runs SET status = 'completed', completed_at = ? WHERE id = ?",
                (db.utcnow(), run_id),
            )
            return {
                "ok": True,
                "study": service.study_snapshot(conn, study_id),
                "inventory": db.inventory(conn, study_id, config),
            }

    @app.post("/api/studies/{study_id}/runs/{run_id}/fail")
    def fail_run(study_id: int, run_id: int, body: dict | None = None):
        """Mark a dosed run as failed: material stays consumed, no response."""
        note = str((body or {}).get("note", "试验失败"))
        with db.transaction(DB_PATH) as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ? AND study_id = ?", (run_id, study_id)
            ).fetchone()
            if row is None:
                raise service.ApiError(404, "试验不存在")
            if row["status"] != "dosed":
                raise service.ApiError(409, "只有已投料、尚无响应的试验可以标记失败")
            conn.execute(
                "UPDATE runs SET status = 'failed', completed_at = ? WHERE id = ?",
                (db.utcnow(), run_id),
            )
            conn.execute(
                "INSERT INTO responses (run_id, value, note, created_at) VALUES (?, NULL, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET note = excluded.note",
                (run_id, note, db.utcnow()),
            )
        return {"ok": True, "status": "failed"}

    @app.delete("/api/studies/{study_id}/runs/{run_id}/response")
    def delete_response(study_id: int, run_id: int):
        with db.transaction(DB_PATH) as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE id = ? AND study_id = ?", (run_id, study_id)
            ).fetchone()
            if row is None:
                raise service.ApiError(404, "试验不存在")
            conn.execute("DELETE FROM responses WHERE run_id = ?", (run_id,))
            conn.execute(
                "UPDATE runs SET status = 'dosed', completed_at = NULL WHERE id = ?",
                (run_id,),
            )
        return {"ok": True}

    # ------------------------------------------------------------------ diagnostics

    @app.get("/api/studies/{study_id}/diagnostics")
    def diagnostics(study_id: int):
        with db.read_conn(DB_PATH) as conn:
            return service.diagnostic(conn, study_id)

    @app.get("/api/studies/{study_id}/report")
    def report(study_id: int):
        with db.read_conn(DB_PATH) as conn:
            diag = service.diagnostic(conn, study_id)
            config = db.load_config(conn, study_id)
            snap = service.study_snapshot(conn, study_id)
            return {
                "generated_at": db.utcnow(),
                "study": snap["study"],
                "response_name": config["response_name"],
                "config": config,
                "inventory": snap["inventory"],
                "history": snap["history"],
                "diagnostics": diag,
                "tasks": snap["tasks"],
            }

    if FRONTEND.exists():
        app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

        @app.get("/")
        def index():
            return FileResponse(str(FRONTEND / "index.html"))

    return app


app = make_app()
