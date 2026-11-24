import math

import pytest

from backend.app.mixture import (
    CandidateCapExceeded,
    RegionError,
    blend_violations,
    enumerate_region,
    region_from_config,
)


def comps(**bounds):
    return [
        {"code": code, "name": code, "lower": lo, "upper": hi, "unit": "g"}
        for code, (lo, hi) in bounds.items()
    ]


BASE = comps(A=(20, 60), B=(0, 50), C=(0, 40), D=(0, 15), E=(0, 10))


def spec(**region):
    return region_from_config(BASE, {"total": 100, "n_steps": 20, **region})


def test_all_candidates_satisfy_every_constraint_exactly():
    s = spec(forbidden_pairs=[["B", "E"]], forbidden_groups=[["C", "D"]])
    result = enumerate_region(s)
    assert result.candidates
    for cand in result.candidates:
        assert abs(sum(cand.amounts) - 100) < 1e-9
        assert not blend_violations(cand.amounts, s)
        for amount, lo, hi in zip(cand.amounts, s.lower, s.upper):
            assert lo - 1e-9 <= amount <= hi + 1e-9


def test_lattice_points_are_exact_multiples_of_step():
    s = spec()
    for cand in enumerate_region(s).candidates:
        if cand.counts is None:
            continue
        assert sum(cand.counts) == s.n_steps
        assert all(int(round(a * s.n_steps / s.total)) == c for a, c in zip(cand.amounts, cand.counts))


def test_candidate_set_matches_brute_force():
    """Exhaustive cross-check against an independent nested loop."""
    s = spec(forbidden_pairs=[["B", "E"]], forbidden_groups=[["C", "D"]])
    expected = 0

    def feasible(w):
        a, b, c, d, e = w
        if abs(a + b + c + d + e - 100) > 1e-9:
            return False
        if not (20 <= a <= 60 and 0 <= b <= 50 and 0 <= c <= 40 and 0 <= d <= 15 and 0 <= e <= 10):
            return False
        if b > 0 and e > 0:
            return False
        if c > 0 and d > 0:
            return False
        return True

    step = 5
    for a in range(0, 101, step):
        for b in range(0, 101 - a, step):
            for c in range(0, 101 - a - b, step):
                for d in range(0, 101 - a - b - c, step):
                    e = 100 - a - b - c - d
                    if e % step == 0 and feasible((a, b, c, d, e)):
                        expected += 1
    got = len([c for c in enumerate_region(s).candidates if c.source == "lattice"])
    assert got == expected == len(enumerate_region(s).candidates)


def test_forbidden_group_excludes_cooccurrence():
    s = spec(forbidden_groups=[["C", "D"]])
    result = enumerate_region(s)
    assert not any(c.amounts[2] > 0 and c.amounts[3] > 0 for c in result.candidates)


def test_forbidden_pair_with_positive_lower_bound_is_empty():
    comps2 = comps(A=(20, 60), B=(10, 50), C=(0, 40))
    s = region_from_config(comps2, {"total": 100, "n_steps": 10, "forbidden_pairs": [["A", "B"]]})
    res = enumerate_region(s)
    assert res.candidates == []
    assert res.blocking and res.blocking[0]["kind"] == "forbidden"


def test_lower_sum_above_total_is_rejected():
    comps2 = comps(A=(60, 90), B=(50, 90))
    with pytest.raises(RegionError) as exc:
        region_from_config(comps2, {"total": 100, "n_steps": 10})
    assert exc.value.blocking[0]["kind"] == "bounds_sum"


def test_upper_sum_below_total_is_rejected():
    comps2 = comps(A=(0, 20), B=(0, 20))
    with pytest.raises(RegionError):
        region_from_config(comps2, {"total": 100, "n_steps": 10})


def test_vertices_sit_on_bounds():
    """A vertex must have at least k-1 components sitting at a bound."""
    s = spec(forbidden_pairs=[])
    result = enumerate_region(s)
    for cand in result.candidates:
        if cand.source != "vertex":
            continue
        at_bound = sum(
            1
            for a, lo, hi in zip(cand.amounts, s.lower, s.upper)
            if math.isclose(a, lo, abs_tol=1e-7) or math.isclose(a, hi, abs_tol=1e-7)
        )
        assert at_bound >= len(s.codes) - 1
        assert abs(sum(cand.amounts) - s.total) < 1e-9


def test_candidate_cap_raises_instead_of_truncating():
    s = spec(n_steps=200)
    with pytest.raises(CandidateCapExceeded):
        enumerate_region(s, max_candidates=100)


def test_unknown_forbidden_component_rejected():
    with pytest.raises(RegionError):
        spec(forbidden_pairs=[["A", "Z"]])


def test_no_normalisation_of_box_corners():
    """Normalising box corners would produce points outside the component bounds."""
    s = spec()
    for cand in enumerate_region(s).candidates:
        assert abs(sum(cand.amounts) - 100) < 1e-9
        # A 0.2/0.5/0.3 corner of the unit box would scale to e.g. C=0 here,
        # so verify the bounds hold for every component rather than trusting
        # a post-hoc normalisation.
        for amount, lo, hi in zip(cand.amounts, s.lower, s.upper):
            assert lo - 1e-9 <= amount <= hi + 1e-9
