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

The clonepool IS the process library.
Every intaked file with frank_usable=true is a suit.
The TAV hex IS the suit identity.
The sidecar IS the suit registration.
No bridge. No install. Intake a file — Frank can wear it.

The library has three jobs:
  1. Scan the clonepool at boot — every frank_usable sidecar becomes a suit
  2. Resolve the right suit for any stage packet
  3. Register new suits without restarting (resolve_by_hex hot-loads on demand)

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

LIBRARY_VERSION = "1.1.0"

log = logging.getLogger("process_library")

# ── SuitType string → SuitType enum ──────────────────────────
# Sidecars store suit_type as a string. We need the enum for SuitSpec.
_SUIT_TYPE_MAP = {
    "PYTHON": SuitType.PYTHON,
    "SHELL":  SuitType.SHELL,
    "BINARY": SuitType.BINARY,
    "NODE":   SuitType.NODE,
    "POWER":  SuitType.POWER,
}

# ── Family string → DataFamily constant ───────────────────────
_FAMILY_MAP = {
    "SYSTEM":  DataFamily.SYSTEM,
    "USER":    DataFamily.USER,
    "NETWORK": DataFamily.NETWORK,
    "AI":      DataFamily.AI,
    "ASSETS":  DataFamily.ASSETS,
    "PHYSICS": DataFamily.PHYSICS,
}


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

    The clonepool IS the library.
    Intake a file → sidecar written → frank_usable=true → suit registered.
    TAV hex is the suit name. No install. No bridge.

    Sections:
      CLONEPOOL — every intaked file with frank_usable=true (primary)
      SYSTEM    — OS-level suits that predate the clonepool model
    """

    # Library index lives at this slot in shared memory
    # Written as JSON so all processes can read it
    LIBRARY_INDEX_SLOT = 63   # last slot — reserved for library

    def __init__(self, frank: Optional[Frank5] = None):
        self.frank      = frank or get_frank()
        self._suits:    dict[str, LibraryEntry] = {}
        self._lock      = threading.Lock()
        self._loaded    = False

        # Clonepool — this IS the library now
        self.clonepool  = Path(os.environ.get(
            "CLONEPOOL_DIR",
            Path.home() / "Phoenix" / "clonepool"
        ))

        # Search paths kept for system suits that predate the clonepool
        self._search_paths: list[Path] = [
            Path(os.environ.get("PHOENIX_SUITS",   "/etc/systemd/system")),
            Path(os.environ.get("PHOENIX_SECTOR1", "/etc/systemd/system/SECTOR1")),
            Path(os.environ.get("PHOENIX_SECTOR2", "/etc/systemd/system/SECTOR2")),
            Path(os.environ.get("PHOENIX_SECTOR3", "/etc/systemd/system/SECTOR3")),
            Path(os.environ.get("PHOENIX_SECTOR4", "/etc/systemd/system/SECTOR4")),
            Path.home() / "projects/phoenix",
            Path.cwd(),
        ]

        log.info(f"ProcessLibrary v{LIBRARY_VERSION} initializing")

    # -------------------------------------------------------------------------
    # Boot — scan clonepool + register system suits
    # -------------------------------------------------------------------------

    def boot(self):
        """
        Boot the process library.
        Scans the clonepool for every frank_usable sidecar.
        Each one becomes a suit. The clonepool IS the library.
        Called once at Phoenix startup.
        """
        log.info("ProcessLibrary booting — scanning clonepool")
        start = time.monotonic()

        # 1. Primary — scan clonepool
        #    Every intaked file with frank_usable=true becomes a suit.
        #    TAV hex is the suit name. Sidecar is the registration.
        clonepool_count = self._scan_clonepool()

        # 2. System suits that predate the clonepool model
        #    helix_audit, integrated_guardian, syncthing, config_centralizer
        #    Once these are intaked they'll come from clonepool automatically.
        self._register_system_suits()

        # 3. Write index to shared memory
        self._write_index()

        elapsed      = (time.monotonic() - start) * 1000
        self._loaded = True

        log.info(
            f"ProcessLibrary ready — "
            f"{len(self._suits)} suits "
            f"({clonepool_count} from clonepool) "
            f"in {elapsed:.1f}ms"
        )
        self.frank._audit_record("LIBRARY_BOOT", {
            "suits":           len(self._suits),
            "clonepool_suits": clonepool_count,
            "elapsed_ms":      round(elapsed, 2),
            "version":         LIBRARY_VERSION,
        })

    # -------------------------------------------------------------------------
    # Clonepool scan — the clonepool IS the library
    # -------------------------------------------------------------------------

    def _scan_clonepool(self) -> int:
        """
        Walk CLONEPOOL_DIR. Register every frank_usable suit.
        Returns the number of suits registered from the clonepool.

        clonepool/
          [tav]/
            [tav].sidecar.json   ← read this
            v1_filename.py       ← entry points here (latest version)
            v2_filename.py
        """
        if not self.clonepool.exists():
            log.warning(
                f"Clonepool not found: {self.clonepool} "
                f"— no suits loaded from pool"
            )
            return 0

        registered = 0
        skipped    = 0
        errors     = 0

        for hex_dir in sorted(self.clonepool.iterdir()):
            if not hex_dir.is_dir():
                continue

            tav     = hex_dir.name
            sidecar = hex_dir / f"{tav}.sidecar.json"

            if not sidecar.exists():
                log.debug(f"No sidecar in {tav} — skipping")
                skipped += 1
                continue

            try:
                with open(sidecar) as f:
                    data = json.load(f)
            except Exception as e:
                log.warning(f"Sidecar read error {tav}: {e}")
                errors += 1
                continue

            # Gate — only frank_usable suits enter the library
            if not data.get("frank_usable", False):
                log.debug(
                    f"Not frank_usable: {tav} "
                    f"({data.get('original_name', '?')})"
                )
                skipped += 1
                continue

            suit_data = data.get("suit")
            if not suit_data:
                log.debug(f"frank_usable=true but suit block missing: {tav}")
                skipped += 1
                continue

            # Validate entry file exists — if stale, find latest
            entry = suit_data.get("entry", "")
            if not entry or not Path(entry).exists():
                entry = self._resolve_latest_entry(
                    hex_dir, data.get("original_name", "")
                )
                if not entry:
                    log.warning(f"No entry file found for {tav} — suit skipped")
                    skipped += 1
                    continue

            suit_type = _SUIT_TYPE_MAP.get(
                suit_data.get("suit_type", "PYTHON"), SuitType.PYTHON
            )
            family = _FAMILY_MAP.get(
                suit_data.get("family", "USER"), DataFamily.USER
            )

            spec = SuitSpec(
                name        = tav,
                suit_type   = suit_type,
                entry       = entry,
                sector      = int(suit_data.get("sector",   2)),
                ring_pos    = int(suit_data.get("ring_pos", 0)),
                family      = family,
                description = (
                    f"{data.get('original_name', tav)} — "
                    f"{data.get('filetype', 'unknown')} — "
                    f"clonepool {data.get('version', 'v?')}"
                ),
                permissions = self._default_permissions(
                    int(suit_data.get("sector", 2))
                ),
            )

            self._register(
                spec,
                tags=["clonepool", data.get("filetype", "unknown").split(":")[0]]
            )
            registered += 1
            log.debug(
                f"Suit registered: {tav} "
                f"({data.get('original_name', '?')}) "
                f"[{suit_data.get('suit_type', '?')}] "
                f"sector{spec.sector} ring{spec.ring_pos}"
            )

        log.info(
            f"Clonepool scan complete — "
            f"{registered} registered, "
            f"{skipped} skipped, "
            f"{errors} errors"
        )
        return registered

    def _resolve_latest_entry(self, hex_dir: Path, original_name: str) -> str:
        """
        Sidecar entry path is stale (file moved or evicted).
        Find the highest-versioned file in hex_dir for this name.
        Mirrors get_latest_file() logic from intake.sh.
        """
        if not original_name:
            return ""

        candidates = []
        for f in hex_dir.iterdir():
            if f.is_file() and f.name.endswith(original_name):
                try:
                    ver_str = f.name.split("_")[0].lstrip("v")
                    candidates.append((int(ver_str), str(f)))
                except (ValueError, IndexError):
                    continue

        if not candidates:
            return ""

        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # -------------------------------------------------------------------------
    # resolve_by_hex — call any suit by its TAV hex
    # -------------------------------------------------------------------------

    def resolve_by_hex(self, tav: str) -> Optional[SuitSpec]:
        """
        Resolve a suit by its TAV hex.

        Usage:
            spec = library.resolve_by_hex("abc123...")
            if spec:
                result = wear(spec, data=b"my data")

        The TAV hex IS the suit name in the library.
        If the suit was intaked after boot, hot-loads it on demand.
        No restart needed. Frank can wear anything in the clonepool.
        """
        # Fast path — already in library
        spec = self.get(tav)
        if spec:
            return spec

        # Slow path — intaked after boot, not yet registered
        hex_dir = self.clonepool / tav
        if not hex_dir.exists():
            log.warning(f"resolve_by_hex: {tav} not found in clonepool")
            return None

        before = len(self._suits)
        self._scan_clonepool_single(hex_dir, tav)
        after  = len(self._suits)

        if after > before:
            log.info(f"resolve_by_hex: hot-loaded {tav} from clonepool")
            return self.get(tav)

        log.warning(
            f"resolve_by_hex: {tav} found in clonepool "
            f"but not frank_usable"
        )
        return None

    def _scan_clonepool_single(self, hex_dir: Path, tav: str):
        """
        Register a single clonepool entry on demand.
        Called by resolve_by_hex for suits intaked after boot.
        """
        sidecar = hex_dir / f"{tav}.sidecar.json"
        if not sidecar.exists():
            return

        try:
            with open(sidecar) as f:
                data = json.load(f)
        except Exception as e:
            log.warning(f"On-demand sidecar read error {tav}: {e}")
            return

        if not data.get("frank_usable", False):
            return

        suit_data = data.get("suit")
        if not suit_data:
            return

        entry = suit_data.get("entry", "")
        if not entry or not Path(entry).exists():
            entry = self._resolve_latest_entry(
                hex_dir, data.get("original_name", "")
            )
        if not entry:
            return

        suit_type = _SUIT_TYPE_MAP.get(
            suit_data.get("suit_type", "PYTHON"), SuitType.PYTHON
        )
        family = _FAMILY_MAP.get(
            suit_data.get("family", "USER"), DataFamily.USER
        )

        spec = SuitSpec(
            name        = tav,
            suit_type   = suit_type,
            entry       = entry,
            sector      = int(suit_data.get("sector",   2)),
            ring_pos    = int(suit_data.get("ring_pos", 0)),
            family      = family,
            description = (
                f"{data.get('original_name', tav)} — "
                f"hot-loaded from clonepool"
            ),
            permissions = self._default_permissions(
                int(suit_data.get("sector", 2))
            ),
        )

        self._register(spec, tags=["clonepool", "hot_loaded"])

    # -------------------------------------------------------------------------
    # System suits — predated the clonepool model
    # Once these files are intaked they'll come from clonepool automatically.
    # -------------------------------------------------------------------------

    def _register_system_suits(self):
        """
        Register system-level suits that predate the clonepool model.
        config_centralizer, guardian, syncthing, helix_audit.
        These are Phoenix OS suits — always available.
        Once intaked into the clonepool they'll be found by _scan_clonepool
        and these hand-coded entries become redundant.
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
            # Don't overwrite a clonepool version of the same suit
            if spec.name not in self._suits:
                self._register(spec, tags=["system"])

        log.info(f"System suits registered")

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
            mod = importlib.import_module(entry_path)
            log.debug(f"Pre-loaded module: {entry_path}")
            return mod
        except ImportError:
            pass

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

            # Third — any clonepool suit for this sector
            for entry in self._suits.values():
                if (entry.spec.sector == sector and
                        "clonepool" in entry.tags):
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
        Used for system suits that predate the clonepool.
        Returns the name if not found — frank_ring handles missing suits.
        """
        for base in self._search_paths:
            for ext in ["", ".py", ".sh", ".js"]:
                candidate = base / f"{name}{ext}"
                if candidate.exists():
                    return str(candidate)

            sector_dir = base / f"SECTOR{sector}"
            for ext in ["", ".py", ".sh", ".js"]:
                candidate = sector_dir / f"{name}{ext}"
                if candidate.exists():
                    return str(candidate)

        return name

    def _detect_suit_type(self, name: str) -> SuitType:
        """Detect suit type from name/extension."""
        name_lower = name.lower()
        if name_lower.endswith(".sh"):  return SuitType.SHELL
        if name_lower.endswith(".js"):  return SuitType.NODE
        if name_lower.endswith(".ps1"): return SuitType.POWER
        if name_lower.endswith(".py"):  return SuitType.PYTHON
        if name_lower.endswith(".c"):   return SuitType.BINARY
        if "frank3" in name_lower:      return SuitType.BINARY
        return SuitType.PYTHON

    def _default_permissions(self, sector: int) -> dict:
        """Default permissions by sector."""
        if sector == 1:
            return {
                "read":      True,
                "write":     True,
                "clone":     True,
                "translate": False,
                "delete":    False,
                "kernel":    True,
            }
        if sector == 3:
            return {
                "read":      True,
                "write":     True,
                "clone":     True,
                "translate": True,
                "delete":    False,
                "kernel":    False,
            }
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
            clonepool_suits = sum(
                1 for e in self._suits.values()
                if "clonepool" in e.tags
            )
            return {
                "version":         LIBRARY_VERSION,
                "loaded":          self._loaded,
                "suit_count":      len(self._suits),
                "clonepool_suits": clonepool_suits,
                "clonepool_path":  str(self.clonepool),
                "suits": {
                    name: {
                        "sector":    e.spec.sector,
                        "ring_pos":  e.spec.ring_pos,
                        "family":    e.spec.family,
                        "type":      e.spec.suit_type.name,
                        "calls":     e.call_count,
                        "preloaded": e._mod is not None,
                        "tags":      e.tags,
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
    print("Clonepool IS the library. TAV hex IS the suit.")
    print("="*60 + "\n")

    lib = boot_library(frank)

    status = lib.status()
    print(f"Suits in closet : {status['suit_count']}")
    print(f"From clonepool  : {status['clonepool_suits']}")
    print(f"Clonepool path  : {status['clonepool_path']}")
    print()

    print(f"{'SUIT':<34} {'SECTOR':<8} {'RING':<6} {'FAMILY':<12} {'TYPE':<10} {'SRC'}")
    print("-" * 82)

    for name, info in status["suits"].items():
        src = "clonepool" if "clonepool" in info["tags"] else "system"
        print(
            f"{name:<34} "
            f"{info['sector']:<8} "
            f"{info['ring_pos']:<6} "
            f"{info['family']:<12} "
            f"{info['type']:<10} "
            f"{src}"
        )

    print("\n" + "="*60)
    print("Frank reaches in. The suit is already there.")
    print("="*60)

    frank.shutdown()
