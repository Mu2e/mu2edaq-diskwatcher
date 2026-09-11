"""Shared fixtures.

Every test gets a pristine ``Settings`` and an empty ``StateStore``, because
both are process-wide singletons that would otherwise leak between tests.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "src"))

from mu2edaq_diskwatcher.settings import reset_settings   # noqa: E402
from mu2edaq_diskwatcher.state import PEERS, STORE        # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    reset_settings()
    STORE.replace([])
    PEERS.clear()
    yield
    reset_settings()
    STORE.replace([])
    PEERS.clear()


@pytest.fixture
def settings():
    return reset_settings()


@pytest.fixture
def tmp_tree(tmp_path):
    """A directory holding an empty file, a 5 MiB file, and a subdirectory."""
    empty = tmp_path / "empty.dat"
    empty.touch()

    big = tmp_path / "big.dat"
    big.write_bytes(b"\0" * (5 * 1024 * 1024))

    subdir = tmp_path / "sub"
    subdir.mkdir()

    return {
        "root":    tmp_path,
        "empty":   empty,
        "big":     big,
        "subdir":  subdir,
        "missing": tmp_path / "does-not-exist.dat",
    }


@pytest.fixture
def client():
    """Flask test client over the shared STORE."""
    from mu2edaq_diskwatcher.web import create_app
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as test_client:
        yield test_client
