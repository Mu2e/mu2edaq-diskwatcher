"""Flask application factory.

``Flask(__name__)`` resolves ``templates/`` and ``static/`` relative to this
package, so the app behaves identically when run from a source checkout and
when pip-installed (given the ``package-data`` entries in pyproject.toml).
"""

from flask import Flask, render_template

from .. import __version__
from .nav import NAV_ITEMS


def create_app() -> Flask:
    app = Flask(__name__)

    from . import api, views
    app.register_blueprint(views.bp)
    app.register_blueprint(api.bp)

    @app.context_processor
    def inject_globals():
        return {"nav_items": NAV_ITEMS, "version": __version__}

    @app.errorhandler(404)
    def not_found(_exc):
        return render_template("error.html", title="Not Found", code=404,
                               message="Page not found"), 404

    return app
