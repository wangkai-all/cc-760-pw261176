"""Constrained mixture region: feasibility, exact-sum candidate generation.

The region is a section of a simplex:

    sum_i x_i = total,   lower_i <= x_i <= upper_i,
    plus pairwise incompatibilities and "at most one of a group" rules.

Candidates are *never* drawn from a bounding box and normalised afterwards.
They are either

* lattice points from an integer composition of ``n_steps`` (so the sum is
  exactly ``total`` by construction), or
* true vertices of the constrained polytope, obtained by solving
  ``{sum x = total}`` together with ``k-1`` active bound hyperplanes and
  keeping only solutions that satisfy every constraint.

Both routes therefore produce genuine mixture formulations.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Sequence

import numpy as np

EPS_REL = 1e-9


class RegionError(ValueError):
    """Invalid region definition. Carries machine-readable blocking entries."""

    def __init__(self, message: str, blocking: list[dict] | None = None):
        super().__init__(message)
        self.blocking = blocking or [{"kind": "region", "message": message}]


class CandidateCapExceeded(RegionError):
    def __init__(self, count: int, cap: int):
        super().__init__(
            f"候选格点超过上限（>{cap}），请增大网格步长或收紧上下限",
            [{"kind": "candidate_cap", "message": "网格过密", "count": count, "cap": cap}],
        )


@dataclass(frozen=True)
class Candidate:
    amounts: tuple[float, ...]
    counts: tuple[int, ...] | None
    source: str  # "lattice" | "vertex"

    @property
    def key(self) -> tuple[float, ...]:
        return tuple(round(v, 9) for v in self.amounts)


@dataclass
class RegionSpec:
    codes: tuple[str, ...]
    names: tuple[str, ...]
    total: float
    lower: tuple[float, ...]
    upper: tuple[float, ...]
    n_steps: int
    forbidden_pairs: tuple[tuple[int, int], ...] = ()
    forbidden_groups: tuple[tuple[int, ...], ...] = ()


@dataclass
class RegionResult:
    candidates: list[Candidate] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    blocking: list[dict] = field(default_factory=list)


def _check_total(value: float) -> float:
    total = float(value)
    if not math.isfinite(total) or total <= 0:
        raise RegionError("配方总量必须为正数")
    return total


def region_from_config(components: Sequence[dict], workspace: dict) -> RegionSpec:
    """Build and validate the region from component rows + workspace JSON."""
    if not components:
        raise RegionError("至少需要一个原料成分")
    codes = tuple(c["code"] for c in components)
    if len(set(codes)) != len(codes):
        raise RegionError("原料代码重复")
    total = _check_total(workspace.get("total", 100.0))
    lower = tuple(float(c.get("lower", 0.0)) for c in components)
    upper = tuple(float(c.get("upper", total)) for c in components)
    names = tuple(c.get("name", c["code"]) for c in components)
    n_steps = int(workspace.get("n_steps", 20))
    if n_steps < 1 or n_steps > 200:
        raise RegionError("网格份数必须在 1..200 之间")

    code_index = {c: i for i, c in enumerate(codes)}
    pairs: list[tuple[int, int]] = []
    for raw in workspace.get("forbidden_pairs", []):
        if len(raw) != 2:
            raise RegionError("禁配关系必须是两个成分")
        try:
            i, j = code_index[raw[0]], code_index[raw[1]]
        except KeyError as exc:
            raise RegionError(f"禁配关系引用了未知成分：{exc}") from None
        if i == j:
            raise RegionError("禁配关系不能把成分与自身配对")
        pairs.append(tuple(sorted((i, j))))
    groups: list[tuple[int, ...]] = []
    for raw in workspace.get("forbidden_groups", []):
        if len(raw) < 2:
            raise RegionError("互斥组至少包含两个成分")
        try:
            groups.append(tuple(sorted(code_index[c] for c in raw)))
        except KeyError:
            raise RegionError("互斥组引用了未知成分") from None

    blocking: list[dict] = []
    for i, code in enumerate(codes):
        if not (math.isfinite(lower[i]) and math.isfinite(upper[i])):
            raise RegionError(f"{code} 的上下限不是有限数")
        if lower[i] < -1e-12:
            raise RegionError(f"{code} 的下限不能为负")
        if upper[i] > total + 1e-9:
            raise RegionError(f"{code} 的上限不能超过配方总量 {total}")
        if lower[i] > upper[i] + 1e-9:
            blocking.append(
                {
                    "kind": "bounds",
                    "component": code,
                    "message": f"{code} 的下限 {lower[i]} 大于上限 {upper[i]}",
                }
            )
    if sum(lower) > total + 1e-9:
        blocking.append(
            {
                "kind": "bounds_sum",
                "message": "各成分下限之和超过配方总量，区域为空",
                "sum_lower": sum(lower),
                "total": total,
            }
        )
    if sum(upper) < total - 1e-9:
        blocking.append(
            {
                "kind": "bounds_sum",
                "message": "各成分上限之和小于配方总量，区域为空",
                "sum_upper": sum(upper),
                "total": total,
            }
        )
    if blocking:
        raise RegionError(blocking[0].get("message", "区域不可行"), blocking)

    return RegionSpec(codes, names, total, lower, upper, n_steps, tuple(pairs), tuple(groups))


def _integer_bounds(spec: RegionSpec) -> tuple[list[int], list[int]]:
    n, total = spec.n_steps, spec.total
    lo, hi = [], []
    for l, u in zip(spec.lower, spec.upper):
        lo.append(max(0, math.ceil(l / total * n - EPS_REL)))
        hi.append(min(n, math.floor(u / total * n + EPS_REL)))
    return lo, hi


def _blend_violations(
    amounts: Sequence[float], spec: RegionSpec, tol: float
) -> list[dict]:
    total = sum(amounts)
    out: list[dict] = []
    if abs(total - spec.total) > tol:
        out.append({"kind": "sum", "message": "成分总和不等于配方总量", "sum": total})
        return out
    for i, (l, u) in enumerate(zip(spec.lower, spec.upper)):
        if amounts[i] < l - tol or amounts[i] > u + tol:
            out.append(
                {
                    "kind": "bounds",
                    "component": spec.codes[i],
                    "message": f"{spec.codes[i]} 超出上下限",
                    "value": amounts[i],
                    "lower": l,
                    "upper": u,
                }
            )
    eps = spec.total * EPS_REL
    for i, j in spec.forbidden_pairs:
        if amounts[i] > eps and amounts[j] > eps:
            out.append(
                {
                    "kind": "forbidden_pair",
                    "components": [spec.codes[i], spec.codes[j]],
                    "message": f"{spec.codes[i]} 与 {spec.codes[j]} 禁止同配",
                }
            )
    for group in spec.forbidden_groups:
        present = [spec.codes[i] for i in group if amounts[i] > eps]
        if len(present) > 1:
            out.append(
                {
                    "kind": "forbidden_group",
                    "components": present,
                    "message": "互斥组内不能同时出现：" + "、".join(present),
                }
            )
    return out


def blend_violations(amounts: Sequence[float], spec: RegionSpec) -> list[dict]:
    return _blend_violations(amounts, spec, spec.total * 1e-7)


def _lattice_candidates(
    spec: RegionSpec, max_candidates: int
) -> tuple[list[Candidate], list[dict], int]:
    """Exact-sum lattice enumeration with hard candidate cap."""
    n = spec.n_steps
    k = len(spec.codes)
    lo, hi = _integer_bounds(spec)

    # Suffix sums allow pruning the recursion.
    suffix_lo = [0] * (k + 1)
    suffix_hi = [0] * (k + 1)
    for i in range(k - 1, -1, -1):
        suffix_lo[i] = suffix_lo[i + 1] + lo[i]
        suffix_hi[i] = suffix_hi[i + 1] + hi[i]

    candidates: list[Candidate] = []
    rejected: list[dict] = []
    rejected_forbidden = 0
    truncated = False

    def rec(i: int, remaining: int, counts: list[int]) -> None:
        nonlocal rejected_forbidden, truncated
        if truncated:
            return
        if i == k:
            if remaining != 0:
                return
            amounts = tuple(round(spec.total * c / n, 12) for c in counts)
            violations = _blend_violations(amounts, spec, spec.total * 1e-9)
            if violations:
                rejected_forbidden += 1
                if len(rejected) < 20:
                    rejected.extend(violations)
                return
            if len(candidates) >= max_candidates:
                truncated = True
                return
            candidates.append(Candidate(amounts, tuple(counts), "lattice"))
            return
        # remaining after this component must stay within suffix bounds
        rest_min = suffix_lo[i + 1]
        rest_max = suffix_hi[i + 1]
        c_lo = max(lo[i], remaining - rest_max)
        c_hi = min(hi[i], remaining - rest_min)
        for c in range(c_lo, c_hi + 1):
            counts.append(c)
            rec(i + 1, remaining - c, counts)
            counts.pop()
            if truncated:
                return

    rec(0, n, [])
    if truncated:
        raise CandidateCapExceeded(len(candidates) + 1, max_candidates)
    return candidates, rejected, rejected_forbidden


def _vertices(spec: RegionSpec, max_candidates: int) -> list[Candidate]:
    """Enumerate exact vertices of {sum x = total, l <= x <= u}."""
    k = len(spec.codes)
    if k == 1:
        value = spec.total
        if spec.lower[0] - 1e-9 <= value <= spec.upper[0] + 1e-9:
            return [Candidate((value,), None, "vertex")]
        return []
    total = spec.total
    bound_rows: list[tuple[int, float]] = []
    for i in range(k):
        bound_rows.append((i, spec.lower[i]))
        bound_rows.append((i, spec.upper[i]))

    found: dict[tuple[float, ...], Candidate] = {}
    tol = total * 1e-7
    # Pick k-1 bound hyperplanes; together with the sum row this fixes a point.
    for chosen in itertools.combinations(range(len(bound_rows)), k - 1):
        A = np.zeros((k, k), dtype=float)
        A[0, :] = 1.0
        b = np.zeros(k, dtype=float)
        b[0] = total
        for row, idx in enumerate(chosen, start=1):
            i, value = bound_rows[idx]
            A[row, i] = 1.0
            b[row] = value
        try:
            if np.linalg.matrix_rank(A) < k:
                continue
            x = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            continue
        if not np.all(np.isfinite(x)):
            continue
        # Snap values that are within tolerance of a bound to that bound.
        for i in range(k):
            if abs(x[i] - spec.lower[i]) <= tol:
                x[i] = spec.lower[i]
            elif abs(x[i] - spec.upper[i]) <= tol:
                x[i] = spec.upper[i]
            x[i] = min(max(x[i], spec.lower[i]), spec.upper[i])
        # Re-scale any residual to keep the sum exact (residual is ~1e-14).
        residual = total - float(x.sum())
        if abs(residual) > tol:
            continue
        x[i] = x[i] + residual
        amounts = tuple(round(float(v), 12) for v in x)
        if abs(sum(amounts) - total) > tol:
            continue
        if _blend_violations(amounts, spec, tol):
            continue
        key = tuple(round(v, 9) for v in amounts)
        if key in found:
            continue
        found[key] = Candidate(amounts, None, "vertex")
        if len(found) > max_candidates:
            raise CandidateCapExceeded(len(found), max_candidates)
    return list(found.values())


def enumerate_region(
    spec: RegionSpec, *, include_vertices: bool = True, max_candidates: int = 50_000
) -> RegionResult:
    """Generate the full feasible candidate set (lattice + vertices)."""
    lattice, rejected, rejected_forbidden = _lattice_candidates(spec, max_candidates)
    blocking: list[dict] = []
    merged: dict[tuple[float, ...], Candidate] = {c.key: c for c in lattice}
    vertex_count = 0
    if include_vertices:
        vertices = _vertices(spec, max_candidates)
        vertex_count = len(vertices)
        added = 0
        for c in vertices:
            if c.key not in merged:
                merged[c.key] = c
                added += 1
        stats_vertices_added = added
    else:
        stats_vertices_added = 0

    # Deterministic order: lexicographic by amount vector.
    candidates = sorted(merged.values(), key=lambda c: c.key)

    if not candidates:
        if rejected_forbidden:
            blocking.append(
                {
                    "kind": "forbidden",
                    "message": "禁配关系排除了全部候选配方",
                    "rejected": rejected_forbidden,
                }
            )
            blocking.extend(rejected[:6])
        else:
            blocking.append(
                {
                    "kind": "empty_region",
                    "message": "区域中没有可行配方，请检查上下限与网格步长",
                }
            )

    stats = {
        "total": spec.total,
        "n_steps": spec.n_steps,
        "lattice_count": len(lattice),
        "vertex_count": vertex_count,
        "vertices_added": stats_vertices_added,
        "candidate_count": len(candidates),
        "removed_by_forbidden": rejected_forbidden,
        "include_vertices": include_vertices,
        "max_candidates": max_candidates,
    }
    return RegionResult(candidates, stats, blocking)


def candidate_amounts_dict(spec: RegionSpec, candidate: Candidate) -> dict[str, float]:
    return {code: amount for code, amount in zip(spec.codes, candidate.amounts)}


def amounts_tuple(spec: RegionSpec, amounts: dict[str, float]) -> tuple[float, ...]:
    try:
        values = tuple(float(amounts[c]) for c in spec.codes)
    except KeyError as exc:
        raise RegionError(f"配方缺少成分 {exc}") from None
    return values


def consumption_vector(spec: RegionSpec, amounts: Sequence[float], charge_mass: float) -> tuple[float, ...]:
    return tuple(charge_mass * a / spec.total for a in amounts)


def chebyshev_distance(a: Sequence[float], b: Sequence[float]) -> float:
    return max(abs(x - y) for x, y in zip(a, b))
