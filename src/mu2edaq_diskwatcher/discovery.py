"""Mu2e DAQ service discovery — best effort.

Advertises the HTTP web port so the app appears in ``mu2edaq-discover`` scans
and the control-room browser.  ``mu2edaq-discovery`` is not on PyPI, so a
missing package must never block startup.
"""


def start_responder(port: int):
    """Start and return a discovery responder, or ``None`` if unavailable."""
    try:
        from mu2edaq_discovery import Responder
        responder = Responder(name="Disk Watcher", app="diskwatcher",
                              port=port, scheme="http")
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
