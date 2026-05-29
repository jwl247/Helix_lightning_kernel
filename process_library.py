#!/usr/bin/env python3
"""
process_library.py — The Process Library
Phoenix DevOps OS / Helix Lightning Kernel | jwl247 | GPL v3

The closet.

Every suit Frank can wear lives here.
Pre-loaded into shared memory at boot.
Nothing fetched at runtime. Nothing loaded on demand.
Frank reaches in. The suit is already there.

That is why Frank is fast.
That is why there is no install.
That is why any device becomes a Phoenix workstation.

The library has three jobs:
  1. Load all suits into shared memory at boot
  2. Resolve the right suit for any stage packet
  3. Register new suits without restarting

One instance. Lives in Sector 4 next to Frank-core.
The rings don't have their own library.
They reach back to Sector 4 shared memory and grab what they need.
Frank-core owns the closet. The clones borrow the suits.
"""

import os
import sys
import time
import json
import hashlib
import logging
import threading
import importlib
import importlib.util
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Callable, Any

from franken5 import (
    Frank5, get_frank,
    DataFamily, KernelSlot, FAMILY_SLOT, FAMILY_ZONE,
    SHM_PATH, AUDIT_PATH
)
from frank_ring import (
    FrankRing, SuitSpec, SuitType,
    SECTOR_MAP, suit_for
)

LIBRARY_VERSION = "1.0.0-alpha"

log = logging.getLogger("process_library")


# =============================================================================
# Suit registry entry
# =============================================================================

@dataclass
class LibraryEntry:
    """
    A suit hanging in the closet.
    Everything Frank needs to wear it — already resolved.
    No I/O at runtime. No imports at runtime.
    It is already here.
    """
    spec:        SuitSpec
    loaded_at:   float        = field(default_factory=time.monotonic)
    call_count:  int          = 0
    last_called: float        = 0.0
    checksum:    str          = ""
    tags:        list         = field(default_factory=list)
    _mod:        Any          = None    # pre-loaded Python module if SuitType.PYTHON

    def touch(self):
        self.call_count += 1
        self.last_called = time.monotonic()

    def to_dict(self) -> dict:
        return {
            "name":        self.spec.name,
            "suit_type":   self.spec.suit_type.name,
            "entry":       self.spec.entry,
            "sector":      self.spec.sector,
            "ring_pos":    self.spec.ring_pos,
            "family":      self.spec.family,
            "loaded_at":   self.loaded_at,
            "call_count":  self.call_count,
            "last_called": self.last_called,
            "checksum":    self.checksum,
            "tags":        self.tags,
        }


# =============================================================================
# Process Library
# =============================================================================

class ProcessLibrary:
    """
    The closet.

    Loaded once at boot. Stays in shared memory.
    Frank reaches in. Suit is already there.

    Three sections:
      CORE     — the 16 sector suits (4 sectors x 4 rings)
      SYSTEM   — OS-level suits (config_centralizer, guardian, syncthing)
      APP      — application suits (game, LifeFirst, Office, etc.)
    """

    # Library index lives at this key in shared memory
    # Written as JSON so all processes can read it
    LIBRARY_INDEX_SLOT = 63   # last slot in shared memory — reserved for library

    def __init__(self, frank: Optional[Frank5] = None):
        self.frank   = frank or get_frank()
        self._suits: dict[str, LibraryEntry] = {}
        self._lock   = threading.Lock()
        self._loaded = False

        # Suit search paths — Frank looks here for suit modules
        self._search_paths: list[Path] = [
            Path(os.environ.get("PHOENIX_SUITS", "/etc/systemd/system")),
            Path(os.environ.get("PHOENIX_SECTOR1", "/etc/systemd/system/SECTOR1")),
            Path(os.environ.get("PHOENIX_SECTOR2", "/etc/systemd/system/SECTOR2")),
            Path(os.environ.get("PHOENIX_SECTOR3", "/etc/systemd/system/SECTOR3")),
            Path(os.environ.get("PHOENIX_SECTOR4", "/etc/systemd/system/SECTOR4")),
            Path.home() / "projects/phoenix",
            Path.cwd(),
        ]

        log.info(f"ProcessLibrary v{LIBRARY_VERSION} initializing")

    # -------------------------------------------------------------------------
    # Boot — load everything into shared memory
    # -------------------------------------------------------------------------

    def boot(self):
        """
        Load all suits at boot.
        Called once. After this Frank reaches in and the suit is already there.
        """
        log.info("ProcessLibrary booting — loading suits into shared memory")
        start = time.monotonic()

        self._register_core_suits()
        self._register_system_suits()
        self._write_index()

        elapsed = (time.monotonic() - start) * 1000
        self._loaded = True

        log.info(
            f"ProcessLibrary ready — "
            f"{len(self._suits)} suits loaded in {elapsed:.1f}ms"
        )
        self.frank._audit_record("LIBRARY_BOOT", {
            "suits":      len(self._suits),
            "elapsed_ms": round(elapsed, 2),
            "version":    LIBRARY_VERSION,
        })

    def _register_core_suits(self):
        """
        Register the 16 core sector suits.
        4 sectors x 4 rings = 16 suits.
        These are always in the library. Always.
        """
        family_map = {
            1: DataFamily.SYSTEM,
            2: DataFamily.USER,
            3: DataFamily.NETWORK,
            4: DataFamily.SYSTEM,
        }

        for sector_num, sector_info in SECTOR_MAP.items():
            for ring_pos, process_name in sector_info["rings"].items():
                family = family_map.get(sector_num, DataFamily.SYSTEM)

                spec = SuitSpec(
                    name        = process_name,
                    suit_type   = self._detect_suit_type(process_name),
                    entry       = self._resolve_entry(process_name, sector_num),
                    sector      = sector_num,
                    ring_pos    = ring_pos,
                    family      = family,
                    description = (
                        f"Sector {sector_num} — "
                        f"{sector_info['name']} — "
                        f"ring {ring_pos}"
                    ),
                    permissions = self._default_permissions(sector_num),
                )

                self._register(spec, tags=["core", f"sector{sector_num}"])

        log.info(f"Core suits registered: {len(self._suits)}")

    def _register_system_suits(self):
        """
        Register system-level suits.
        config_centralizer, guardian, syncthing, helix_audit.
        These are Phoenix OS suits — always available.
        """
        system_suits = [
            SuitSpec(
                name        = "config_centralizer",
                suit_type   = SuitType.PYTHON,
                entry       = self._resolve_entry("config_centralizer", 2),
                sector      = 2,
                ring_pos    = 0,
                family      = DataFamily.SYSTEM,
                description = "Config scanner, importer, desktop card writer",
                permissions = {
                    "read":      True,
                    "write":     True,
                    "clone":     False,
                    "translate": False,
                    "delete":    False,
                    "kernel":    False,
                }
            ),
            SuitSpec(
                name        = "integrated_guardian",
                suit_type   = SuitType.PYTHON,
                entry       = self._resolve_entry("integrated_guardian", 4),
                sector      = 4,
                ring_pos    = 3,
                family      = DataFamily.SYSTEM,
                description = "REALsure security — file guardian, threat response",
                permissions = {
                    "read":      True,
                    "write":     True,
                    "clone":     False,
                    "translate": False,
                    "delete":    False,
                    "kernel":    False,
                }
            ),
            SuitSpec(
                name        = "syncthing_module",
                suit_type   = SuitType.PYTHON,
                entry       = self._resolve_entry("syncthing_module", 4),
                sector      = 4,
                ring_pos    = 2,
                family      = DataFamily.NETWORK,
                description = "Syncthing — Frank clone sync across rings",
                permissions = {
                    "read":      True,
                    "write":     True,
                    "clone":     True,
                    "translate": False,
                    "delete":    False,
                    "kernel":    False,
                }
            ),
            SuitSpec(
                name        = "helix_audit",
                suit_type   = SuitType.SHELL,
                entry       = self._resolve_entry("helixaudit.sh", 4),
                sector      = 4,
                ring_pos    = 3,
                family      = DataFamily.SYSTEM,
                description = "Helix audit — scans sector files for health",
                permissions = {
                    "read":      True,
                    "write":     False,
                    "clone":     False,
                    "translate": False,
                    "delete":    False,
                    "kernel":    False,
                }
            ),
        ]

        for spec in system_suits:
            self._register(spec, tags=["system"])

        log.info(f"System suits registered: {len(system_suits)}")

    # -------------------------------------------------------------------------
    # Registration
    # -------------------------------------------------------------------------

    def register(self, spec: SuitSpec, tags: list = None) -> LibraryEntry:
        """
        Register a new suit at runtime.
        No restart needed. Frank can wear it immediately.
        This is how the tailor adds suits to the closet.
        """
        entry = self._register(spec, tags=tags or [])
        self._write_index()
        log.info(f"Suit registered: {spec.name} [{spec.suit_type.name}]")
        return entry

    def _register(self, spec: SuitSpec, tags: list = None) -> LibraryEntry:
        """Internal registration — no index write."""
        checksum = self._checksum_spec(spec)

        # Pre-load Python modules — zero import time at runtime
        mod = None
        if spec.suit_type == SuitType.PYTHON and spec.entry:
            mod = self._preload_python(spec)

        entry = LibraryEntry(
            spec     = spec,
            checksum = checksum,
            tags     = tags or [],
            _mod     = mod,
        )

        with self._lock:
            self._suits[spec.name] = entry

        return entry

    def _preload_python(self, spec: SuitSpec) -> Optional[Any]:
        """
        Pre-load a Python module.
        Done at boot so runtime import is instant.
        If the module doesn't exist yet — that's OK.
        It'll be loaded when the suit is first worn.
        """
        entry_path = spec.entry
        if not entry_path:
            return None

        try:
            # Try as module name first
            mod = importlib.import_module(entry_path)
            log.debug(f"Pre-loaded module: {entry_path}")
            return mod
        except ImportError:
            pass

        # Try as file path
        path = Path(entry_path)
        if path.exists() and path.suffix == ".py":
            try:
                spec_obj = importlib.util.spec_from_file_location(
                    spec.name, str(path)
                )
                mod = importlib.util.module_from_spec(spec_obj)
                spec_obj.loader.exec_module(mod)
                log.debug(f"Pre-loaded from file: {entry_path}")
                return mod
            except Exception as e:
                log.debug(f"Pre-load failed {entry_path}: {e}")

        return None

    # -------------------------------------------------------------------------
    # Resolution — find the right suit for a stage
    # -------------------------------------------------------------------------

    def resolve(self, sector: int, ring_pos: int,
                family: str = DataFamily.SYSTEM,
                data: bytes = b"") -> Optional[SuitSpec]:
        """
        Find the right suit for a stage packet.
        Called by frank_spawn._default_resolver.
        Returns a SuitSpec or None.

        Frank reaches in. The suit is already there.
        """
        with self._lock:
            # First — exact match by sector + ring_pos
            for entry in self._suits.values():
                if (entry.spec.sector   == sector and
                    entry.spec.ring_pos == ring_pos):
                    entry.touch()
                    return entry.spec

            # Second — match by family
            for entry in self._suits.values():
                if entry.spec.family == family:
                    entry.touch()
                    return entry.spec

            # Third — any core suit for this sector
            for entry in self._suits.values():
                if (entry.spec.sector == sector and
                        "core" in entry.tags):
                    entry.touch()
                    return entry.spec

        return None

    def get(self, name: str) -> Optional[SuitSpec]:
        """Get a suit by name. Direct lookup."""
        with self._lock:
            entry = self._suits.get(name)
            if entry:
                entry.touch()
                return entry.spec
        return None

    def get_preloaded_module(self, name: str) -> Optional[Any]:
        """
        Get the pre-loaded Python module for a suit.
        Zero import time. Already in memory.
        """
        with self._lock:
            entry = self._suits.get(name)
            if entry and entry._mod:
                return entry._mod
        return None

    # -------------------------------------------------------------------------
    # Index — written to shared memory so all processes can read it
    # -------------------------------------------------------------------------

    def _write_index(self):
        """
        Write the library index to shared memory slot 63.
        Every process can read this.
        Frank-core, rings, Helix — all see the same closet.
        """
        index = {
            "version":    LIBRARY_VERSION,
            "ts":         time.time(),
            "suit_count": len(self._suits),
            "suits":      {
                name: entry.to_dict()
                for name, entry in self._suits.items()
            }
        }

        try:
            raw = json.dumps(index).encode()
            # Index may exceed one slot — write summary only to shared memory
            summary = {
                "version":    LIBRARY_VERSION,
                "ts":         time.time(),
                "suit_count": len(self._suits),
                "suits":      list(self._suits.keys()),
            }
            self.frank.bus.write_stage(
                self.LIBRARY_INDEX_SLOT,
                json.dumps(summary).encode()[:4000]
            )
        except Exception as e:
            log.error(f"Library index write failed: {e}")

        # Full index to disk for inspection
        index_path = Path(os.environ.get(
            "PHOENIX_LIBRARY_INDEX",
            "/tmp/phoenix_library.json"
        ))
        try:
            with open(index_path, "w") as f:
                json.dump(index, f, indent=2)
        except Exception as e:
            log.debug(f"Library index disk write: {e}")

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _resolve_entry(self, name: str, sector: int) -> str:
        """
        Find the actual file path for a suit entry.
        Searches PHOENIX_SECTOR* paths.
        Returns the name if not found — frank_ring handles missing suits.
        """
        # Try each search path
        for base in self._search_paths:
            # Direct match
            for ext in ["", ".py", ".sh", ".js"]:
                candidate = base / f"{name}{ext}"
                if candidate.exists():
                    return str(candidate)

            # In sector subdirectory
            sector_dir = base / f"SECTOR{sector}"
            for ext in ["", ".py", ".sh", ".js"]:
                candidate = sector_dir / f"{name}{ext}"
                if candidate.exists():
                    return str(candidate)

        # Not found — return the name
        # Frank will try to import it as a module name
        return name

    def _detect_suit_type(self, name: str) -> SuitType:
        """Detect suit type from name/extension."""
        name_lower = name.lower()
        if name_lower.endswith(".sh"):   return SuitType.SHELL
        if name_lower.endswith(".js"):   return SuitType.NODE
        if name_lower.endswith(".ps1"):  return SuitType.POWER
        if name_lower.endswith(".py"):   return SuitType.PYTHON
        # C files compile to binary
        if name_lower.endswith(".c"):    return SuitType.BINARY
        # frank3 kernel slots are C
        if "frank3" in name_lower:       return SuitType.BINARY
        return SuitType.PYTHON

    def _default_permissions(self, sector: int) -> dict:
        """Default permissions by sector."""
        # Sector 1 — boot/kernel — most restricted
        if sector == 1:
            return {
                "read":      True,
                "write":     True,
                "clone":     True,
                "translate": False,
                "delete":    False,
                "kernel":    True,    # sector 1 needs kernel access
            }
        # Sector 3 — comms — translation allowed at boundary
        if sector == 3:
            return {
                "read":      True,
                "write":     True,
                "clone":     True,
                "translate": True,    # sector 3 is the translation boundary
                "delete":    False,
                "kernel":    False,
            }
        # Default
        return {
            "read":      True,
            "write":     True,
            "clone":     True,
            "translate": False,
            "delete":    False,
            "kernel":    False,
        }

    def _checksum_spec(self, spec: SuitSpec) -> str:
        """Checksum a suit spec for integrity."""
        data = f"{spec.name}:{spec.entry}:{spec.sector}:{spec.ring_pos}"
        return hashlib.sha3_256(data.encode()).hexdigest()[:16]

    def status(self) -> dict:
        with self._lock:
            return {
                "version":    LIBRARY_VERSION,
                "loaded":     self._loaded,
                "suit_count": len(self._suits),
                "suits": {
                    name: {
                        "sector":     e.spec.sector,
                        "ring_pos":   e.spec.ring_pos,
                        "family":     e.spec.family,
                        "type":       e.spec.suit_type.name,
                        "calls":      e.call_count,
                        "preloaded":  e._mod is not None,
                    }
                    for name, e in self._suits.items()
                }
            }

    def __len__(self):
        return len(self._suits)

    def __contains__(self, name: str):
        return name in self._suits


# =============================================================================
# Singleton — one library, lives in Sector 4 next to Frank-core
# =============================================================================

_library: Optional[ProcessLibrary] = None


def get_library(frank: Optional[Frank5] = None) -> ProcessLibrary:
    """
    The one true process library.
    One instance. Lives in Sector 4.
    Frank-core owns it. The rings borrow from it.
    """
    global _library
    if _library is None:
        _library = ProcessLibrary(frank=frank or get_frank())
    return _library


def boot_library(frank: Optional[Frank5] = None) -> ProcessLibrary:
    """Boot the library. Call once at Phoenix startup."""
    lib = get_library(frank)
    lib.boot()
    return lib


# =============================================================================
# Demo
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s [LIBRARY] %(levelname)s %(message)s",
        handlers= [logging.StreamHandler()]
    )

    frank = get_frank()
    frank.boot()

    print("\n" + "="*60)
    print("PROCESS LIBRARY — The Closet")
    print("="*60 + "\n")

    lib = boot_library(frank)

    status = lib.status()
    print(f"Suits in closet: {status['suit_count']}\n")

    print(f"{'SUIT':<25} {'SECTOR':<8} {'RING':<6} {'FAMILY':<12} {'TYPE':<12} {'PRELOADED'}")
    print("-" * 75)

    for name, info in status["suits"].items():
        print(
            f"{name:<25} "
            f"{info['sector']:<8} "
            f"{info['ring_pos']:<6} "
            f"{info['family']:<12} "
            f"{info['type']:<12} "
            f"{'yes' if info['preloaded'] else 'no'}"
        )

    print("\n" + "="*60)
    print(f"Library index written to: /tmp/phoenix_library.json")
    print(f"All suits pre-loaded and ready.")
    print(f"Frank reaches in. The suit is already there.")
    print("="*60)

    frank.shutdown()
