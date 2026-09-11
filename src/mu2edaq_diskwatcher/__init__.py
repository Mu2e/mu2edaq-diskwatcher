"""mu2edaq-diskwatcher — file mtime, free-space and file-size monitor.

Watches configured files and directories for three independent conditions:

  * staleness   — the path has not been modified within its ``delay``
  * free space  — a directory's filesystem has dropped below a space threshold
  * file size   — a file is zero length, or has grown past a size threshold

A background daemon thread polls every entry; a Flask web application serves
three dashboards (Watcher, Disk Space, File Sizes) plus a JSON API.

Several instances can be federated: an instance configured with ``peers:``
fetches each peer's ``/api/state`` over HTTP and shows that peer's entries on
its own dashboards, grouped under the peer's connection details.
"""

import uuid
from datetime import datetime, timezone

__version__ = "1.3.0"

#: Process start time, used for the uptime shown on the About page.
START_TIME = datetime.now(timezone.utc)

#: Random identity for this process, published in ``/api/state`` and
#: ``/api/version``.  A peer whose URL resolves back to this very process is
#: recognised by it and refused, so a misconfigured ``peers:`` entry cannot
#: show the local entries a second time under a "remote" heading.
INSTANCE_ID = uuid.uuid4().hex
