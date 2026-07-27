import pytest
import yaml

from mu2edaq_diskwatcher.config import entries_from_config, load_config


def build(text, default_delay=300):
    return entries_from_config(yaml.safe_load(text) or {}, default_delay=default_delay)


def only(text, **kw):
    entries, issues = build(text, **kw)
    assert len(entries) == 1, entries
    return entries[0], issues


# ------------------------------------------------------------ delay fallback
# A silent staleness regression on a production config is the worst possible
# outcome of this feature, so every branch of the rule is pinned here.

def test_explicit_delay_wins():
    entry, _ = only("files:\n  - path: /a\n    delay: 60\n")
    assert entry["delay"] == 60


def test_absent_delay_falls_back_to_default():
    entry, _ = only("files:\n  - path: /a\n")
    assert entry["delay"] == 300


def test_absent_delay_with_size_block_disables_staleness():
    entry, _ = only("files:\n  - path: /a\n    size:\n      warning: 1MiB\n")
    assert entry["delay"] is None


def test_absent_delay_with_space_block_disables_staleness():
    entry, _ = only("paths:\n  - path: /a\n    space:\n      warning: 1GiB\n")
    assert entry["delay"] is None


def test_explicit_delay_survives_a_threshold_block():
    entry, _ = only("paths:\n  - path: /a\n    delay: 60\n"
                    "    space:\n      warning: 1GiB\n")
    assert entry["delay"] == 60


def test_explicit_null_delay():
    entry, issues = only("files:\n  - path: /a\n    delay: null\n")
    assert entry["delay"] is None
    assert any("nothing is monitored" in i for i in issues)


def test_non_numeric_delay_warns_and_uses_default():
    entry, issues = only("files:\n  - path: /a\n    delay: soon\n")
    assert entry["delay"] == 300
    assert any("not a number" in i for i in issues)


def test_bare_string_entries_get_the_default_delay():
    entries, _ = build("files:\n  - /a\n  - /b\n")
    assert [e["delay"] for e in entries] == [300, 300]
    assert [e["path"] for e in entries] == ["/a", "/b"]


def test_default_delay_is_configurable():
    entry, _ = only("files:\n  - path: /a\n", default_delay=42)
    assert entry["delay"] == 42


# ------------------------------------------------------------- kind and shape
def test_files_and_paths_map_to_kinds():
    entries, _ = build("files:\n  - path: /f\npaths:\n  - path: /d\n")
    assert {e["path"]: e["kind"] for e in entries} == \
           {"/f": "file", "/d": "directory"}


def test_label_defaults_to_path():
    entry, _ = only("files:\n  - path: /a\n")
    assert entry["label"] == "/a"


def test_entry_without_path_is_dropped_with_a_warning():
    entries, issues = build("files:\n  - label: nameless\n")
    assert entries == []
    assert any("no path" in i for i in issues)


# ------------------------------------------------------------ threshold blocks
def test_space_thresholds_are_parsed():
    entry, issues = only("paths:\n  - path: /d\n    space:\n"
                         "      warning: 20%\n      critical: 500GiB\n      full: 50GiB\n")
    assert issues == []
    assert entry["space"]["warning"].percent == 20
    assert entry["space"]["critical"].bytes == 500 * 1024 ** 3
    assert entry["space"]["full"].bytes == 50 * 1024 ** 3


def test_size_thresholds_are_parsed():
    entry, issues = only("files:\n  - path: /f\n    size:\n"
                         "      warning: 1GiB\n      critical: 4GiB\n      full: 8GiB\n")
    assert issues == []
    assert entry["size"]["warning"].bytes == 1024 ** 3


def test_max_is_a_synonym_for_full():
    entry, _ = only("files:\n  - path: /f\n    size:\n      max: 8GiB\n")
    assert entry["size"]["full"].bytes == 8 * 1024 ** 3


def test_allow_empty_is_captured():
    entry, _ = only("files:\n  - path: /f\n    size:\n      allow_empty: true\n")
    assert entry["allow_empty"] is True


def test_empty_only_size_block_is_legitimate():
    # "alarm if this file is zero bytes" needs no thresholds at all.
    entry, _ = only("files:\n  - path: /f\n    size:\n      allow_empty: false\n")
    assert entry["size"] is not None
    assert all(v is None for v in entry["size"].values())


def test_percent_in_size_block_is_rejected_but_siblings_survive():
    entry, issues = only("files:\n  - path: /f\n    size:\n"
                         "      warning: 10%\n      critical: 4GiB\n")
    assert entry["size"]["warning"] is None
    assert entry["size"]["critical"].bytes == 4 * 1024 ** 3
    assert any("not meaningful for file sizes" in i for i in issues)


def test_unparseable_threshold_keeps_the_entry_and_its_siblings():
    entry, issues = only("paths:\n  - path: /d\n    space:\n"
                         "      warning: banana\n      critical: 5GiB\n")
    assert entry["space"]["warning"] is None
    assert entry["space"]["critical"] is not None
    assert entry["config_errors"]
    assert any("banana" in i for i in issues)


def test_space_on_a_file_entry_is_rejected():
    entry, issues = only("files:\n  - path: /f\n    space:\n      warning: 1GiB\n")
    assert entry["space"] is None
    assert any("directories only" in i for i in issues)


def test_size_on_a_directory_entry_is_rejected():
    entry, issues = only("paths:\n  - path: /d\n    size:\n      warning: 1GiB\n")
    assert entry["size"] is None
    assert any("files only" in i for i in issues)


def test_empty_space_block_disables_monitoring():
    entry, issues = only("paths:\n  - path: /d\n    space: {}\n")
    assert entry["space"] is None
    assert any("empty" in i for i in issues)


def test_space_block_with_only_bad_thresholds_disables_monitoring():
    entry, issues = only("paths:\n  - path: /d\n    space:\n      warning: banana\n")
    assert entry["space"] is None
    assert any("no usable thresholds" in i for i in issues)


def test_non_mapping_threshold_block_is_rejected():
    entry, issues = only("paths:\n  - path: /d\n    space: 5GiB\n")
    assert entry["space"] is None
    assert any("must be a mapping" in i for i in issues)


# --------------------------------------------------------------- ordering
def test_misordered_space_thresholds_warn():
    _, issues = only("paths:\n  - path: /d\n    space:\n"
                     "      warning: 50GiB\n      critical: 500GiB\n")
    assert any("misordered" in i for i in issues)


def test_misordered_size_thresholds_warn():
    _, issues = only("files:\n  - path: /f\n    size:\n"
                     "      warning: 8GiB\n      critical: 1GiB\n")
    assert any("misordered" in i for i in issues)


def test_correctly_ordered_thresholds_are_quiet():
    _, issues = only("paths:\n  - path: /d\n    space:\n"
                     "      warning: 500GiB\n      critical: 50GiB\n      full: 5GiB\n")
    assert issues == []


def test_mixed_percent_and_absolute_ordering_is_deferred():
    # Ordering depends on the filesystem size, so it cannot be judged here.
    _, issues = only("paths:\n  - path: /d\n    space:\n"
                     "      warning: 1%\n      critical: 500GiB\n")
    assert not any("misordered" in i for i in issues)


# ------------------------------------------------------------- unknown keys
def test_unknown_entry_key_warns():
    _, issues = only("files:\n  - path: /f\n    dealy: 60\n")
    assert any("dealy" in i for i in issues)


def test_unknown_threshold_key_warns():
    _, issues = only("paths:\n  - path: /d\n    space:\n"
                     "      warnign: 1GiB\n      critical: 1GiB\n")
    assert any("warnign" in i for i in issues)


# ------------------------------------------------------------- load_config
def test_load_config_reads_yaml(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("watcher:\n  web_port: 1234\n")
    assert load_config(str(path))["watcher"]["web_port"] == 1234


def test_load_config_missing_is_tolerated_by_default(tmp_path):
    assert load_config(str(tmp_path / "nope.yaml")) == {}


def test_load_config_missing_exits_when_required(tmp_path):
    with pytest.raises(SystemExit):
        load_config(str(tmp_path / "nope.yaml"), required=True)


def test_empty_config_yields_no_entries():
    assert build("") == ([], [])
