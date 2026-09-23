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
CONFIGS = sorted(str(p.relative_to(REPO / "config"))
                 for p in (REPO / "config").rglob("*.yaml")
                 if not p.name.endswith(".template.yaml"))


@pytest.mark.parametrize("name", CONFIGS)
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


def test_node_configs_match_the_template_and_watch_the_six_areas():
    """Regenerating must be a no-op, and every node file must watch the areas
    the DAQ group asked for.  The template itself is not a valid config until
    its placeholders are filled, so it is excluded from CONFIGS above."""
    import subprocess
    import yaml
    nodes = REPO / "config" / "nodes"
    generated = sorted(p for p in nodes.glob("mu2e-diskwatcher-*.yaml"))
    assert len(generated) == 27, [p.name for p in generated]
    names = {p.name for p in generated}
    # Zero-padded numbering, as the nodes are actually named.
    assert "mu2e-diskwatcher-calo-01.yaml" in names and "mu2e-diskwatcher-trk-14.yaml" in names
    assert not any(n.startswith("mu2e-diskwatcher-calo-") and len(n) < len("mu2e-diskwatcher-calo-01.yaml")
                   for n in names), names
    for path in generated:
        text = path.read_text()
        assert "# GENERATED from" in text, path.name
        cfg = yaml.safe_load(text)
        assert [e["path"] for e in cfg["paths"]] == \
            ["/data", "/daqlogs", "/scratch", "/var", "/var/log", "/tmp"], path.name
        assert [e["path"] for e in cfg["files"]] == \
            ["/var/log/messages", "/var/log/secure"], path.name
        assert all(e["size"]["allow_empty"] for e in cfg["files"]), path.name
        stem = path.name[:-len(".yaml")]
        assert cfg["watcher"]["pid_file"] == f"{{run_dir}}/{stem}.pid"
        assert cfg["peers"]["discover"]["enabled"] is False
    # A dry run against the committed files must name exactly these, plus the
    # aggregator.
    out = subprocess.run(["bash", str(REPO / "tools" / "make-node-configs.sh"), "-n"],
                         capture_output=True, text=True, cwd=str(REPO), timeout=60)
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    node_lines = [l for l in lines if "(aggregator" not in l]
    assert sorted(l.split()[2] for l in node_lines) == sorted(str(p) for p in generated)
    (agg_line,) = [l for l in lines if "(aggregator" in l]
    assert agg_line.split()[2] == str(REPO / "config" / "mu2e-diskwatcher-mgr-01.yaml")
    assert "28 static peers" in agg_line


#: Disk sizes the fleet actually spans, 20 GiB (/var/log) to 15 TiB (dl-02's
#: /data), plus margin either side.
_CAPACITIES = [int(g * 2**30) for g in (10, 20, 50, 100, 250, 500, 1024, 4096, 16384, 65536)]


def _space_blocks(path):
    import yaml
    from mu2edaq_diskwatcher.config import entries_from_config
    entries, _ = entries_from_config(yaml.safe_load(path.read_text()) or {})
    return [(e["path"], e["space"]) for e in entries if e.get("space")]


@pytest.mark.parametrize("name", sorted(
    [str(p.relative_to(REPO / "config")) for p in (REPO / "config" / "nodes").glob("mu2e-diskwatcher-*.yaml")]
    + ["mu2e-diskwatcher-mgr-01.yaml"]))
def test_generated_space_thresholds_are_ordered_on_every_disk_size(name):
    """A generated config is deployed to nodes whose disks for the same path
    range from 50 GiB to 15 TiB, so its thresholds must escalate whatever the
    capacity.  Observed for real on 2026-09-23: "10% / 5% / 500 GiB" on /data
    and "15% / 5% / 100 GiB" on /scratch misordered on 56 of 175 readings and
    reported 37 empty disks as FULL."""
    from mu2edaq_diskwatcher.thresholds import check_order
    for area, limits in _space_blocks(REPO / "config" / name):
        for total in _CAPACITIES:
            resolved = {lvl: (t.resolve(total) if t is not None else None)
                        for lvl, t in limits.items()}
            assert check_order(resolved, descending=True), \
                (name, area, total // 2**30, {k: (v.raw if v else None) for k, v in limits.items()})


def test_dl_01_space_thresholds_are_ordered_on_its_own_disks():
    """The hand-written dl-01 config only has to fit dl-01.  Capacities as
    measured on 2026-09-23."""
    from mu2edaq_diskwatcher.thresholds import check_order
    measured = {"/data": 12.7 * 2**40, "/daqlogs": 5.0 * 2**40, "/scratch": 931.1 * 2**30,
                "/home": 5.0 * 2**40, "/var": 50 * 2**30, "/var/log": 20 * 2**30, "/tmp": 50 * 2**30}
    for area, limits in _space_blocks(REPO / "config" / "mu2e-diskwatcher-dl-01.yaml"):
        total = int(measured[area])
        resolved = {lvl: (t.resolve(total) if t is not None else None) for lvl, t in limits.items()}
        assert check_order(resolved, descending=True), (area, total // 2**30)


def test_aggregator_config_lists_the_whole_fleet_statically_and_by_probe():
    """mu2e-mgr-01 shows everything.  Static entries do not depend on
    multicast; the same hosts are probed by unicast because multicast on the
    DAQ network does not carry between switches; and the file is generated
    and committed so a `git pull` can never switch federation off again."""
    import yaml
    path = REPO / "config" / "mu2e-diskwatcher-mgr-01.yaml"
    text = path.read_text()
    assert "# GENERATED from" in text
    cfg = yaml.safe_load(text)
    peers = cfg["peers"]
    urls = [p["url"] for p in peers["static"]]
    assert len(urls) == 28 and len(set(urls)) == 28
    assert "http://mu2e-dl-01.fnal.gov:5002" in urls          # the hand-written node too
    assert "http://mu2e-trk-14.fnal.gov:5002" in urls
    assert not any("mgr-01" in u for u in urls)                # never itself
    d = peers["discover"]
    assert d["enabled"] is True
    assert sorted(d["probe"]) == sorted(u[len("http://"):-len(":5002")] for u in urls)
    assert cfg["watcher"]["pid_file"] == "{run_dir}/mu2e-diskwatcher-mgr-01.pid"


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
