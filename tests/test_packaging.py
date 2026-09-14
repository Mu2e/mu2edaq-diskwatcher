"""Guards against the packaging failures that only show up after deployment."""

import importlib.metadata
import pathlib
import re

import pytest

import mu2edaq_diskwatcher
from mu2edaq_diskwatcher.web import create_app

REPO = pathlib.Path(__file__).resolve().parent.parent


def test_version_is_importable():
    assert re.fullmatch(r"\d+\.\d+\.\d+", mu2edaq_diskwatcher.__version__)


def test_installed_metadata_matches_the_source():
    try:
        installed = importlib.metadata.version("mu2edaq-diskwatcher")
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("package not installed; run `pip install -e .`")
    assert installed == mu2edaq_diskwatcher.__version__


@pytest.mark.parametrize("page", ["mu2edaq-diskwatcher.1", "mu2edaq-diskwatcher.conf.5"])
def test_man_page_version_matches_the_code(page):
    """The old man page said 1.1.0 while the code said 1.0.0."""
    path = REPO / "man" / page
    if not path.exists():
        pytest.skip(f"{page} not present")
    header = next(line for line in path.read_text().splitlines()
                  if line.startswith(".TH"))
    assert mu2edaq_diskwatcher.__version__ in header, header


def test_templates_and_static_are_reachable_from_the_package():
    """Wrong package-data breaks pip installs while source checkouts work."""
    app = create_app()
    # Both folders are recorded relative to the package directory.
    root = pathlib.Path(app.root_path)
    templates = root / app.template_folder
    static = root / app.static_folder
    assert templates.is_dir() and static.is_dir()
    for name in ("base.html", "_navbar.html", "_macros.html", "index.html",
                 "space.html", "sizes.html", "config.html", "about.html",
                 "api.html", "sitemap.html", "error.html"):
        assert (templates / name).is_file(), name
    for name in ("diskwatcher.js", "diskwatcher.css"):
        assert (static / name).is_file(), name


def test_every_nav_target_is_a_real_endpoint():
    from mu2edaq_diskwatcher.web.nav import NAV_ITEMS
    endpoints = {rule.endpoint for rule in create_app().url_map.iter_rules()}
    for endpoint, _label, _icon in NAV_ITEMS:
        assert endpoint in endpoints, endpoint


def test_entry_point_is_declared():
    try:
        scripts = importlib.metadata.distribution("mu2edaq-diskwatcher").entry_points
    except importlib.metadata.PackageNotFoundError:
        pytest.skip("package not installed")
    console = [e for e in scripts if e.group == "console_scripts"]
    assert any(e.name == "mu2edaq-diskwatcher" for e in console)


def test_shim_still_exists():
    """The control room runs `python diskwatcher.py`; that must keep working."""
    shim = REPO / "diskwatcher.py"
    assert shim.is_file()
    assert "mu2edaq_diskwatcher.cli" in shim.read_text()


# ---- shipped configuration files ----------------------------------------
# An operator edits these in place; a parse error in one stops the daemon
# from starting at all. Observed for real: uncommenting the `discover:`
# sub-block of an example while its `peers:` header stayed commented.
@pytest.mark.parametrize("name", sorted(p.name for p in (REPO / "config").glob("*.yaml")))
def test_shipped_config_parses_and_is_clean(name):
    import yaml
    from mu2edaq_diskwatcher.config import entries_from_config, peers_from_config
    cfg = yaml.safe_load((REPO / "config" / name).read_text()) or {}
    assert isinstance(cfg, dict), name
    static, discover, peer_issues = peers_from_config(cfg)
    assert peer_issues == [], (name, peer_issues)
    assert isinstance(discover, dict) and "enabled" in discover
    entries, issues = entries_from_config(cfg, default_delay=300)
    assert issues == [], (name, issues)
    assert entries or static or discover["enabled"], f"{name} watches nothing"


def test_default_config_ships_with_discovery_present_but_off():
    """The block must be live YAML, not a commented example, so turning
    discovery on is a one-value edit and never a parse error."""
    import yaml
    cfg = yaml.safe_load((REPO / "config" / "mu2edaq-diskwatcher.yaml").read_text())
    assert cfg["peers"]["discover"]["enabled"] is False
    assert cfg["peers"]["static"] == []


# ---- changelog ---------------------------------------------------------
# A changelog's failure mode is drifting out of date without anyone noticing,
# so the two things that can be checked mechanically are checked here. Whether
# an entry is *accurate* is a review question; these only catch a version bump
# that left the changelog behind.
def test_changelog_exists():
    assert (REPO / "CHANGELOG.md").is_file()


def test_current_version_appears_in_the_changelog():
    """A version bump with no changelog entry is the drift this catches."""
    text = (REPO / "CHANGELOG.md").read_text()
    version = mu2edaq_diskwatcher.__version__
    heading = next((line for line in text.splitlines()
                    if line.startswith("## ") and version in line), None)
    assert heading, f"no '## ' heading mentions {version}"


def test_every_released_changelog_heading_names_a_real_tag():
    """Guards against a heading invented for a release that never got tagged.

    Skipped outside a git checkout (a source tarball has no tags), and the
    Unreleased heading is exempt by definition.
    """
    import subprocess
    try:
        out = subprocess.run(["git", "tag"], cwd=str(REPO), capture_output=True,
                             text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        pytest.skip("git unavailable")
    if out.returncode != 0:
        pytest.skip("not a git checkout")
    tags = set(out.stdout.split())

    for line in (REPO / "CHANGELOG.md").read_text().splitlines():
        if not line.startswith("## ["):
            continue
        label = line[line.index("[") + 1:line.index("]")]
        if label == "Unreleased":
            continue
        # A heading may cover two tags that point at the same commit.
        named = [t.strip() for t in label.split("/")]
        assert any(t in tags for t in named), f"no such tag: {label}"
