"""mu2edaq-diskwatcher — file mtime, free-space and file-size monitor.

Watches configured files and directories for three independent conditions:

  * staleness   — the path has not been modified within its ``delay``
  * free space  — a directory's filesystem has dropped below a space threshold
  * file size   — a file is zero length, or has grown past a size threshold

A background daemon thread polls every entry; a Flask web application serves
three dashboards (Watcher, Disk Space, File Sizes) plus a JSON API.
"""

from datetime import datetime, timezone

__version__ = "1.2.0"

#: Process start time, used for the uptime shown on the About page.
START_TIME = datetime.now(timezone.utc)
