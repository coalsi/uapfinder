"""File locations of the appliance (docs/NODE_APPLIANCE.md "Software on the node").

The three roots can be moved with environment variables so that tests (and
a development checkout) never touch the real system directories:
``SKYNET_ETC_DIR`` (default ``/etc/skynet``), ``SKYNET_STATE_DIR``
(``/var/lib/skynet``) and ``SKYNET_RUN_DIR`` (``/run/skynet``, a tmpfs created
by systemd-tmpfiles, shared by the node, the setup page and star calibration).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional


@dataclass(frozen=True)
class Paths:
    etc: Path = Path("/etc/skynet")
    state: Path = Path("/var/lib/skynet")
    run: Path = Path("/run/skynet")

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Paths":
        env = os.environ if env is None else env
        return cls(Path(env.get("SKYNET_ETC_DIR") or "/etc/skynet"),
                   Path(env.get("SKYNET_STATE_DIR") or "/var/lib/skynet"),
                   Path(env.get("SKYNET_RUN_DIR") or "/run/skynet"))

    # /etc/skynet
    @property
    def edge_yaml(self) -> Path:
        return self.etc / "edge.yaml"

    @property
    def local_yaml(self) -> Path:
        return self.etc / "edge.local.yaml"

    @property
    def defaults_yaml(self) -> Path:
        return self.etc / "defaults.yaml"

    # /var/lib/skynet
    @property
    def credentials(self) -> Path:
        return self.state / "credentials.json"

    @property
    def calibration(self) -> Path:
        return self.state / "calibration.json"

    @property
    def setup_dir(self) -> Path:
        """Setup-page state: device password hash, settings overrides (0700)."""
        return self.state / "setup"

    @property
    def clips(self) -> Path:
        return self.state / "clips"

    # /run/skynet
    @property
    def detect_json(self) -> Path:
        return self.run / "detect.json"

    @property
    def node_json(self) -> Path:
        return self.run / "node.json"

    @property
    def preview_jpg(self) -> Path:
        return self.run / "preview.jpg"

    @property
    def preview_request(self) -> Path:
        return self.run / "preview.request"

    @property
    def stack_request(self) -> Path:
        return self.run / "stack.request"

    @property
    def stack_npy(self) -> Path:
        return self.run / "stack.npy"

    @property
    def stack_json(self) -> Path:
        return self.run / "stack.json"

    @property
    def admin_spool(self) -> Path:
        """Requests from the setup page to the root ``uapfinder-admin.service``."""
        return self.run / "admin"

    @property
    def activity(self) -> Path:
        """Touched by the setup page on every request (the hotspot logic reads it)."""
        return self.run / "setup.activity"
