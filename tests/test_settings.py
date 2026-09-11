from mu2edaq_diskwatcher.settings import ENV_PREFIX, Settings, get_settings, reset_settings


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


def test_env_peer_timeout_and_interval():
    settings = reset_settings()
    assert settings.apply_env({ENV_PREFIX + "PEER_TIMEOUT": "2.5",
                               ENV_PREFIX + "PEER_INTERVAL": "60"}) == []
    assert settings.peer_timeout == 2.5 and settings.peer_interval == 60


def test_peer_interval_falls_back_to_the_poll_interval():
    settings = reset_settings(poll_interval=45)
    assert settings.effective_peer_interval() == 45
    settings.peer_interval = 10
    assert settings.effective_peer_interval() == 10
