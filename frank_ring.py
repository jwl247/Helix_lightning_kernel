#!/usr/bin/env python3
"""
frank_ring.py — Fixed & Polished Frank Ring
Phoenix DevOps OS | jwl247 | GPL v3
"""

import os
import sys
import time
import logging
import importlib
import importlib.util
import subprocess
import threading
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Any
from enum import IntEnum

from franken5 import (
    Frank5, get_frank, RingRecord, RingState, Ball, PCS, 
    DataFamily, FRANK_VERSION
)

RING_VERSION = "1.1.0-fixed"

log = logging.getLogger("frank_ring")


class SuitType(IntEnum):
    PYTHON = 0
    SHELL = 1
    BINARY = 2
    NODE = 3
    POWER = 4


# Sector definitions
SECTOR_MAP = {
    1: {"name": "Boot/Kernel", "rings": {0: "frank3_slot_a", 1: "frank3_slot_b", 2: "phoenix_auth", 3: "concierge"}},
    2: {"name": "Intake/Package", "rings": {0: "intake", 1: "clone_pool", 2: "propagator", 3: "packages_worker"}},
    3: {"name": "Comms/Network", "rings": {0: "romeo", 1: "juliet", 2: "dbl_juliet", 3: "quadengine"}},
    4: {"name": "Core Engine", "rings": {0: "helix", 1: "freewheeling", 2: "propcoms", 3: "conductor"}},
}


@dataclass
class SuitSpec:
    name: str
    suit_type: SuitType
    entry: str
    sector: int
    ring_pos: int
    family: str = DataFamily.SYSTEM
    permissions: dict = field(default_factory=dict)
    args: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    timeout: float = 30.0
    description: str = ""


def suit_for(sector: int, ring_pos: int, suit_type: SuitType = SuitType.PYTHON, 
             entry: str = "") -> SuitSpec:
    """Create a SuitSpec - fixes missing function error"""
    name = SECTOR_MAP.get(sector, {}).get("rings", {}).get(ring_pos, f"suit_s{sector}r{ring_pos}")
    return SuitSpec(
        name=name,
        suit_type=suit_type,
        entry=entry or name,
        sector=sector,
        ring_pos=ring_pos,
        family=DataFamily.SYSTEM,
        permissions={"read": True, "write": True, "clone": True, "translate": False, "delete": False, "kernel": False}
    )


def wear(suit: SuitSpec, data: bytes = b"", channel: int = 1) -> Any:
    """Convenience function"""
    ring = FrankRing(suit)
    return ring.ride(data=data, channel=channel)


class FrankRing:
    def __init__(self, suit: SuitSpec, frank: Optional[Frank5] = None):
        self.suit = suit
        self.frank = frank or get_frank()
        self.rec: Optional[RingRecord] = None
        self._result: Any = None
        self._lock = threading.Lock()

    def mount(self, channel: int = 1, data: bytes = b"") -> RingRecord:
        self.rec = self.frank.spawn_ring(
            process_name=self.suit.name,
            channel=channel,
            family=self.suit.family,
            permissions=self.suit.permissions,
            sector=self.suit.sector,
            ring_pos=self.suit.ring_pos,
        )
        if self.rec and self.rec.ball:
            self.rec.ball.hand_off("frank5_core", self.suit.name)
        return self.rec

    def run(self, data: bytes = b"", **kwargs) -> Any:
        if not self.rec or self.rec.state == RingState.DEAD:
            raise RuntimeError(f"Ring {self.rec.ring_id if self.rec else 'N/A'} is dead")

        self.frank.mark_running(self.rec.ring_id, os.getpid())
        self.rec.call2(data or self.suit.name.encode())

        try:
            if self.suit.suit_type == SuitType.PYTHON:
                self._result = self._run_python(data, **kwargs)
            else:
                self._result = self._run_subprocess(data)
            return self._result
        except Exception as e:
            log.error(f"Ring {self.rec.ring_id} execution failed: {e}")
            raise

    def sync(self, final_data: bytes = b"") -> bool:
        if not self.rec:
            return False
        self.frank.mark_syncing(self.rec.ring_id)
        payload = final_data or json.dumps({"result": str(self._result)}).encode()
        self.rec.call3(payload)
        return bool(getattr(self.rec.pcs, 'definitive', False))

    def die(self):
        if not self.rec:
            return
        self.rec.died = time.monotonic()
        self.rec.state = RingState.DONE
        self.frank.bus.write_ring_state(self.rec.ring_id, RingState.DONE)
        custody = self.rec.to_custody_record()
        self.frank._commit_custody(custody)
        log.info(f"Ring {self.rec.ring_id} terminated cleanly")

    def ride(self, data: bytes = b"", channel: int = 1, **kwargs) -> Any:
        """Main entry point: mount → run → sync → die"""
        try:
            self.mount(channel=channel, data=data)
            if not self.rec or self.rec.state == RingState.DEAD:
                return None
            result = self.run(data=data, **kwargs)
            self.sync(final_data=data)
            return result
        finally:
            self.die()

    def _run_python(self, data: bytes, **kwargs):
        entry = self.suit.entry
        try:
            mod = importlib.import_module(entry)
        except ImportError:
            spec = importlib.util.spec_from_file_location(self.suit.name, entry)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            else:
                raise

        if hasattr(mod, "run"):
            return mod.run(data, self.rec.ball, self.rec.pcs, **kwargs)
        elif hasattr(mod, "main"):
            return mod.main()
        else:
            return None

    def _run_subprocess(self, data: bytes) -> bytes:
        env = os.environ.copy()
        env.update(self.suit.env)

        if self.rec and self.rec.ball:
            env["FRANK_BALL_FAMILY"] = str(self.rec.ball.family)
            env["FRANK_BALL_SLOT"] = str(int(self.rec.ball.slot))
        if self.rec and self.rec.pcs:
            env["FRANK_PCS"] = self.rec.pcs.string()
            env["FRANK_RING_ID"] = str(self.rec.ring_id)

        cmd = [self.suit.entry] if isinstance(self.suit.entry, str) else self.suit.entry
        if self.suit.args:
            cmd += self.suit.args

        result = subprocess.run(
            cmd,
            input=data,
            capture_output=True,
            timeout=self.suit.timeout,
            env=env,
        )
        return result.stdout

    def _snap_clone(self):
        log.info(f"[Snap Clone] Triggered for ring {self.rec.ring_id if self.rec else 'N/A'}")
