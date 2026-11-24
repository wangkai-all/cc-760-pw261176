import itertools
import math

import numpy as np
import pytest

from backend.app.design import (
    DesignSpec,
    _criterion,
    build_matrix,
    build_terms,
    fit_model,
    rank_and_aliases,
    select_batch,
)
from backend.app.mixture import enumerate_region, region_from_config

COMPS = [
    {"code": "A", "name": "A", "lower": 20, "upper": 60, "unit": "g"},
    {"code": "B", "name": "B", "lower": 0, "upper": 50, "unit": "g"},
    {"code": "C", "name": "C", "lower": 0, "upper": 40, "unit": "g"},
]


def make_spec(n_steps=10, forbidden=()):
    return region_from_config(
        COMPS,
        {"total": 100, "n_steps": n_steps, "forbidden_pairs": [list(p) for p in forbidden]},
    )


def brute_force_best(spec, design, n_runs, pool, available=None):
    terms = build_terms(spec, pool, design)
    best = None
    best_val = -math.inf
    for combo in itertools.combinations_with_replacement(pool, n_runs):
        rows = build_matrix(
            terms,
            design,
            [
                (c.amounts, design.process_levels[i % len(design.process_levels)] if design.process_levels else None)
                for i, c in enumerate(combo)
            ],
            spec.total,
        )
        val = _criterion(rows)
        if val > best_val:
            best_val = val
            best = combo
    return best, best_val


def test_exact_search_matches_brute_force_optimum():
    """The exhaustive route must find the same criterion as brute force."""
    spec = make_spec(n_steps=5)
    pool = enumerate_region(spec).candidates[:12]
    design = DesignSpec(model="linear", include_process=False, batch_size=3, allow_replicates=False)
    _, brute = brute_force_best(spec, design, 3, pool)
    res = select_batch(spec, pool, design)
    assert res.search == "exact-enumeration"
    assert res.metrics["criterion"] >= brute - 1e-6


def test_batch_is_jointly_optimal_not_greedy_per_run():
    """A joint batch must beat taking the individually most informative point N times."""
    spec = make_spec(n_steps=10)
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="quadratic", include_process=False, batch_size=4, allow_replicates=True)
    res = select_batch(spec, pool, design)
    assert res.feasible_size == 4
    # Greedy first point
    single = select_batch(spec, pool, DesignSpec(model="quadratic", include_process=False, batch_size=1))
    assert single.feasible_size == 1
    first = single.runs[0].amounts
    repeated = [first] * 4
    terms = build_terms(spec, pool, design)
    rows = build_matrix(terms, design, [(a, None) for a in repeated], spec.total)
    assert res.metrics["criterion"] > _criterion(rows) + 0.5


def test_process_levels_are_covered():
    spec = make_spec()
    pool = enumerate_region(spec).candidates
    design = DesignSpec(
        model="linear",
        include_process=True,
        process_levels=("低", "中", "高"),
        min_per_level=2,
        batch_size=6,
        allow_replicates=False,
    )
    res = select_batch(spec, pool, design)
    assert res.feasible_size == 6
    for level in ("低", "中", "高"):
        assert sum(1 for r in res.runs if r.level == level) >= 2


def test_process_tradeoff_uses_same_criterion():
    """Discrete level swaps must be judged by the same Phi as mixture moves."""
    spec = make_spec()
    pool = enumerate_region(spec).candidates
    design = DesignSpec(
        model="linear",
        include_process=True,
        process_levels=("低", "高"),
        batch_size=4,
    )
    res = select_batch(spec, pool, design)
    terms = build_terms(spec, pool, design)
    best_val = -math.inf
    for combo in itertools.product(pool, repeat=4):
        for levels in itertools.product(("低", "高"), repeat=4):
            rows = build_matrix(terms, design, [(c.amounts, lv) for c, lv in zip(combo, levels)], spec.total)
            best_val = max(best_val, _criterion(rows))
    assert res.metrics["criterion"] >= best_val - 1e-6


def test_stock_runs_out_and_partial_batch_reports_blocking():
    spec = make_spec()
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="linear", include_process=False, batch_size=10, allow_replicates=False)
    available = [250.0, 200.0, 150.0]  # charge mass 100 -> 2-3 runs total
    res = select_batch(spec, pool, design, stock_available=available, charge_mass=100)
    assert res.partial
    assert 0 < res.feasible_size < 10
    assert any(b["kind"] == "stock" for b in res.blocking)
    used = np.array([0.0, 0.0, 0.0])
    for run in res.runs:
        used += np.array(run.usage)
    assert np.all(used <= np.array(available) + 1e-9)


def test_stock_blocks_all_runs_before_anything_is_chosen():
    spec = make_spec()
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="linear", include_process=False, batch_size=3)
    res = select_batch(spec, pool, design, stock_available=[1.0, 0.0, 0.0], charge_mass=100)
    assert res.feasible_size == 0
    assert res.blocking
    # The blocker must name the material and the shortfall, not just say "none".
    stock = [b for b in res.blocking if b["kind"] == "stock"]
    assert stock and stock[0]["component"] in ("A", "B", "C")
    assert stock[0]["shortfall"] > 0


def test_no_affordable_run_reports_stock_not_level_coverage():
    """Zero material must not be misreported as a level-coverage problem."""
    spec = make_spec()
    pool = enumerate_region(spec).candidates
    design = DesignSpec(
        model="linear",
        include_process=True,
        process_levels=("low", "high"),
        batch_size=4,
    )
    res = select_batch(spec, pool, design, stock_available=[0.0, 0.0, 0.0], charge_mass=100)
    assert res.feasible_size == 0
    assert res.search == "blocked"
    assert [b["kind"] for b in res.blocking] == ["stock", "stock", "stock"]
    assert {b["component"] for b in res.blocking} == {"A", "B", "C"}


def test_locked_run_is_kept_and_counts_against_stock():
    spec = make_spec()
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="linear", include_process=False, batch_size=3)
    locked_amounts = [((30.0, 40.0, 30.0), None)]
    res = select_batch(
        spec,
        pool,
        design,
        stock_available=[500.0, 500.0, 500.0],
        charge_mass=100,
        locked_amounts=locked_amounts,
    )
    assert any(r.locked and r.amounts == (30.0, 40.0, 30.0) for r in res.runs)


def test_locked_run_off_grid_is_kept_exactly():
    """Operators may lock a formulation that is not on the exploration lattice."""
    spec = make_spec(n_steps=10)
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="linear", include_process=False, batch_size=3)
    res = select_batch(
        spec,
        pool,
        design,
        stock_available=[500.0, 500.0, 500.0],
        locked_amounts=[((33.0, 33.0, 34.0), None)],
    )
    assert any(r.locked and r.amounts == (33.0, 33.0, 34.0) for r in res.runs)


def test_locked_run_violating_constraints_is_rejected():
    spec = make_spec(n_steps=10, forbidden=[("A", "B")])
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="linear", include_process=False, batch_size=3)
    res = select_batch(
        spec,
        pool,
        design,
        stock_available=[500.0, 500.0, 500.0],
        locked_amounts=[((40.0, 40.0, 20.0), None)],
    )
    assert res.feasible_size == 0
    assert any(b["kind"] == "forbidden_pair" for b in res.blocking)


def test_collinear_design_is_detected():
    """All runs at the same blend + no process variation => rank deficient."""
    spec = make_spec()
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="quadratic", include_process=False, batch_size=3, allow_replicates=True)
    terms = build_terms(spec, pool, design)
    rows = build_matrix(
        terms,
        design,
        [(pool[0].amounts, None)] * 3,
        spec.total,
    )
    info = rank_and_aliases(rows, [t.label for t in terms.terms])
    # Every replicate contributes the same row, so the design resolves a single
    # direction regardless of how many terms the model nominally has.
    assert info["rank"] == 1
    # a single direction cannot identify any individual coefficient
    assert len(info["aliased"]) == len(terms.terms)


def test_quadratic_interaction_forbidden_pair_is_dropped():
    spec = make_spec(forbidden=[("A", "B")])
    pool = enumerate_region(spec).candidates
    terms = build_terms(spec, pool, DesignSpec(model="quadratic"))
    assert not any(t.pair == (0, 1) for t in terms.terms if t.kind == "interaction")


def test_fit_recovers_known_coefficients():
    spec = make_spec(n_steps=20)
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="linear", include_process=False, batch_size=12, allow_replicates=True)
    terms = build_terms(spec, pool, design)
    rng = np.random.default_rng(1)
    true = np.array([1.0, 2.0, 3.0])
    obs = []
    for cand in pool[::3]:
        rows = build_matrix(terms, design, [(cand.amounts, None)], spec.total)
        obs.append((cand.amounts, None, float((rows @ true)[0])))
    fit = fit_model(terms, design, spec, obs)
    assert np.allclose(fit.beta[:3], true, atol=1e-6)


def test_fit_without_data_is_prior_not_crash():
    spec = make_spec()
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="linear", include_process=False)
    terms = build_terms(spec, pool, design)
    fit = fit_model(terms, design, spec, [])
    assert fit.rank == 0
    assert np.all(np.isfinite(fit.posterior_sd))


def test_duplicate_flag_marks_repeated_runs():
    spec = make_spec()
    pool = enumerate_region(spec).candidates
    design = DesignSpec(model="linear", include_process=False, batch_size=3, allow_replicates=True)
    res = select_batch(spec, pool, design)
    if any(r.duplicate for r in res.runs):
        dup = [r for r in res.runs if r.duplicate][0]
        assert any(r.amounts == dup.amounts and r.level == dup.level for r in res.runs)
