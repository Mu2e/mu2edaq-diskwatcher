import pytest

from mu2edaq_diskwatcher.thresholds import (
    Threshold,
    ThresholdError,
    check_order,
    parse_size_threshold,
)


@pytest.mark.parametrize("text, expected", [
    ("500 GiB",  500 * 1024 ** 3),
    ("2TB",      2 * 1000 ** 4),
    ("1.5 gb",   1_500_000_000),
    ("8 MiB",    8 * 1024 ** 2),
    ("  8 MiB ", 8 * 1024 ** 2),
    ("1024",     1024),
    ("1024B",    1024),
    ("0",        0),
    ("512K",     512_000),
])
def test_absolute_sizes(text, expected):
    assert parse_size_threshold(text).bytes == expected


@pytest.mark.parametrize("text", ["gib", "GIB", "GiB", "giB"])
def test_units_are_case_insensitive(text):
    assert parse_size_threshold("3" + text).bytes == 3 * 1024 ** 3


def test_si_and_iec_differ():
    # The distinction that makes "2TB renders as 1.8 TiB" true.
    assert parse_size_threshold("1TB").bytes != parse_size_threshold("1TiB").bytes


@pytest.mark.parametrize("value, expected", [(1024, 1024), (2048.0, 2048), (1.5, 2)])
def test_bare_numbers_are_bytes(value, expected):
    assert parse_size_threshold(value).bytes == expected


@pytest.mark.parametrize("text, percent", [("10%", 10.0), ("5 %", 5.0),
                                           ("0.5%", 0.5), ("100%", 100.0)])
def test_percentages(text, percent):
    parsed = parse_size_threshold(text)
    assert parsed.percent == percent
    assert parsed.bytes is None


@pytest.mark.parametrize("value", [
    "", "   ", "abc", "10 XB", "GiB", -5, "-5GiB", "0%", "150%", "-1%",
    None, True, [], {},
])
def test_rejects_nonsense(value):
    with pytest.raises(ThresholdError):
        parse_size_threshold(value)


def test_percent_rejected_when_not_allowed():
    with pytest.raises(ThresholdError, match="not meaningful for file sizes"):
        parse_size_threshold("10%", allow_percent=False)

    # ...but an absolute size is still fine in the same mode.
    assert parse_size_threshold("1 GiB", allow_percent=False).bytes == 1024 ** 3


def test_resolve():
    absolute = parse_size_threshold("1 GiB")
    assert absolute.resolve(None) == 1024 ** 3        # no total needed
    assert absolute.resolve(999) == 1024 ** 3

    relative = parse_size_threshold("10%")
    assert relative.resolve(1000) == 100
    assert relative.resolve(None) is None             # unresolvable, not zero
    assert relative.resolve(0) is None


def test_raw_is_preserved_for_display():
    assert parse_size_threshold("  2TB ").raw == "2TB"
    assert parse_size_threshold("10%").raw == "10%"


def test_describe_shows_resolved_percent():
    assert parse_size_threshold("1 GiB").describe(None) == "1 GiB"
    assert "10%" in parse_size_threshold("10%").describe(1024 ** 3)
    assert "MiB" in parse_size_threshold("10%").describe(1024 ** 3)


def test_check_order():
    # descending = free space: warning > critical > full
    assert check_order({"warning": 100, "critical": 50, "full": 10}, descending=True)
    assert not check_order({"warning": 10, "critical": 50, "full": 100}, descending=True)
    # ascending = file size
    assert check_order({"warning": 10, "critical": 50, "full": 100}, descending=False)
    assert not check_order({"warning": 100, "critical": 50, "full": 10}, descending=False)
    # a partially configured block is checked on the levels it does have
    assert check_order({"warning": 100, "critical": None, "full": 10}, descending=True)
    assert check_order({"warning": None, "critical": None, "full": 10}, descending=True)
    assert check_order({}, descending=True)


def test_threshold_is_hashable_and_frozen():
    threshold = Threshold(raw="1 GiB", bytes=1024 ** 3)
    assert hash(threshold)
    with pytest.raises(Exception):
        threshold.bytes = 5
