#!/usr/bin/env python3
"""Public facade for historical Claude task discovery."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

SCRIPT_ROOT = Path(__file__).resolve().parent
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import historical_discovery_capabilities as _capabilities
import historical_discovery_core as _core

# Session observation needs the canonical plugin spelling supplied by the
# capability inventory shard. Keeping the binding explicit avoids a circular
# import while preserving the original late-bound lookup.
_core._canonical_plugin_observations = _capabilities._canonical_plugin_observations

_SHARDS = (_core, _capabilities)
_OWNERS: dict[str, tuple[types.ModuleType, ...]] = {}
for _shard in _SHARDS:
    for _name in _shard.__all__:
        globals()[_name] = getattr(_shard, _name)
        _OWNERS[_name] = tuple(owner for owner in _SHARDS if _name in owner.__dict__)

__all__ = tuple(dict.fromkeys(name for shard in _SHARDS for name in shard.__all__))


class _FacadeModule(types.ModuleType):
    """Mirror patched facade attributes into every shard that resolves them."""

    def __setattr__(self, name: str, value: Any) -> None:
        for owner in _OWNERS.get(name, ()):
            setattr(owner, name, value)
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        for owner in _OWNERS.get(name, ()):
            if name in owner.__dict__:
                delattr(owner, name)
        super().__delattr__(name)


sys.modules[__name__].__class__ = _FacadeModule
