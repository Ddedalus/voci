"""Registers a route on `app` from a different module than the one that
created it -- tests cross-module effect folding."""
from .app_mod import app


@app.get("/extra")
def extra_handler():
    return "extra"
