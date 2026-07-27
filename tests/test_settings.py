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
    assert "web_port" in keys
    assert "entries" not in keys and "config_issues" not in keys
