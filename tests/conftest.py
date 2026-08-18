"""Let pure client tests import submodules without executing HA's package setup."""

from __future__ import annotations

import sys
import types
from pathlib import Path

PACKAGE_NAME = "custom_components.ufanet_intercom"
if PACKAGE_NAME not in sys.modules:
    package = types.ModuleType(PACKAGE_NAME)
    package.__path__ = [
        str(
            Path(__file__).resolve().parents[1]
            / "custom_components"
            / "ufanet_intercom"
        )
    ]
    sys.modules[PACKAGE_NAME] = package
