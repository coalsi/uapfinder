"""``python -m skynet_server`` is the ``skynet-server`` command."""

import sys

from .cli import main

sys.exit(main())
