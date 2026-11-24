"""Seed a fictional but internally consistent study.

History: 17 historical formulations with responses from a hidden truth plus
noise, two of which are failures (no response).  One more lies outside the
current region, so it is imported but excluded from the fit.  The truth is deliberately
"bumpy": a strong A*C interaction and a process effect, so the fitted model
has something real to recover.  The history does NOT identify the interaction
terms - that is what the next batch is for.
"""

from __future__ import annotations

import json
import random

from . import db, service

TRUE_BETA = {"A": 42.0, "B": 28.0, "C": 18.0, "D": 5.0, "E": 12.0}
TRUE_INTERACTION = {("A", "C"): -0.9, ("A", "B"): 0.35, ("B", "C"): -0.25}
TRUE_LEVEL = {"低": 0.0, "中": 2.5, "高": 6.0}

HISTORY = [
    ("H-101", {"A": 50, "B": 30, "C": 20, "D": 0, "E": 0}, "低", True),
    ("H-102", {"A": 40, "B": 40, "C": 20, "D": 0, "E": 0}, "中", False),
    ("H-103", {"A": 60, "B": 30, "C": 10, "D": 0, "E": 0}, "低", True),
    ("H-104", {"A": 35, "B": 35, "C": 30, "D": 0, "E": 0}, "中", True),
    ("H-105", {"A": 60, "B": 20, "C": 20, "D": 0, "E": 0}, "高", True),
    ("H-106", {"A": 30, "B": 50, "C": 20, "D": 0, "E": 0}, "低", False),
    ("H-107", {"A": 45, "B": 35, "C": 20, "D": 0, "E": 0}, "高", True),
    ("H-108", {"A": 50, "B": 40, "C": 10, "D": 0, "E": 0}, "中", True),
    ("H-109", {"A": 40, "B": 20, "C": 40, "D": 0, "E": 0}, "高", True),
    ("H-110", {"A": 55, "B": 25, "C": 20, "D": 0, "E": 0}, "低", True),
    ("H-111", {"A": 30, "B": 40, "C": 30, "D": 0, "E": 0}, "中", False),
    ("H-112", {"A": 60, "B": 40, "C": 0, "D": 0, "E": 0}, "低", True),
    ("H-113", {"A": 35, "B": 50, "C": 0, "D": 15, "E": 0}, "中", True),
    ("H-114", {"A": 60, "B": 25, "C": 0, "D": 15, "E": 0}, "高", True),
    ("H-115", {"A": 30, "B": 0, "C": 10, "D": 0, "E": 10}, "中", False),
    ("H-116", {"A": 45, "B": 0, "C": 35, "D": 0, "E": 10}, "低", True),
    # Imported from an older line whose limits were wider: outside this region,
    # so it is stored and shown but never fitted.
    ("H-117", {"A": 80, "B": 20, "C": 0, "D": 0, "E": 0}, "低", True),
]


def true_response(amounts: dict[str, float], level: str) -> float:
    total = sum(amounts.values())
    z = {code: amounts[code] / total for code in TRUE_BETA}
    y = sum(TRUE_BETA[code] * z[code] for code in TRUE_BETA)
    for (a, b), coef in TRUE_INTERACTION.items():
        y += coef * 100 * z[a] * z[b]
    y += TRUE_LEVEL.get(level, 0.0)
    return y


def seed(conn, study_id: int) -> dict:
    rng = random.Random(20261124)
    created = 0
    failed = 0
    for label, amounts, level, has_response in HISTORY:
        y = true_response(amounts, level) + rng.gauss(0, 0.35)
        conn.execute(
            "INSERT INTO history (study_id, label, amounts_json, level, response, note, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                study_id,
                label,
                json.dumps(amounts),
                level,
                round(y, 2) if has_response else None,
                "历史批次" if has_response else "历史批次：试样开裂，无有效响应",
                "completed" if has_response else "failed",
                db.utcnow(),
            ),
        )
        if has_response:
            created += 1
        else:
            failed += 1
    return {"history": created, "failed": failed}


def build_demo(c=None, force: bool = False) -> dict:
    """Create the demo study (idempotent unless force=True)."""
    from . import api

    conn = db.connect(api.DB_PATH)
    try:
        existing = conn.execute("SELECT id FROM studies ORDER BY id LIMIT 1").fetchone()
        if existing is not None and not force:
            return {"study_id": existing["id"], "created": False}
        if existing is not None and force:
            conn.execute("DELETE FROM studies")
            conn.commit()
        config = service.default_config()
        cur = conn.execute(
            "INSERT INTO studies (name, config_json, created_at) VALUES (?, ?, ?)",
            ("树脂配方 · 响应面设计", json.dumps(config, ensure_ascii=False), db.utcnow()),
        )
        study_id = cur.lastrowid
        for code, qty in config["stock"].items():
            conn.execute(
                "INSERT INTO stock_entries (study_id, component_code, delta, kind, note, created_at) "
                "VALUES (?, ?, ?, 'receipt', '期初入库', ?)",
                (study_id, code, float(qty), db.utcnow()),
            )
        conn.commit()
        result = seed(conn, study_id)
        conn.commit()
        t = conn.execute(
            "INSERT INTO tasks (study_id, name, design_json, status, created_at) "
            "VALUES (?, ?, ?, 'open', ?)",
            (
                study_id,
                "第二批 · 补充 AC 交互",
                json.dumps(
                    {
                        "model": "quadratic",
                        "include_process": True,
                        "batch_size": config["batch_size"],
                        "min_per_level": config["min_per_level"],
                        "allow_replicates": config["allow_replicates"],
                    },
                    ensure_ascii=False,
                ),
                db.utcnow(),
            ),
        )
        conn.commit()
        try:
            service.plan_task(conn, study_id, t.lastrowid)
            conn.commit()
        except Exception:  # pragma: no cover - demo remains usable without a plan
            conn.rollback()
        return {"study_id": study_id, "created": True, **result}
    finally:
        conn.close()


if __name__ == "__main__":  # pragma: no cover
    print(build_demo())
