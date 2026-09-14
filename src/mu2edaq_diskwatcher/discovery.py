"""Mu2e DAQ service discovery — best effort.

Advertises the HTTP web port so the app appears in ``mu2edaq-discover`` scans
and the control-room browser, and so other diskwatchers can find this one
(see :mod:`mu2edaq_diskwatcher.peers`).  ``mu2edaq-discovery`` is not on
PyPI, so a missing package must never block startup.
"""

from . import INSTANCE_ID, __version__

#: What we announce ourselves as; peers.discover filters on it by default.
APP = "diskwatcher"


def start_responder(port: int):
    """Start and return a discovery responder, or ``None`` if unavailable.

    ``meta.instance_id`` lets a diskwatcher that discovers this one tell
    whether the record is itself, before ever making an HTTP request.
    """
    try:
        from mu2edaq_discovery import Responder
        responder = Responder(name="Disk Watcher", app=APP, port=port,
                              scheme="http", version=__version__,
                              meta={"instance_id": INSTANCE_ID})
        responder.start()
        return responder
    except Exception as exc:
        print(f"[Discovery] responder not started: {exc}")
        return None


def stop_responder(responder) -> None:
    if responder is not None:
        try:
            responder.stop()
        except Exception as exc:
            print(f"[Discovery] responder stop failed: {exc}")
