"""Study-level service: region config, fitting, batch planning, ledger actions.

All writes are done inside ``db.transaction`` so that concurrent planners see
one consistent set of holds and cannot double-spend stock.
"""

from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

import numpy as np

from . import db
from .design import (
    RIDGE,
    DesignSpec,
    ModelTerms,
    build_matrix,
    build_terms,
    fit_model,
    posterior_covariance,
    prediction_variance,
    rank_and_aliases,
    relative_prediction_variance,
    select_batch,
)
from .mixture import (
    RegionSpec,
    blend_violations,
    enumerate_region,
    region_from_config,
)


def _amounts_for(spec: RegionSpec, amounts: dict) -> tuple[float, ...]:
    out = []
    for code in spec.codes:
        if code not in amounts:
            raise ApiError(400, f"缺少成分 {code} 的数量")
        try:
            out.append(float(amounts[code]))
        except (TypeError, ValueError):
            raise ApiError(400, f"成分 {code} 的数量不是数值") from None
    extra = set(amounts) - set(spec.codes)
    if extra:
        raise ApiError(400, f"未知成分：{', '.join(sorted(extra))}")
    return tuple(out)


class ApiError(Exception):
    def __init__(self, status: int, message: str, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.detail = detail

    def payload(self) -> dict:
        out = {"error": self.message}
        if self.detail is not None:
            out["detail"] = self.detail
        return out


# ---------------------------------------------------------------------------
# Config


def default_config() -> dict:
    return {
        "components": [
            {"code": "A", "name": "树脂 A", "lower": 20, "upper": 60, "unit": "g"},
            {"code": "B", "name": "填料 B", "lower": 0, "upper": 50, "unit": "g"},
            {"code": "C", "name": "增塑剂 C", "lower": 0, "upper": 40, "unit": "g"},
            {"code": "D", "name": "稳定剂 D", "lower": 0, "upper": 30, "unit": "g"},
            {"code": "E", "name": "色浆 E", "lower": 0, "upper": 25, "unit": "g"},
        ],
        "region": {
            "total": 100,
            "n_steps": 20,
            "forbidden_pairs": [["B", "E"]],
            "forbidden_groups": [["C", "D"]],
        },
        "process_levels": ["低", "中", "高"],
        "min_per_level": 1,
        "model": "quadratic",
        "include_process": True,
        "allow_replicates": True,
        "batch_size": 12,
        "charge_mass": 120,
        "stock": {"A": 2600, "B": 1800, "C": 1200, "D": 600, "E": 500},
        "response_name": "拉伸强度 MPa",
    }


def _clean_config(config: dict) -> dict:
    base = default_config()
    components = config.get("components", base["components"])
    if not isinstance(components, list) or not components:
        raise ApiError(400, "components 必须是非空列表")
    cleaned_comps = []
    codes = set()
    for comp in components:
        code = str(comp.get("code", "")).strip()
        if not code:
            raise ApiError(400, "每个成分都需要 code")
        if code in codes:
            raise ApiError(400, f"成分代码重复：{code}")
        codes.add(code)
        cleaned_comps.append(
            {
                "code": code,
                "name": str(comp.get("name", code)),
                "lower": float(comp.get("lower", 0.0)),
                "upper": float(comp.get("upper", base["region"]["total"])),
                "unit": str(comp.get("unit", "g")),
            }
        )
    region = {**base["region"], **config.get("region", {})}
    process_levels = [str(x) for x in config.get("process_levels", base["process_levels"])]
    out = {
        "components": cleaned_comps,
        "region": {
            "total": float(region.get("total", 100)),
            "n_steps": int(region.get("n_steps", 20)),
            "forbidden_pairs": [list(map(str, p)) for p in region.get("forbidden_pairs", [])],
            "forbidden_groups": [
                list(map(str, g)) for g in region.get("forbidden_groups", [])
            ],
        },
        "process_levels": process_levels,
        "min_per_level": max(1, int(config.get("min_per_level", 1))),
        "model": config.get("model", "quadratic"),
        "include_process": bool(config.get("include_process", True)),
        "allow_replicates": bool(config.get("allow_replicates", True)),
        "batch_size": max(1, int(config.get("batch_size", 8))),
        "charge_mass": float(config.get("charge_mass", region.get("total", 100))),
        "stock": {k: float(v) for k, v in config.get("stock", {}).items()},
        "response_name": str(config.get("response_name", "响应")),
    }
    if out["model"] not in ("linear", "quadratic"):
        raise ApiError(400, "model 必须是 linear 或 quadratic")
    if len(out["process_levels"]) > 6:
        raise ApiError(400, "加工档位最多 6 个")
    if len(set(out["process_levels"])) != len(out["process_levels"]):
        raise ApiError(400, "加工档位重复")
    if out["include_process"] and not out["process_levels"]:
        raise ApiError(400, "启用加工档位时至少需要一个档位")
    # Validate the mixture region eagerly so bad config never reaches planning.
    region_from_config(out["components"], out["region"])
    return out


def plan_task(
    conn: sqlite3.Connection,
    study_id: int,
    task_id: int,
    design_overrides: dict | None = None,
) -> dict:
    """Recompute the next batch for a task and persist it as a new revision."""
    config = db.load_config(conn, study_id)
    spec = region_from_config(config["components"], config["region"])
    region = enumerate_region(spec)
    if not region.candidates:
        raise ApiError(422, "区域为空，无法生成试验", region.blocking)

    task = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND study_id = ?", (task_id, study_id)
    ).fetchone()
    if task is None:
        raise ApiError(404, "任务不存在")

    design_json = json.loads(task["design_json"])
    merged = {**design_json, **(design_overrides or {})}
    design = DesignSpec(
        model=merged.get("model", config["model"]),
        include_process=merged.get("include_process", config["include_process"]),
        process_levels=tuple(config["process_levels"]),
        min_per_level=int(merged.get("min_per_level", config["min_per_level"])),
        allow_replicates=bool(merged.get("allow_replicates", config["allow_replicates"])),
        batch_size=int(merged.get("batch_size", config["batch_size"])),
    )

    locked_rows = conn.execute(
        "SELECT * FROM runs WHERE task_id = ? AND locked = 1 AND status = 'planned'",
        (task_id,),
    ).fetchall()
    locked_inputs = []
    for row in locked_rows:
        amounts = json.loads(row["amounts_json"])
        locked_inputs.append(
            (tuple(float(amounts[c]) for c in spec.codes), row["level"])
        )

    # Material this task may commit: balance minus every *other* task's holds.
    # Its own holds are about to be replaced by this revision, so they are free
    # for this task and for nobody else.
    available = db.available_for_task(conn, study_id, config, task_id)

    result = select_batch(
        spec,
        region.candidates,
        design,
        stock_available=available,
        charge_mass=config["charge_mass"],
        locked_amounts=locked_inputs,
    )
    revision = (
        conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 AS r FROM batches WHERE task_id = ?",
            (task_id,),
        ).fetchone()["r"]
    )
    cur = conn.execute(
        "INSERT INTO batches (task_id, revision, created_at, design_json, requested_size, "
        "feasible_size, partial, blocking_json, metrics_json, search) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            task_id,
            revision,
            db.utcnow(),
            json.dumps(merged, ensure_ascii=False),
            result.requested_size,
            result.feasible_size,
            1 if result.partial else 0,
            json.dumps(result.blocking, ensure_ascii=False),
            json.dumps(result.metrics, ensure_ascii=False),
            result.search,
        ),
    )
    batch_id = cur.lastrowid
    run_ids = []
    for run in result.runs:
        rc = conn.execute(
            "INSERT INTO runs (study_id, task_id, batch_id, ordinal, amounts_json, level, "
            "usage_json, charge_mass, locked, duplicate, mixture_info, total_info, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?)",
            (
                study_id,
                task_id,
                batch_id,
                run.ordinal,
                json.dumps({c: a for c, a in zip(spec.codes, run.amounts)}),
                run.level,
                json.dumps({c: u for c, u in zip(spec.codes, run.usage)}),
                config["charge_mass"],
                1 if run.locked else 0,
                1 if run.duplicate else 0,
                run.mixture_info,
                run.total_info,
                db.utcnow(),
            ),
        )
        run_ids.append(rc.lastrowid)
    return {
        "batch_id": batch_id,
        "revision": revision,
        "run_ids": run_ids,
        "requested_size": result.requested_size,
        "feasible_size": result.feasible_size,
        "partial": result.partial,
        "blocking": result.blocking,
        "metrics": result.metrics,
        "search": result.search,
    }


def replace_open_revision(
    conn: sqlite3.Connection,
    study_id: int,
    task_id: int,
    design_overrides: dict | None = None,
) -> dict:
    """Cancel the task's previous *unlocked* planned runs, then plan anew.

    Locked runs are the user's explicit instruction to keep a formulation, so
    they are not touched here: ``plan_task`` re-inserts them as locked runs in
    the new revision and their holds count against this task's stock.  Dosed
    runs are untouched too: their material is already consumed and their hold
    is a ledger entry, not a reservation.
    """
    conn.execute(
        "UPDATE runs SET status = 'cancelled', cancelled_at = ? "
        "WHERE task_id = ? AND status = 'planned' AND locked = 0",
        (db.utcnow(), task_id),
    )
    result = plan_task(conn, study_id, task_id, design_overrides)
    # Locked runs were carried into the new revision; retire the copies that
    # belong to older revisions so the ledger does not hold the same material
    # twice.
    conn.execute(
        "UPDATE runs SET status = 'cancelled', cancelled_at = ? "
        "WHERE task_id = ? AND status = 'planned' AND locked = 1 "
        "AND batch_id NOT IN (SELECT id FROM batches WHERE task_id = ? AND revision = ?)",
        (db.utcnow(), task_id, task_id, result["revision"]),
    )
    return result


# ---------------------------------------------------------------------------
# Task views


def task_detail(conn: sqlite3.Connection, study_id: int, task_id: int) -> dict:
    task = conn.execute(
        "SELECT * FROM tasks WHERE id = ? AND study_id = ?", (task_id, study_id)
    ).fetchone()
    if task is None:
        raise ApiError(404, "任务不存在")
    batches = []
    for b in conn.execute(
        "SELECT * FROM batches WHERE task_id = ? ORDER BY revision DESC", (task_id,)
    ):
        runs = []
        for r in conn.execute(
            "SELECT * FROM runs WHERE batch_id = ? ORDER BY ordinal", (b["id"],)
        ):
            runs.append(_run_dict(r))
        batches.append(
            {
                "id": b["id"],
                "revision": b["revision"],
                "created_at": b["created_at"],
                "requested_size": b["requested_size"],
                "feasible_size": b["feasible_size"],
                "partial": bool(b["partial"]),
                "blocking": json.loads(b["blocking_json"]),
                "metrics": json.loads(b["metrics_json"]),
                "search": b["search"],
                "runs": runs,
            }
        )
    return {
        "id": task["id"],
        "study_id": task["study_id"],
        "name": task["name"],
        "status": task["status"],
        "design": json.loads(task["design_json"]),
        "batches": batches,
    }


def _run_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "ordinal": row["ordinal"],
        "amounts": json.loads(row["amounts_json"]),
        "level": row["level"],
        "usage": json.loads(row["usage_json"]),
        "charge_mass": row["charge_mass"],
        "locked": bool(row["locked"]),
        "duplicate": bool(row["duplicate"]),
        "mixture_info": row["mixture_info"],
        "total_info": row["total_info"],
        "status": row["status"],
        "batch_id": row["batch_id"],
        "task_id": row["task_id"],
    }


def study_snapshot(conn: sqlite3.Connection, study_id: int) -> dict:
    config = db.load_config(conn, study_id)
    spec = region_from_config(config["components"], config["region"])
    return {
        "study": {"id": study_id, "name": db.get_study(conn, study_id)["name"]},
        "config": config,
        "region_summary": {
            "total": spec.total,
            "n_steps": spec.n_steps,
            "n_components": len(spec.codes),
        },
        "inventory": db.inventory(conn, study_id, config),
        "history": [
            {**ob, "amounts": ob["amounts"], "response": ob["response"]}
            for ob in db.response_rows(conn, study_id)
        ],
        "failed_runs": db.failed_runs(conn, study_id),
        "tasks": [
            dict(r) for r in conn.execute(
                "SELECT * FROM tasks WHERE study_id = ? ORDER BY id", (study_id,)
            )
        ],
    }


def diagnostic(conn: sqlite3.Connection, study_id: int) -> dict:
    """Fit the current model and compare estimability before / after the open batch."""
    config = db.load_config(conn, study_id)
    spec = region_from_config(config["components"], config["region"])
    region = enumerate_region(spec)
    if not region.candidates:
        raise ApiError(422, "区域为空", region.blocking)
    design = DesignSpec(
        model=config["model"],
        include_process=config["include_process"],
        process_levels=tuple(config["process_levels"]),
        min_per_level=config["min_per_level"],
        allow_replicates=config["allow_replicates"],
        batch_size=config["batch_size"],
    )
    terms = build_terms(spec, region.candidates, design)
    labels = [t.label for t in terms.terms]
    # A model cannot identify a component the region never varies.  Report it
    # as a structural fact rather than letting users wonder why no design fixes it.
    structural: list[dict] = []
    table = np.array([[c.amounts[i] / spec.total for c in region.candidates] for i in range(len(spec.codes))])
    if len(region.candidates) >= 2:
        span = table @ table.T
        span = span / max(float(np.trace(span)), 1e-12)
        for i, code in enumerate(spec.codes):
            frac = float(span[i, i])
            if frac < 1e-4:
                structural.append(
                    {
                        "component": code,
                        "message": f"{code} 在可行区域内几乎不变（方差占比 {frac:.2%}），其主效应与交互项实际不可辨识",
                        "variance_share": frac,
                    }
                )
    obs = db.response_rows(conn, study_id)
    runs = []
    excluded: list[dict] = []
    excluded_refs: set[str] = set()
    for ob in obs:
        try:
            amounts = tuple(float(ob["amounts"][c]) for c in spec.codes)
        except KeyError:
            excluded.append({"ref": ob["ref"], "reason": "缺少成分"})
            excluded_refs.add(ob["ref"])
            continue
        if blend_violations(amounts, spec):
            # Historical formulations outside the declared region cannot inform
            # a model defined on that region; they are reported, not fitted.
            excluded.append({"ref": ob["ref"], "reason": "配方违反区域约束"})
            excluded_refs.add(ob["ref"])
            continue
        runs.append((amounts, ob["level"], float(ob["response"])))
    rows_before = build_matrix(
        terms, design, [(a, lv) for a, lv, _ in runs], spec.total
    )
    before = rank_and_aliases(rows_before, labels)

    open_rows = conn.execute(
        "SELECT r.* FROM runs r WHERE r.study_id = ? AND r.status IN ('planned','dosed') "
        "ORDER BY r.id",
        (study_id,),
    ).fetchall()
    planned = [r for r in open_rows if r["status"] == "planned"]
    dosed_no_resp = [r for r in open_rows if r["status"] == "dosed"]
    extra = [
        (tuple(float(json.loads(r["amounts_json"])[c]) for c in spec.codes), r["level"])
        for r in planned + dosed_no_resp
    ]
    rows_after = build_matrix(
        terms, design, [(a, lv) for a, lv, _ in runs] + extra, spec.total
    )
    after = rank_and_aliases(rows_after, labels)

    fit = fit_model(terms, design, spec, runs)
    fit_after = (
        fit_model(
            terms,
            design,
            spec,
            runs
            + [(a, lv, _impute(fit, terms, design, spec, (a, lv))) for a, lv in extra],
        )
        if extra
        else fit
    )

    rows_pool = build_matrix(
        terms, design, [(c.amounts, None) for c in region.candidates], spec.total
    )
    mean_var_before = (
        float(np.mean(relative_prediction_variance(rows_before, rows_pool)))
        if len(rows_before)
        else None
    )
    mean_var_after = (
        float(np.mean(relative_prediction_variance(rows_after, rows_pool)))
        if len(rows_after)
        else None
    )
    # Normalised posterior SD: 1.0 means "no information beyond the prior".
    def normalised(rows):
        if len(rows) == 0:
            return [1.0] * len(labels)
        cov = posterior_covariance(rows)
        return [
            float(math.sqrt(max(cov[i, i], 0.0)) * math.sqrt(RIDGE))
            for i in range(len(labels))
        ]

    sd_before = normalised(rows_before)
    sd_after = normalised(rows_after)
    aliased_before = set(before["aliased"])
    aliased_after = set(after["aliased"])
    terms_view = [
        {
            "term": label,
            "before_sd": sd_before[i],
            "after_sd": sd_after[i],
            "identifiable_before": label not in aliased_before,
            "identifiable_after": label not in aliased_after,
        }
        for i, label in enumerate(labels)
    ]

    return {
        "labels": labels,
        "before": before,
        "after": after,
        "mean_variance_before": mean_var_before,
        "mean_variance_after": mean_var_after,
        "terms": terms_view,
        "structural": structural,
        "n_new_runs": len(extra),
        "sigma2": fit.sigma2,
        "fitted": fit.as_dict(),
        "fitted_after": fit_after.as_dict(),
        "history": [
            {
                "ref": ob["ref"],
                "source": ob["source"],
                "response": ob["response"],
                "level": ob["level"],
                "amounts": ob["amounts"],
                "note": ob["note"],
            }
            for ob in obs
            if ob["ref"] not in excluded_refs
        ],
        "failed": db.failed_runs(conn, study_id),
        "excluded": excluded,
        "open_runs": [
            {**_run_dict(r), "kind": "planned"} for r in planned
        ] + [{**_run_dict(r), "kind": "dosed_no_response"} for r in dosed_no_resp],
    }


def _impute(fit, terms: ModelTerms, design: DesignSpec, spec: RegionSpec, run) -> float:
    if fit is None or fit.n_obs == 0:
        return 0.0
    amounts, level = run
    X = build_matrix(terms, design, [(amounts, level)], spec.total)
    return float((X @ fit.beta)[0])
