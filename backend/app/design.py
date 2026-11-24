"""Mixture response-surface model, D-optimal batch design, estimability.

Model (Scheffé form on proportions ``z_i = x_i / total``):

    y = sum_i beta_i z_i + sum_{i<j, co-occurring} gamma_ij z_i z_j
        + sum_l delta_l 1[level = l] + noise

The intercept is not a separate column: the linear mixture terms sum to one,
so they span it.  Interaction terms are only included for pairs that can
actually co-occur inside the region; a forbidden pair has ``z_i z_j == 0``
everywhere and would be an unidentifiable column.

Criterion.  For a batch ``D`` the information matrix is ``M(D) = X'X`` on
column-scaled regressors.  The selection criterion is

    Phi(D) = log det(M(D) + ridge * I) - log det(ridge * I)

which is the standard Bayesian D-criterion with an isotropic N(0, 1/ridge)
prior.  ``ridge`` is tiny (1e-8) and exists only so that batches smaller than
the parameter count still have a finite criterion; estimability is reported
separately from the *unregularised* rank.  Every candidate and every exchange
is evaluated with this formula, so the discrete processing-level choice and
the continuous blend choice are compared on exactly the same scale.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

from .mixture import (
    Candidate,
    RegionSpec,
    RegionError,
    blend_violations,
    chebyshev_distance,
    consumption_vector,
)

RIDGE = 1e-8
RANK_TOL_REL = 1e-7
MAX_EXACT_OPTIONS = 26
MAX_EXACT_COMBOS = 60_000


@dataclass(frozen=True)
class Term:
    kind: str  # intercept | linear | interaction | process
    label: str
    index: int | None = None
    pair: tuple[int, int] | None = None
    level: str | None = None


@dataclass
class ModelTerms:
    terms: list[Term]
    scales: np.ndarray
    aliased: list[dict] = field(default_factory=list)
    cooccurring_pairs: list[tuple[int, int]] = field(default_factory=list)
    reference: str | None = None


@dataclass
class DesignSpec:
    model: str = "quadratic"  # linear | quadratic
    include_process: bool = True
    process_levels: tuple[str, ...] = ()
    min_per_level: int = 1
    allow_replicates: bool = True
    batch_size: int = 6
    locked: list[dict] = field(default_factory=list)  # {"amounts": {...}, "level": str|None}


@dataclass
class PlannedRun:
    ordinal: int
    amounts: tuple[float, ...]
    level: str | None
    usage: tuple[float, ...]
    duplicate: bool
    locked: bool
    mixture_info: float
    total_info: float

    def as_dict(self, spec: RegionSpec) -> dict:
        return {
            "ordinal": self.ordinal,
            "component_codes": list(spec.codes),
            "amounts": {c: a for c, a in zip(spec.codes, self.amounts)},
            "level": self.level,
            "usage": {c: u for c, u in zip(spec.codes, self.usage)},
            "duplicate": self.duplicate,
            "locked": self.locked,
            "mixture_info": self.mixture_info,
            "process_info": self.total_info - self.mixture_info,
            "total_info": self.total_info,
        }


@dataclass
class BatchResult:
    runs: list[PlannedRun]
    requested_size: int
    feasible_size: int
    partial: bool
    blocking: list[dict]
    metrics: dict
    search: str


# ---------------------------------------------------------------------------
# Model terms
#
# The mixture part is reference-coded.  In the Scheffé form
# ``sum_i beta_i z_i`` every set of coefficients can be shifted by a constant
# without changing predictions (because ``sum z_i = 1``), so at most k-1 linear
# directions are identifiable.  Writing the same model as
#
#     y = beta_ref + sum_{i != ref} (beta_i - beta_ref) z_i + ...
#
# keeps the model identical but makes every coefficient a genuine contrast, so
# a rank-deficiency reported later means the *design* failed, not the algebra.


def build_terms(
    spec: RegionSpec, pool: Sequence[Candidate], design: DesignSpec
) -> ModelTerms:
    z_pool = np.array([[a / spec.total for a in c.amounts] for c in pool])
    k = len(spec.codes)
    ref = spec.codes[0]
    terms: list[Term] = [Term("intercept", f"基准 {ref}")]
    for i in range(1, k):
        terms.append(Term("linear", f"{spec.codes[i]} − {ref}", index=i))

    pairs: list[tuple[int, int]] = []
    if design.model == "quadratic":
        forbidden = {tuple(sorted(p)) for p in spec.forbidden_pairs}
        groups = [set(g) for g in spec.forbidden_groups]
        for i in range(k):
            for j in range(i + 1, k):
                if (i, j) in forbidden:
                    continue
                if any(i in g and j in g for g in groups):
                    continue
                if np.any(z_pool[:, i] * z_pool[:, j] > 1e-10):
                    pairs.append((i, j))
                    terms.append(
                        Term("interaction", f"{spec.codes[i]}×{spec.codes[j]}", pair=(i, j))
                    )
    n_levels = len(design.process_levels)
    if design.include_process and n_levels >= 2:
        base = design.process_levels[0]
        for lv in design.process_levels[1:]:
            terms.append(Term("process", f"档位 {lv} − {base}", level=lv))

    # Column scales put mixture contrasts, interactions and level effects on a
    # comparable footing.  A constant column would make a coefficient
    # unidentifiable; flag it rather than silently fitting it.
    scales: list[float] = []
    aliased: list[dict] = []
    for t in terms:
        col = _pool_term_column(t, z_pool, n_levels)
        scale = float(np.sqrt(np.mean(col**2))) if len(col) else 0.0
        if scale < 1e-12:
            scales.append(1.0)
            aliased.append(
                {
                    "term": t.label,
                    "kind": "constant",
                    "message": f"{t.label} 在候选区域内为常量，系数不可辨识",
                }
            )
        else:
            scales.append(scale)
    return ModelTerms(terms, np.array(scales), aliased, pairs, ref)


def _pool_term_column(term: Term, z_pool: np.ndarray, n_levels: int) -> np.ndarray:
    """Term values over the candidate pool, used only to derive column scales."""
    if term.kind == "intercept":
        return np.ones(len(z_pool))
    if term.kind == "linear":
        return z_pool[:, term.index]  # type: ignore[index]
    if term.kind == "interaction":
        i, j = term.pair  # type: ignore[misc]
        return z_pool[:, i] * z_pool[:, j]
    # A balanced pool runs every level equally often.
    return np.array([0.0, 1.0] * max(n_levels - 1, 1))


def _mixture_row(
    terms: ModelTerms,
    design: DesignSpec,
    z_row: np.ndarray,
    level: str | None,
    n_linear: int,
) -> np.ndarray:
    out = np.zeros(len(terms.terms))
    for idx, t in enumerate(terms.terms):
        if t.kind == "intercept":
            out[idx] = 1.0 / terms.scales[idx]
        elif t.kind == "linear":
            out[idx] = z_row[t.index] / terms.scales[idx]  # type: ignore[index]
        elif t.kind == "interaction":
            i, j = t.pair  # type: ignore[misc]
            out[idx] = z_row[i] * z_row[j] / terms.scales[idx]
        else:
            out[idx] = (1.0 if level == t.level else 0.0) / terms.scales[idx]
    return out


def build_matrix(
    terms: ModelTerms,
    design: DesignSpec,
    runs: Sequence[tuple[tuple[float, ...], str | None]],
    total: float,
) -> np.ndarray:
    p = len(terms.terms)
    if not runs:
        return np.empty((0, p))
    z = np.array([[a / total for a in amounts] for amounts, _ in runs])
    rows = [
        _mixture_row(terms, design, z[idx], level, len(terms.terms))
        for idx, (_, level) in enumerate(runs)
    ]
    return np.array(rows).reshape(len(runs), p)


# ---------------------------------------------------------------------------
# Linear algebra helpers


def _information(rows: np.ndarray, ridge: float = RIDGE) -> np.ndarray:
    M = rows.T @ rows
    return M + ridge * np.eye(M.shape[0])


def _logdet(M: np.ndarray) -> float:
    sign, ld = np.linalg.slogdet(M)
    if sign <= 0 or not np.isfinite(ld):
        return float("-inf")
    return float(ld)


def _criterion(rows: np.ndarray) -> float:
    if len(rows) == 0:
        return 0.0
    p = rows.shape[1]
    return _logdet(_information(rows)) - p * math.log(RIDGE)


def _finite(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def rank_and_aliases(rows: np.ndarray, labels: Sequence[str]) -> dict:
    """Rank of the unregularised information matrix + per-coefficient estimability."""
    if len(rows) == 0:
        return {
            "rank": 0,
            "n_terms": len(labels),
            "aliased": list(labels),
            "singular_values": [],
            "condition": None,
        }
    U, s, Vt = np.linalg.svd(rows, full_matrices=False)
    smax = float(s[0]) if len(s) else 0.0
    tol = max(rows.shape) * np.finfo(float).eps * smax * 100
    rank = int(np.sum(s > tol))
    V_r = Vt[:rank, :]
    ident = np.linalg.norm(V_r, axis=0)  # norm of each column of V_r
    ident_mask = ident > 0.999
    return {
        "rank": rank,
        "n_terms": len(labels),
        "aliased": [labels[i] for i in range(len(labels)) if not ident_mask[i]],
        "singular_values": [float(v) for v in s],
        "condition": float(smax / s[rank - 1]) if rank > 0 else None,
    }


def prediction_variance(rows_fit: np.ndarray, rows_eval: np.ndarray) -> np.ndarray:
    """Leverage of every evaluation point using only identifiable directions.

    A regularised inverse would assign enormous leverage to directions the
    design cannot resolve; restricting to the numerical rank reports the
    leverage actually explained by the fitted design (sum = rank).
    """
    if len(rows_fit) == 0:
        return np.full(len(rows_eval), float("inf"))
    U, s, Vt = np.linalg.svd(rows_fit, full_matrices=False)
    smax = float(s[0]) if len(s) else 0.0
    tol = max(rows_fit.shape) * np.finfo(float).eps * smax * 100
    rank = int(np.sum(s > tol))
    if rank == 0:
        return np.zeros(len(rows_eval))
    V_r = Vt[:rank, :]
    proj = rows_eval @ V_r.T
    return np.einsum("ij,ij->i", proj, proj)


def relative_prediction_variance(
    rows_fit: np.ndarray, rows_eval: np.ndarray, ridge: float = RIDGE
) -> np.ndarray:
    """Posterior predictive variance divided by the prior predictive variance.

    With the isotropic N(0, 1/ridge) prior a point's prior variance is
    ``1/ridge`` (since ``x'x`` grows with the number of terms, the ratio below
    uses ``x'x`` itself, making it a per-point fraction).  The value is in
    ``[0, 1]``: 1 means "the data told us nothing about this blend", 0 means
    "fully determined".  Unlike a rank-restricted leverage, this quantity is
    comparable across designs of different rank, which is what the
    before/after comparison needs.
    """
    if len(rows_eval) == 0:
        return np.array([])
    if len(rows_fit) == 0:
        return np.ones(len(rows_eval))
    p = rows_fit.shape[1]
    M = rows_fit.T @ rows_fit + ridge * np.eye(p)
    try:
        M_inv = np.linalg.inv(M)
    except np.linalg.LinAlgError:  # pragma: no cover - ridge keeps M invertible
        M_inv = np.linalg.pinv(M)
    post = np.einsum("ij,jk,ik->i", rows_eval, M_inv, rows_eval)
    prior = np.einsum("ij,ij->i", rows_eval, rows_eval)
    ratio = ridge * post / np.maximum(prior, 1e-300)
    return np.clip(ratio, 0.0, 1.0)


def posterior_covariance(rows_fit: np.ndarray, ridge: float = RIDGE) -> np.ndarray:
    M = _information(rows_fit, ridge)
    try:
        return np.linalg.inv(M)
    except np.linalg.LinAlgError:
        return np.linalg.pinv(M)


# ---------------------------------------------------------------------------
# Batch selection


@dataclass
class _Option:
    candidate_index: int
    level: str | None
    amounts: tuple[float, ...]
    usage: tuple[float, ...]

    def key(self) -> tuple:
        return (self.candidate_index, self.level)


def _available_stock(
    stock_available: Sequence[float] | None, charge_mass: float
) -> np.ndarray | None:
    if stock_available is None:
        return None
    return np.asarray(stock_available, dtype=float)


def _fits_stock(
    used: np.ndarray, option: _Option, available: np.ndarray | None
) -> bool:
    if available is None:
        return True
    total = used + np.asarray(option.usage, dtype=float)
    return bool(np.all(total <= available + 1e-9))


def _shortfall(
    used: np.ndarray, option: _Option, available: np.ndarray, spec: RegionSpec
) -> list[dict]:
    total = used + np.asarray(option.usage, dtype=float)
    out = []
    for i, code in enumerate(spec.codes):
        missing = total[i] - available[i]
        if missing > 1e-9:
            out.append(
                {
                    "kind": "stock",
                    "component": code,
                    "shortfall": float(missing),
                    "required": float(total[i]),
                    "available": float(available[i]),
                    "message": f"{code} 库存不足，缺 {missing:.3f}",
                }
            )
    return out


def select_batch(
    spec: RegionSpec,
    pool: Sequence[Candidate],
    design: DesignSpec,
    *,
    stock_available: Sequence[float] | None = None,
    charge_mass: float = 1.0,
    locked_amounts: Sequence[tuple[tuple[float, ...], str | None]] = (),
) -> BatchResult:
    """Select a batch of runs maximising the joint D-criterion.

    Feasibility (stock, coverage, region) is checked on the *joint* batch, not
    per run.  If the requested size cannot be met, the feasible prefix and the
    blocking constraints are returned.
    """
    terms = build_terms(spec, pool, design)
    levels = tuple(design.process_levels if design.include_process else ())
    if design.include_process and not levels:
        raise RegionError("已启用加工档位，但没有定义档位")
    if design.include_process and design.batch_size < len(levels) * design.min_per_level:
        return BatchResult(
            [],
            design.batch_size,
            0,
            True,
            [
                {
                    "kind": "level_coverage",
                    "message": (
                        f"批量 {design.batch_size} 不足以覆盖 {len(levels)} 个档位"
                        f"（每档至少 {design.min_per_level} 次）"
                    ),
                }
            ],
            {},
            "none",
        )

    available = _available_stock(stock_available, charge_mass)
    used = np.zeros(len(spec.codes))
    options: list[_Option] = []
    for ci, cand in enumerate(pool):
        usage = consumption_vector(spec, cand.amounts, charge_mass)
        for lv in levels or (None,):
            options.append(_Option(ci, lv, cand.amounts, usage))

    blocking: list[dict] = []
    chosen: list[tuple[_Option, bool]] = []

    # Locked runs: validate feasibility and stock jointly, in the given order.
    for amounts, level in locked_amounts:
        violations = blend_violations(amounts, spec)
        if violations:
            blocking.extend(violations)
            continue
        if level is not None and levels and level not in levels:
            blocking.append(
                {"kind": "level", "message": f"锁定配方使用了未定义档位 {level}"}
            )
            continue
        if levels and level is None:
            level = levels[0]
        match = _Option(
            -1,
            level,
            tuple(float(a) for a in amounts),
            consumption_vector(spec, amounts, charge_mass),
        )
        if not _fits_stock(used, match, available):
            blocking.extend(_shortfall(used, match, available, spec))  # type: ignore[arg-type]
            continue
        used = used + np.asarray(match.usage)
        chosen.append((match, True))

    if blocking:
        return BatchResult([], design.batch_size, 0, True, blocking, {}, "locked")

    target = design.batch_size
    level_counts = {lv: 0 for lv in levels}
    for opt, _ in chosen:
        if opt.level in level_counts:
            level_counts[opt.level] += 1

    def coverage_ok(extra: list[_Option]) -> bool:
        if not levels:
            return True
        counts = dict(level_counts)
        for opt in extra:
            counts[opt.level] = counts.get(opt.level, 0) + 1
        return all(counts.get(lv, 0) >= design.min_per_level for lv in levels)

    # Exact enumeration for small problems: guarantees the global optimum.
    remaining_slots = target - len(chosen)
    feasible_pool = [
        o
        for o in options
        if all(
            chebyshev_distance(o.amounts, c.amounts) > 0 or o.level != c.level
            for c, _ in chosen
        )
        or design.allow_replicates
    ]
    feasible_pool = [o for o in feasible_pool if _fits_stock(used, o, available)]
    if remaining_slots > 0 and not feasible_pool:
        # Nothing on the grid fits the material left for this task.  Report the
        # option with the smallest overrun rather than a generic "no
        # candidates", so the user sees which raw material is the blocker.
        search = "blocked"
        if options and available is not None:
            cheapest = min(
                options,
                key=lambda o: max(
                    (float(a) - float(u) for u, a in zip(o.usage, available)),
                    default=0.0,
                ),
            )
            blocking.extend(_shortfall(used, cheapest, available, spec))
        elif not blocking:
            blocking.append(
                {"kind": "candidates", "message": "没有可加入的候选试验（信息已饱和）"}
            )
    else:
        exact = _exact_search(
            terms,
            design,
            spec,
            chosen,
            feasible_pool,
            remaining_slots,
            used,
            available,
            coverage_ok,
        )
        if exact is not None:
            extra, strategy = exact
            chosen.extend((opt, False) for opt in extra)
            search = strategy
        else:
            extra, blocked = _greedy_exchange(
                terms,
                design,
                spec,
                chosen,
                feasible_pool,
                remaining_slots,
                used,
                available,
                coverage_ok,
            )
            chosen.extend((opt, False) for opt in extra)
            blocking.extend(blocked)
            search = "greedy+exchange"

    if chosen and levels and not coverage_ok([o for o, _ in chosen]):
        blocking.append(
            {
                "kind": "level_coverage",
                "message": "在库存与批量限制下无法满足档位覆盖下限",
                "counts": level_counts,
            }
        )

    # Compute per-run sequential contributions in a stable order.
    ordered = sorted(
        enumerate(chosen),
        key=lambda pair: (0 if pair[1][1] else 1, pair[1][0].level or "", pair[0]),
    )
    rows_so_far: list[np.ndarray] = []
    planned: list[PlannedRun] = []
    k_linear = len(spec.codes)
    for ordinal, (_, (opt, is_locked)) in enumerate(ordered, start=1):
        z = np.array([a / spec.total for a in opt.amounts])
        row_mix = _mixture_row(terms, design, z, None, k_linear)
        row_all = _mixture_row(terms, design, z, opt.level, k_linear)
        base_mix_rows = np.array(rows_so_far) if rows_so_far else np.empty((0, len(terms.terms)))
        mix_ids = [i for i, t in enumerate(terms.terms) if t.kind != "process"]
        base_mix = (
            base_mix_rows[:, mix_ids] if len(base_mix_rows) else np.empty((0, len(mix_ids)))
        )
        mix_info = _criterion(np.vstack([base_mix, row_mix[mix_ids]])) - _criterion(base_mix)
        total_info = _criterion(np.vstack([base_mix_rows, row_all])) - _criterion(base_mix_rows)
        duplicate = any(
            chebyshev_distance(opt.amounts, p.amounts) < 1e-9 and opt.level == p.level
            for p in planned
        )
        planned.append(
            PlannedRun(
                ordinal=ordinal,
                amounts=tuple(opt.amounts),
                level=opt.level,
                usage=tuple(float(u) for u in opt.usage),
                duplicate=duplicate,
                locked=is_locked,
                mixture_info=mix_info if math.isfinite(mix_info) else 0.0,
                total_info=total_info if math.isfinite(total_info) else 0.0,
            )
        )
        rows_so_far.append(row_all)

    rows = (
        np.array(rows_so_far)
        if rows_so_far
        else np.empty((0, len(terms.terms)))
    )
    metrics = _batch_metrics(terms, rows, planned, spec, pool)
    feasible_size = len(planned)
    if feasible_size < target and not blocking:
        blocking.append(
            {
                "kind": "batch_size",
                "message": f"只能满足 {feasible_size}/{target} 次试验",
            }
        )
    return BatchResult(
        planned,
        target,
        feasible_size,
        feasible_size < target,
        blocking,
        metrics,
        search,
    )


def _batch_metrics(
    terms: ModelTerms,
    rows: np.ndarray,
    planned: Sequence[PlannedRun],
    spec: RegionSpec,
    pool: Sequence[Candidate],
) -> dict:
    labels = [t.label for t in terms.terms]
    info = rank_and_aliases(rows, labels)
    rows_pool = build_matrix(
        terms, DesignSpec(), [(c.amounts, None) for c in pool], spec.total
    )
    mix_ids = [i for i, t in enumerate(terms.terms) if t.kind != "process"]
    rows_pool_mix = rows_pool[:, mix_ids] if mix_ids else rows_pool
    rows_mix = rows[:, mix_ids] if mix_ids else rows
    lev_var = prediction_variance(rows, rows_pool)
    lev_var_mix = prediction_variance(rows_mix, rows_pool_mix) if len(rows_mix) else None
    return {
        "criterion": _criterion(rows) if len(rows) else 0.0,
        "mixture_criterion": _criterion(rows_mix) if len(rows_mix) else 0.0,
        "purpose": "bayesian D-optimal, ridge=%g" % RIDGE,
        "rank": info["rank"],
        "n_terms": info["n_terms"],
        "aliased_terms": info["aliased"],
        "condition": _finite(info["condition"]) if info["condition"] is not None else None,
        "singular_values": info["singular_values"],
        "mean_leverage": _finite(np.mean(lev_var)) if len(lev_var) else None,
        "max_leverage": _finite(np.max(lev_var)) if len(lev_var) else None,
        "n_runs": len(planned),
        "mean_mixture_leverage": _finite(np.mean(lev_var_mix))
        if lev_var_mix is not None and len(lev_var_mix)
        else None,
        "duplicates": [p.ordinal for p in planned if p.duplicate],
        "levels": [p.level for p in planned],
    }


def _greedy_exchange(
    terms: ModelTerms,
    design: DesignSpec,
    spec: RegionSpec,
    chosen: list[tuple[_Option, bool]],
    options: Sequence[_Option],
    slots: int,
    used: np.ndarray,
    available: np.ndarray | None,
    coverage_ok,
) -> tuple[list[_Option], list[dict]]:
    """Greedy forward selection followed by best-improvement pairwise exchange.

    Each step picks the option with the largest increase in the joint
    Bayesian D-criterion of the whole batch.  When only as many slots remain as
    there are uncovered levels, an uncovered level is forced - the same
    criterion is then used to choose the blend.
    """
    k_linear = len(spec.codes)
    levels = tuple(design.process_levels if design.include_process else ())
    p = len(terms.terms)

    def row_for(opt: _Option) -> np.ndarray:
        z = np.array([a / spec.total for a in opt.amounts])
        return _mixture_row(terms, design, z, opt.level, k_linear)

    def rows_of(sel: Sequence[tuple[_Option, bool]]) -> np.ndarray:
        if not sel:
            return np.empty((0, p))
        return np.array([row_for(o) for o, _ in sel])

    def usage_of(sel: Sequence[tuple[_Option, bool]]) -> np.ndarray:
        total = np.zeros(len(spec.codes))
        for o, _ in sel:
            total = total + np.asarray(o.usage)
        return total

    extra: list[_Option] = []
    blocking: list[dict] = []
    for _ in range(slots):
        current = chosen + [(o, False) for o in extra]
        base = _criterion(rows_of(current))
        best = None
        best_val = base - 1e-12
        missing: list[str] = []
        if levels:
            counts = {lv: 0 for lv in levels}
            for o, _ in current:
                if o.level in counts:
                    counts[o.level] += 1
            missing = [lv for lv in levels if counts[lv] < design.min_per_level]
        slots_left = slots - len(extra)
        must_cover = set(missing) if len(missing) == slots_left and missing else set()
        for opt in options:
            if (
                any(
                    chebyshev_distance(opt.amounts, o.amounts) < 1e-9
                    and opt.level == o.level
                    for o, _ in current
                )
                and not design.allow_replicates
            ):
                continue
            if must_cover and opt.level not in must_cover:
                continue
            if available is not None and np.any(
                usage_of(current + [(opt, False)]) > available + 1e-9
            ):
                continue
            trial = current + [(opt, False)]
            val = _criterion(rows_of(trial))
            if val > best_val:
                best, best_val = opt, val
        if best is None:
            used_now = usage_of(current)
            for opt in options:
                if available is not None and np.any(
                    used_now + np.asarray(opt.usage) > available + 1e-9
                ):
                    blocking.extend(_shortfall(used_now, opt, available, spec))
                    break
            if not blocking and levels and missing:
                blocking.append(
                    {
                        "kind": "level_coverage",
                        "message": "剩余槽位无法满足加工档位覆盖：" + "、".join(missing),
                    }
                )
            if not blocking:
                blocking.append(
                    {"kind": "candidates", "message": "没有可加入的候选试验（信息已饱和）"}
                )
            break
        extra.append(best)

    improved = True
    rounds = 0
    while improved and rounds < 100:
        rounds += 1
        improved = False
        current = chosen + [(o, False) for o in extra]
        current_val = _criterion(rows_of(current))
        best_move = None
        for pos in range(len(chosen), len(current)):
            for opt in options:
                if opt is current[pos][0]:
                    continue
                trial = list(current)
                trial[pos] = (opt, False)
                if (
                    any(
                        chebyshev_distance(opt.amounts, o.amounts) < 1e-9
                        and opt.level == o.level
                        for idx, (o, _) in enumerate(trial)
                        if idx != pos
                    )
                    and not design.allow_replicates
                ):
                    continue
                if not coverage_ok([o for o, _ in trial[len(chosen) :]]):
                    continue
                if available is not None and np.any(
                    usage_of(trial) > available + 1e-9
                ):
                    continue
                val = _criterion(rows_of(trial))
                if val > current_val + 1e-10 and (
                    best_move is None or val > best_move[0]
                ):
                    best_move = (val, trial)
        if best_move is not None:
            current_val, current = best_move
            extra = [o for o, _ in current[len(chosen) :]]
            improved = True
    return extra, blocking


def _exact_search(
    terms: ModelTerms,
    design: DesignSpec,
    spec: RegionSpec,
    chosen: list[tuple[_Option, bool]],
    options: Sequence[_Option],
    slots: int,
    used: np.ndarray,
    available: np.ndarray | None,
    coverage_ok,
) -> tuple[list[_Option], str] | None:
    if slots <= 0:
        return [], "exact"
    if len(options) == 0:
        return None
    n_combos = math.comb(len(options) + slots - 1, slots)
    if len(options) > MAX_EXACT_OPTIONS or n_combos > MAX_EXACT_COMBOS:
        return None
    k_linear = len(spec.codes)

    def row_for(opt: _Option) -> np.ndarray:
        z = np.array([a / spec.total for a in opt.amounts])
        return _mixture_row(terms, design, z, opt.level, k_linear)

    best_val = float("-inf")
    best: list[_Option] | None = None
    base_rows = (
        np.array(
            [
                row_for(o)
                for o, _ in chosen
            ]
        )
        if chosen
        else np.empty((0, len(terms.terms)))
    )
    for combo in itertools.combinations_with_replacement(options, slots):
        # identical options may repeat only if replicates are allowed
        if not design.allow_replicates:
            keys = [o.key() for o in combo]
            if len(set(keys)) != len(keys):
                continue
        if not coverage_ok(list(combo)):
            continue
        usage = used.copy()
        ok = True
        for o in combo:
            usage = usage + np.asarray(o.usage)
            if available is not None and np.any(usage > available + 1e-9):
                ok = False
                break
        if not ok:
            continue
        rows = np.vstack([base_rows, np.array([row_for(o) for o in combo])])
        val = _criterion(rows)
        if val > best_val:
            best_val = val
            best = list(combo)
    if best is None:
        return None
    return best, "exact-enumeration"


# ---------------------------------------------------------------------------
# Fitting


@dataclass
class FitResult:
    beta: np.ndarray
    labels: list[str]
    covariance: np.ndarray
    posterior_sd: np.ndarray
    rank: int
    aliased: list[str]
    sigma2: float
    n_obs: int
    residuals: np.ndarray
    condition: float | None

    def as_dict(self) -> dict:
        return {
            "coefficients": {l: float(b) for l, b in zip(self.labels, self.beta)},
            "posterior_sd": {l: float(s) for l, s in zip(self.labels, self.posterior_sd)},
            "rank": self.rank,
            "n_terms": len(self.labels),
            "aliased_terms": self.aliased,
            "sigma2": self.sigma2,
            "n_obs": self.n_obs,
            "condition": self.condition,
            "residuals": [float(r) for r in self.residuals],
        }


def fit_model(
    terms: ModelTerms,
    design: DesignSpec,
    spec: RegionSpec,
    runs: Sequence[tuple[tuple[float, ...], str | None, float]],
    ridge: float = RIDGE,
) -> FitResult:
    labels = [t.label for t in terms.terms]
    if not runs:
        p = len(labels)
        cov = np.eye(p) / ridge
        return FitResult(
            np.zeros(p), labels, cov, np.full(p, math.sqrt(1 / ridge)), 0, labels, 1.0, 0, np.array([]), None
        )
    X = build_matrix(terms, design, [(a, lv) for a, lv, _ in runs], spec.total)
    y = np.array([r for _, _, r in runs], dtype=float)
    U, s, Vt = np.linalg.svd(X, full_matrices=False)
    smax = float(s[0]) if len(s) else 0.0
    tol = max(X.shape) * np.finfo(float).eps * smax * 100
    rank = int(np.sum(s > tol))
    V_r = Vt[:rank, :]
    beta = V_r.T @ ((U[:, :rank].T @ y) / s[:rank])
    resid = y - X @ beta
    df = max(len(y) - rank, 0)
    sigma2 = float(resid @ resid / df) if df > 0 else 1.0
    # Bayesian posterior covariance with isotropic prior; always full rank.
    cov = np.linalg.inv(X.T @ X + ridge * np.eye(X.shape[1])) * sigma2
    ident = np.linalg.norm(V_r, axis=0) > 0.999
    aliased = [labels[i] for i in range(len(labels)) if not ident[i]]
    return FitResult(
        beta,
        labels,
        cov,
        np.sqrt(np.maximum(np.diag(cov), 0.0)),
        rank,
        aliased,
        sigma2,
        len(y),
        resid,
        float(s[0] / s[rank - 1]) if rank else None,
    )
