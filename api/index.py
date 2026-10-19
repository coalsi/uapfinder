"""Vercel Python Function: the SKY-NET HTTP API (``/api/*``).

``vercel.json`` rewrites every ``/api/...`` request to this function. The
original path is what reaches the ASGI app, so the FastAPI routes match
exactly as in standalone mode.

The ``server/`` and ``common/`` packages are imported from the repository
rather than installed: Vercel bundles the project's files (minus
``excludeFiles``) next to this entrypoint, and putting their directories on
``sys.path`` avoids depending on how the builder converts local path
requirements. ``requirements.txt`` at the repository root lists only their
third-party dependencies.

Vercel sets ``VERCEL=1``, which selects ``SKYNET_MODE=vercel`` (Redis state,
synchronous database writes, no UDP). A configuration error is reported on
every request instead of crashing the function, so it shows up in
``/api/v1/health`` while setting up a deployment.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _package_dir in (_ROOT / "server", _ROOT / "common"):
    if str(_package_dir) not in sys.path:
        sys.path.insert(0, str(_package_dir))

from fastapi import FastAPI  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from skynet_server.app import create_app  # noqa: E402
from skynet_server.config import ConfigError  # noqa: E402

try:
    app = create_app()
except ConfigError as _exc:
    _message = f"server misconfigured: {_exc}"
    app = FastAPI()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"])
    async def misconfigured(path: str) -> JSONResponse:
        return JSONResponse({"detail": _message}, status_code=500)
