import pytest

from mu2edaq_diskwatcher.formatting import DASH, fmt_bytes, fmt_duration, fmt_pct


@pytest.mark.parametrize("seconds, expected", [
    (None, DASH), (-1, DASH),
    (0, "0s"), (59, "59s"),
    (60, "1m 0s"), (300, "5m 0s"), (3599, "59m 59s"),
    (3600, "1h 0m 0s"), (86399, "23h 59m 59s"),
    (86400, "1d 0h 0m"), (86401, "1d 0h 0m"),
])
def test_fmt_duration(seconds, expected):
    assert fmt_duration(seconds) == expected


@pytest.mark.parametrize("value, expected", [
    (None, DASH),
    (0, "0.0 B"),
    (1023, "1023.0 B"),
    (1024, "1.0 KiB"),
    (1024 ** 2, "1.0 MiB"),
    (1024 ** 3, "1.0 GiB"),
    (1024 ** 4, "1.0 TiB"),
    (1024 ** 5, "1.0 PiB"),
    (1024 ** 6, "1.0 EiB"),
])
def test_fmt_bytes(value, expected):
    # The separator is a non-breaking space, so compare on the parts.
    assert fmt_bytes(value).split() == expected.split()


def test_fmt_bytes_uses_a_non_breaking_space():
    assert " " in fmt_bytes(1024)


def test_si_threshold_displays_as_iec():
    # 2 TB written SI is shown as 1.8 TiB — documented, and worth pinning so
    # nobody "fixes" it into a mismatch with the configured value.
    assert fmt_bytes(2 * 1000 ** 4).startswith("1.8")


@pytest.mark.parametrize("value, expected", [
    (None, DASH), (0, "0.0%"), (12.34, "12.3%"), (100, "100.0%"),
])
def test_fmt_pct(value, expected):
    assert fmt_pct(value) == expected
