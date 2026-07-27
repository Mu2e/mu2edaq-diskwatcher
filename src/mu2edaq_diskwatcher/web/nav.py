"""The one and only definition of the navigation bar.

Adding a page means adding one tuple here; ``_navbar.html`` iterates it and
marks the current ``request.endpoint`` active.  The previous single-file version
duplicated the navbar markup in three places, which had already drifted apart.
"""

#: (endpoint, label, bootstrap-icon class)
NAV_ITEMS = [
    ("views.index",   "Watcher",    "bi-hdd-stack"),
    ("views.space",   "Disk Space", "bi-pie-chart"),
    ("views.sizes",   "File Sizes", "bi-file-earmark-bar-graph"),
    ("views.config",  "Config",     "bi-gear"),
    ("views.api_docs", "API",       "bi-braces"),
    ("views.about",   "About",      "bi-info-circle"),
]
