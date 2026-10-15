#!/usr/bin/env python3
"""Retired name of the Firebase docs loader, kept so existing commands work.

Runs ``parse-llms-txt.py`` with ``--source firebase``. Use
``parse-llms-txt.py <command> ... --source firebase`` directly. See
``_shim.py``.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from _shim import run  # noqa: E402

if __name__ == "__main__":
    run("firebase")
