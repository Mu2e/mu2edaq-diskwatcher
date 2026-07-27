import pytest

from mu2edaq_diskwatcher.thresholds import (
    SEVERITY,
    evaluate_size_state,
    evaluate_space_state,
    severity_rank,
    watch_rank,
)

TOTAL = 1000
SPACE = {"warning": 300, "critical": 200, "full": 100}
SIZE  = {"warning": 100, "critical": 200, "full": 300}


def space(free, limits=None):
    return evaluate_space_state(free, TOTAL, SPACE if limits is None else limits)[0]


def size(value, limits=None, allow_empty=False):
    return evaluate_size_state(value, SIZE if limits is None else limits,
                               allow_empty=allow_empty)[0]


# ---------------------------------------------------------------- free space
@pytest.mark.parametrize("free, expected", [
    (900, "GOOD"), (301, "GOOD"),
    (300, "WARNING"), (250, "WARNING"), (201, "WARNING"),
    (200, "CRITICAL"), (150, "CRITICAL"), (101, "CRITICAL"),
    (100, "FULL"), (50, "FULL"), (0, "FULL"),
])
def test_space_bands_and_inclusive_boundaries(free, expected):
    # Each threshold is inclusive on the bad side: free == warning is WARNING.
    assert space(free) == expected


def test_space_unknown_without_measurements():
    assert evaluate_space_state(None, TOTAL, SPACE)[0] == "UNKNOWN"
    assert evaluate_space_state(500, None, SPACE)[0] == "UNKNOWN"
    assert evaluate_space_state(500, 0, SPACE)[0] == "UNKNOWN"


@pytest.mark.parametrize("limits, free, expected", [
    ({"full": 100}, 50, "FULL"),
    ({"full": 100}, 500, "GOOD"),
    ({"warning": 300}, 250, "WARNING"),
    ({}, 1, "GOOD"),
])
def test_space_threshold_subsets(limits, free, expected):
    assert space(free, limits) == expected


def test_space_misordered_still_defined():
    # Nonsense config (full > warning): every band must still return a state.
    bad = {"warning": 100, "critical": 200, "full": 300}
    for free in (500, 300, 250, 150, 50):
        assert space(free, bad) in SEVERITY


def test_space_reason_names_the_threshold_that_fired():
    state, trigger, reason = evaluate_space_state(150, TOTAL, SPACE)
    assert (state, trigger) == ("CRITICAL", "critical")
    assert "critical" in reason
    # A GOOD reading names the limit it cleared — the largest one.
    _, trigger, reason = evaluate_space_state(900, TOTAL, SPACE)
    assert trigger is None
    assert "warning" in reason


# ---------------------------------------------------------------- file size
@pytest.mark.parametrize("value, expected", [
    (1, "GOOD"), (99, "GOOD"),
    (100, "WARNING"), (150, "WARNING"), (199, "WARNING"),
    (200, "CRITICAL"), (299, "CRITICAL"),
    (300, "FULL"), (5000, "FULL"),
])
def test_size_bands_and_inclusive_boundaries(value, expected):
    assert size(value) == expected


def test_zero_length_is_empty():
    assert size(0) == "EMPTY"
    state, trigger, reason = evaluate_size_state(0, SIZE)
    assert (trigger, reason) == ("empty", "file is zero length")


def test_allow_empty_suppresses_the_empty_alarm():
    assert size(0, allow_empty=True) == "GOOD"


def test_empty_beats_thresholds():
    # A zero-length file is EMPTY even with no thresholds configured at all.
    assert size(0, limits={}) == "EMPTY"


def test_size_unknown_without_measurement():
    assert size(None) == "UNKNOWN"


@pytest.mark.parametrize("limits, value, expected", [
    ({"full": 300}, 500, "FULL"),
    ({"full": 300}, 10, "GOOD"),
    ({"warning": 100}, 100, "WARNING"),
    ({}, 5, "GOOD"),
])
def test_size_threshold_subsets(limits, value, expected):
    assert size(value, limits) == expected


def test_size_reason_names_the_threshold_that_fired():
    state, trigger, reason = evaluate_size_state(250, SIZE)
    assert (state, trigger) == ("CRITICAL", "critical")
    assert "critical" in reason
    # A GOOD reading names the limit it stayed under — the smallest one.
    _, _, reason = evaluate_size_state(10, SIZE)
    assert "warning" in reason


# ---------------------------------------------------------------- ranking
def test_severity_is_monotonic():
    order = ["GOOD", "EMPTY", "WARNING", "CRITICAL", "FULL", "UNKNOWN", "MISSING"]
    ranks = [severity_rank(name) for name in order]
    assert ranks == sorted(ranks)
    assert len(set(ranks)) == len(ranks)


def test_severity_rank_edge_cases():
    assert severity_rank(None) is None
    assert severity_rank("good") == severity_rank("GOOD")
    assert severity_rank("nonsense") == severity_rank("UNKNOWN")


def test_watch_rank_ordering():
    assert watch_rank("ok") < watch_rank("unmonitored") < \
           watch_rank("stale") < watch_rank("missing")
