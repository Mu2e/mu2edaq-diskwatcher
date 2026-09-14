import pytest
import yaml

from mu2edaq_diskwatcher.config import (
    entries_from_config,
    load_config,
    normalise_peer_url,
    peers_from_config,
    peers_from_urls,
)


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


# ------------------------------------------------------------------- peers
def peers(text):
    """Static peers and issues only; discovery has its own tests below."""
    static, _discover, issues = peers_from_config(yaml.safe_load(text) or {})
    return static, issues


def discover(text):
    _static, d, issues = peers_from_config(yaml.safe_load(text) or {})
    return d, issues


@pytest.mark.parametrize("raw, expected", [
    ("http://node:5002",   "http://node:5002"),
    ("http://node:5002/",  "http://node:5002"),      # one instance, one spelling
    ("https://node",       "https://node"),
    ("node:5002",          "http://node:5002"),      # scheme optional
    ("node",               "http://node"),
    ("",                   None),
    ("   ",                None),
    ("ftp://node",         None),
    ("http://",            None),
    ("http://node?x=1",    None),
])
def test_peer_url_normalisation(raw, expected):
    assert normalise_peer_url(raw) == expected


def test_peer_mapping_is_parsed():
    result, issues = peers("peers:\n  - url: http://dl-01:5002\n    label: DL-01\n"
                           "    timeout: 2.5\n    enabled: false\n")
    assert issues == []
    assert result == [{"url": "http://dl-01:5002", "label": "DL-01", "timeout": 2.5,
                       "enabled": False, "config_errors": [], "source": "static"}]


def test_peer_label_defaults_to_host_and_port():
    result, _ = peers("peers:\n  - url: http://dl-01.fnal.gov:5002/\n")
    assert result[0]["label"] == "dl-01.fnal.gov:5002"


def test_bare_string_peer_is_accepted():
    result, issues = peers("peers:\n  - dl-01:5002\n")
    assert issues == []
    assert result[0]["url"] == "http://dl-01:5002"
    assert result[0]["timeout"] is None and result[0]["enabled"] is True


def test_peer_without_url_is_dropped_with_a_warning():
    result, issues = peers("peers:\n  - label: nameless\n")
    assert result == []
    assert any("no url" in i for i in issues)


def test_peer_with_bad_url_is_dropped_but_siblings_survive():
    result, issues = peers("peers:\n  - url: ftp://nope\n  - url: http://ok:1\n")
    assert [p["url"] for p in result] == ["http://ok:1"]
    assert any("not an http(s) URL" in i for i in issues)


def test_bad_peer_timeout_warns_and_uses_the_default():
    result, issues = peers("peers:\n  - url: http://a:1\n    timeout: soon\n")
    assert result[0]["timeout"] is None
    assert any("timeout" in i for i in issues)
    assert result[0]["config_errors"]                 # surfaced per peer too


def test_unknown_peer_key_warns():
    _, issues = peers("peers:\n  - url: http://a:1\n    lable: typo\n")
    assert any("lable" in i for i in issues)


def test_duplicate_peer_keeps_the_first():
    result, issues = peers("peers:\n  - url: http://a:1\n    label: first\n"
                           "  - url: http://a:1/\n    label: second\n")
    assert [p["label"] for p in result] == ["first"]
    assert any("more than once" in i for i in issues)


def test_peers_mapping_with_unknown_keys_warns_and_keeps_going():
    result, issues = peers("peers:\n  url: http://a:1\n  static: [http://b:1]\n")
    assert [p["url"] for p in result] == ["http://b:1"]
    assert any("unknown key 'url'" in i for i in issues)


def test_peers_scalar_is_rejected():
    result, issues = peers("peers: 42\n")
    assert result == []
    assert any("must be a list" in i for i in issues)


def test_no_peers_key_is_fine():
    assert peers("files: []") == ([], [])
    assert discover("files: []")[0]["enabled"] is False


# ---------------------------------------------------------------- discovery
def test_flat_list_is_shorthand_for_static_with_discovery_off():
    d, issues = discover("peers:\n  - http://a:1\n")
    assert issues == [] and d["enabled"] is False


def test_static_and_discover_sections():
    static, issues = peers("peers:\n  static:\n    - http://a:1\n  discover:\n"
                           "    filter: {host: 'mu2e-dl-*'}\n")
    assert issues == [] and [p["url"] for p in static] == ["http://a:1"]
    d, _ = discover("peers:\n  static:\n    - http://a:1\n  discover:\n"
                    "    filter: {host: 'mu2e-dl-*'}\n")
    assert d["enabled"] is True                       # writing the block means on
    assert d["filter"] == {"host": "mu2e-dl-*", "app": "diskwatcher"}   # app defaulted
    assert d["interval"] is None and d["grace"] is None and d["timeout"] == 2.0


def test_discover_block_can_be_explicitly_disabled():
    d, _ = discover("peers:\n  discover:\n    enabled: false\n    filter: {host: x}\n")
    assert d["enabled"] is False and d["filter"]["host"] == "x"


def test_discover_bare_boolean():
    assert discover("peers:\n  discover: true\n")[0]["enabled"] is True
    assert discover("peers:\n  discover: false\n")[0]["enabled"] is False


def test_discover_numbers_and_exclude_are_parsed():
    d, issues = discover("peers:\n  discover:\n    interval: 45\n    timeout: 1.5\n"
                         "    grace: 300\n    exclude: [mu2e-dl-99, 'test-*']\n")
    assert issues == []
    assert (d["interval"], d["timeout"], d["grace"]) == (45, 1.5, 300)
    assert d["exclude"] == ["mu2e-dl-99", "test-*"]


def test_discover_probe_hosts_are_parsed_and_deduplicated():
    d, issues = discover("peers:\n  discover:\n    probe: [mu2e-trk-01.fnal.gov, "
                         "'mu2e-trk-02:28999', mu2e-trk-01.fnal.gov, '', 'bad host']\n")
    assert d["probe"] == ["mu2e-trk-01.fnal.gov", "mu2e-trk-02:28999"]
    assert sum("probe:" in i for i in issues) == 2          # '' and 'bad host'
    assert discover("peers:\n  discover:\n    probe: mu2e-trk-01\n")[0]["probe"] == ["mu2e-trk-01"]
    assert discover("peers:\n  discover: {}\n")[0]["probe"] == []


def test_discover_exclude_accepts_a_single_string():
    assert discover("peers:\n  discover:\n    exclude: mu2e-dl-99\n")[0]["exclude"] == ["mu2e-dl-99"]


def test_discover_bad_numbers_fall_back_with_a_warning():
    d, issues = discover("peers:\n  discover:\n    interval: soon\n    timeout: -1\n")
    assert d["interval"] is None and d["timeout"] == 2.0
    assert sum("not a positive number" in i for i in issues) == 2


def test_discover_filter_rejects_unknown_keys_but_keeps_the_rest():
    d, issues = discover("peers:\n  discover:\n    filter: {host: 'a*', port: 5}\n")
    assert d["filter"] == {"host": "a*", "app": "diskwatcher"}
    assert any("unknown key 'port'" in i for i in issues)


def test_discover_filter_must_be_a_mapping():
    d, issues = discover("peers:\n  discover:\n    filter: 'host=a*'\n")
    assert d["filter"] == {"app": "diskwatcher"}
    assert any("filter: must be a mapping" in i for i in issues)


def test_discover_unknown_key_warns():
    _, issues = discover("peers:\n  discover:\n    intreval: 5\n")
    assert any("intreval" in i for i in issues)


def test_discover_that_is_not_a_mapping_is_disabled():
    d, issues = discover("peers:\n  discover: [a, b]\n")
    assert d["enabled"] is False
    assert any("must be a mapping or true/false" in i for i in issues)


@pytest.mark.parametrize("spec, expected", [
    ("host=mu2e-dl-*", {"host": "mu2e-dl-*", "app": "diskwatcher"}),
    ("host=a*,name=Disk*", {"host": "a*", "name": "Disk*", "app": "diskwatcher"}),
    ("app=other host=x", {"app": "other", "host": "x"}),
])
def test_parse_filter_spec(spec, expected):
    from mu2edaq_diskwatcher.config import parse_filter_spec
    assert parse_filter_spec(spec) == expected


@pytest.mark.parametrize("spec", ["host", "port=5", "host="])
def test_parse_filter_spec_rejects_bad_input(spec):
    from mu2edaq_diskwatcher.config import parse_filter_spec
    with pytest.raises(ValueError):
        parse_filter_spec(spec)


def test_peers_from_urls_matches_the_yaml_form():
    result, issues = peers_from_urls(["dl-01:5002", "http://dl-02:5002/"])
    assert issues == []
    assert [p["url"] for p in result] == ["http://dl-01:5002", "http://dl-02:5002"]
    assert result[0]["label"] == "dl-01:5002"
