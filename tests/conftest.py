"""Stub out homeassistant modules so geometric helpers can be tested standalone."""
import sys
from unittest.mock import MagicMock

# Build minimal stubs for every homeassistant sub-module the component imports.
_HA_MODULES = [
    "homeassistant",
    "homeassistant.components",
    "homeassistant.components.binary_sensor",
    "homeassistant.config_entries",
    "homeassistant.core",
    "homeassistant.const",
    "homeassistant.data_entry_flow",
    "homeassistant.helpers",
    "homeassistant.helpers.entity",
    "homeassistant.helpers.event",
    "homeassistant.helpers.selector",
]

for _mod in _HA_MODULES:
    sys.modules.setdefault(_mod, MagicMock())

# Provide a real BinarySensorEntity base class so PolyZoneBinarySensor can
# inherit from it without error.
class _BinarySensorEntity:
    pass

sys.modules["homeassistant.components.binary_sensor"].BinarySensorEntity = _BinarySensorEntity

# EntityCategory needs to be an enum-like object with a DIAGNOSTIC attribute.
class _EntityCategory:
    DIAGNOSTIC = "diagnostic"

sys.modules["homeassistant.helpers.entity"].EntityCategory = _EntityCategory
sys.modules["homeassistant.helpers.entity"].DeviceInfo = dict
