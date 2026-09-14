from mu2edaq_diskwatcher.settings import (
    DEFAULT_LOG_FILE,
    DEFAULT_PID_FILE,
    ENV_PREFIX,
    Settings,
    expand_path,
    get_settings,
    path_placeholders,
    reset_settings,
    short_hostname,
)


def test_apply_ignores_none():
    """The mechanism the whole precedence chain rests on."""
    settings = reset_settings()
    settings.apply(web_port=8080)
    settings.apply(web_port=None)          # "not supplied" must not clobber
    assert settings.web_port == 8080


def test_get_settings_returns_the_same_object():
    # Consumers hold no copy, so a later apply() is visible everywhere.
    assert get_settings() is get_settings()


def test_reset_settings_mutates_rather_than_rebinding():
    original = get_settings()
    original.web_port = 9999
    fresh = reset_settings()
    assert fresh is original
    assert fresh.web_port == Settings().web_port


def test_env_overrides():
    settings = reset_settings()
    issues = settings.apply_env({
        ENV_PREFIX + "WEB_HOST":      "127.0.0.1",
        ENV_PREFIX + "WEB_PORT":      "8080",
        ENV_PREFIX + "POLL_INTERVAL": "15",
        ENV_PREFIX + "DEFAULT_DELAY": "120",
        ENV_PREFIX + "PID_FILE":      "/tmp/x.pid",
    })
    assert issues == []
    assert (settings.web_host, settings.web_port) == ("127.0.0.1", 8080)
    assert (settings.poll_interval, settings.default_delay) == (15, 120)
    assert settings.pid_file == "/tmp/x.pid"


def test_env_booleans():
    for raw, expected in [("1", True), ("true", True), ("YES", True), ("on", True),
                          ("0", False), ("false", False), ("no", False), ("", False)]:
        settings = reset_settings()
        assert settings.apply_env({ENV_PREFIX + "DAEMON": raw}) == []
        assert settings.daemon is expected


def test_bad_env_values_are_reported_not_raised():
    settings = reset_settings()
    issues = settings.apply_env({ENV_PREFIX + "WEB_PORT": "not-a-port",
                                 ENV_PREFIX + "DAEMON":   "maybe"})
    assert len(issues) == 2
    assert settings.web_port == Settings().web_port     # left at the default
    assert settings.daemon is False


def test_unset_env_leaves_settings_alone():
    settings = reset_settings(web_port=1234)
    settings.apply_env({})
    assert settings.web_port == 1234


def test_as_dict_excludes_bulk_fields():
    keys = set(reset_settings().as_dict())
    assert "web_port" in keys and "peer_timeout" in keys
    assert "entries" not in keys and "config_issues" not in keys
    assert "peers" not in keys


# ------------------------------------------------------------------- peers
def test_env_peers_accept_commas_and_whitespace():
    settings = reset_settings()
    issues = settings.apply_env({ENV_PREFIX + "PEERS":
                                 "http://a:5002, b:5002 https://c/"})
    assert issues == []
    assert [p["url"] for p in settings.peers] == \
           ["http://a:5002", "http://b:5002", "https://c"]


def test_env_peers_replace_the_config_files_list():
    settings = reset_settings()
    settings.peers = [{"url": "http://from-yaml:1", "label": "y", "timeout": None,
                       "enabled": True, "config_errors": []}]
    settings.apply_env({ENV_PREFIX + "PEERS": "http://from-env:1"})
    assert [p["url"] for p in settings.peers] == ["http://from-env:1"]


def test_bad_env_peer_is_reported_and_the_variable_ignored():
    settings = reset_settings()
    issues = settings.apply_env({ENV_PREFIX + "PEERS": "http://ok:1 ftp://bad"})
    assert len(issues) == 1 and "ftp://bad" in issues[0]
    assert settings.peers == []          # all-or-nothing, like every other override


def test_env_discover_overrides_fold_into_the_discover_block():
    settings = reset_settings(poll_interval=20)
    assert settings.apply_env({ENV_PREFIX + "DISCOVER_PEERS": "yes",
                               ENV_PREFIX + "DISCOVER_FILTER": "host=mu2e-dl-*"}) == []
    d = settings.resolve_discover()
    assert d["enabled"] is True
    assert d["filter"] == {"host": "mu2e-dl-*", "app": "diskwatcher"}
    assert d["interval"] == 20 and d["grace"] == 60        # defaults filled


def test_env_discover_off_beats_a_yaml_block_that_is_on():
    settings = reset_settings()
    settings.discover["enabled"] = True
    settings.apply_env({ENV_PREFIX + "DISCOVER_PEERS": "0"})
    assert settings.resolve_discover()["enabled"] is False


def test_bad_env_discover_filter_is_reported_and_ignored():
    settings = reset_settings()
    issues = settings.apply_env({ENV_PREFIX + "DISCOVER_FILTER": "port=5"})
    assert len(issues) == 1 and "port" in issues[0]
    assert settings.discover_filter is None


def test_as_dict_carries_discover_but_not_the_override_slots():
    keys = set(reset_settings().as_dict())
    assert "discover" in keys
    assert "discover_peers" not in keys and "discover_filter" not in keys


def test_env_peer_timeout_and_interval():
    settings = reset_settings()
    assert settings.apply_env({ENV_PREFIX + "PEER_TIMEOUT": "2.5",
                               ENV_PREFIX + "PEER_INTERVAL": "60"}) == []
    assert settings.peer_timeout == 2.5 and settings.peer_interval == 60


# ------------------------------------------------------- per-node run paths
# The checkout is shared over NFS between DAQ nodes, so anything a process
# writes must be keyed by node.  These pin the placeholder mechanism the pid
# file, log and future state all rely on.

def test_short_hostname_has_no_domain_and_is_never_empty():
    node = short_hostname()
    assert node and "." not in node


def test_placeholders_cover_the_documented_set():
    values = path_placeholders(5010, run_dir="run/x")
    assert set(values) == {"host", "hostname", "port", "user", "run_dir"}
    assert values["port"] == "5010" and values["run_dir"] == "run/x"
    assert "run_dir" not in path_placeholders(5010)      # absent while resolving run_dir


def test_expand_path_substitutes_known_and_keeps_unknown():
    out = expand_path("/x/{host}-{port}/{nope}/{run_dir}", {"host": "n1", "port": "5"})
    assert out == "/x/n1-5/{nope}/{run_dir}"           # unknown stays visible
    assert expand_path(None, {"host": "n1"}) is None


def test_resolve_paths_keys_everything_by_node_in_daemon_mode():
    settings = reset_settings(daemon=True, web_port=5010)
    settings.resolve_paths()
    node = short_hostname()
    assert settings.run_dir == f"run/{node}"
    assert settings.pid_file == f"run/{node}/mu2edaq-diskwatcher.pid"
    assert settings.log_file == f"run/{node}/mu2edaq-diskwatcher.log"


def test_resolve_paths_leaves_foreground_runs_without_pid_or_log():
    """Foreground behaviour is unchanged: terminal output, no pid file."""
    settings = reset_settings()
    settings.resolve_paths()
    assert settings.pid_file is None and settings.log_file is None
    assert settings.run_dir == f"run/{short_hostname()}"


def test_resolve_paths_expands_run_dir_first_then_uses_it():
    settings = reset_settings(run_dir="/var/tmp/dw-{host}-{port}",
                              pid_file="{run_dir}/p.pid",
                              log_file="{run_dir}/{user}.log", web_port=7)
    values = settings.resolve_paths()
    node = short_hostname()
    assert settings.run_dir == f"/var/tmp/dw-{node}-7"
    assert settings.pid_file == f"/var/tmp/dw-{node}-7/p.pid"
    assert settings.log_file == f"/var/tmp/dw-{node}-7/{values['user']}.log"


def test_explicit_pid_and_log_files_are_respected_verbatim():
    settings = reset_settings(daemon=True, pid_file="/tmp/a.pid", log_file="/tmp/a.log")
    settings.resolve_paths()
    assert (settings.pid_file, settings.log_file) == ("/tmp/a.pid", "/tmp/a.log")


def test_defaults_are_inside_the_run_dir():
    assert DEFAULT_PID_FILE.startswith("{run_dir}/")
    assert DEFAULT_LOG_FILE.startswith("{run_dir}/")


def test_env_run_dir_override():
    settings = reset_settings()
    assert settings.apply_env({ENV_PREFIX + "RUN_DIR": "/scratch/{host}"}) == []
    assert settings.run_dir == "/scratch/{host}"


def test_peer_interval_falls_back_to_the_poll_interval():
    settings = reset_settings(poll_interval=45)
    assert settings.effective_peer_interval() == 45
    settings.peer_interval = 10
    assert settings.effective_peer_interval() == 10
