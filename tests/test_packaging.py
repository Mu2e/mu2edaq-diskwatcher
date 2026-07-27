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
