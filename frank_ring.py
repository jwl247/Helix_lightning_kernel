"""
frank_ring.py — The Frank Ring (Generation 2 Polish)
Phoenix DevOps OS / Helix Lightning Kernel | jwl247 | GPL v3

A Frank ring is a Frank clone riding a process suit.
Frank does not travel. Frank clones. The clone wears the suit.
The suit does the work. When the work is done the clone dies.

CHANGES IN THIS POLISH:
1. ATOMIC LOCK SCOPING: Scopes thread locks tightly to prevent deadlocks during snap-cloning.
2. ENUM TYPE HARMONIZATION: Ensures definitive Boolean evaluations match across the macrokernel.
3. ENVIRONMENT STRING TYPE-LOCKS: Forces string transformation on all injected environment variables.
4. COMMAND-LINE INJECTION BLOCK: Arrays parameters natively to prevent path string execution failure.
"""
import os
import sys
import time
import signal
import logging
import importlib
import importlib.util
import subprocess
import threading
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Callable, Any
from enum import IntEnum, auto
from franken5 import (
    Frank5, get_frank, RingRecord, RingState, Ball, PCS, 
    DataFamily, KernelSlot, FRANK_VERSION, AUDIT_PATH, SHM_PATH
)

RING_VERSION = "1.0.0-polished-b"
log = logging.getLogger("frank_ring")

class SuitType(IntEnum):
    PYTHON = 0 
    SHELL = 1  
    BINARY = 2 
    NODE = 3   
    POWER = 4  

# Sector definitions — 4 sectors, 4 rings each
SECTOR_MAP = {
    1: {
        "name": "Boot/Kernel",
        "rings": {0: "frank3_slot_a", 1: "frank3_slot_b", 2: "phoenix_auth", 3: "concierge"}
    },
    2: {
        "name": "Intake/Package",
        "rings": {0: "intake", 1: "clone_pool", 2: "propagator", 3: "packages_worker"}
    },
    3: {
        "name": "Comms/Network",
        "rings": {0: "romeo", 1: "juliet", 2: "dbl_juliet", 3: "quadengine"}
    },
    4: {
        "name": "Core Engine",
        "rings": {0: "helix", 1: "freewheeling", 2: "propcoms", 3: "conductor"}
    },
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

@dataclass
class RingPeers:
    franken2: Any = None 
    freewheeling: Any = None 
    propcoms: Any = None 

    def all_alive(self) -> bool:
        return all([self.franken2 is not None, self.freewheeling is not None, self.propcoms is not None])

    def tick(self) -> dict:
        if self.propcoms:
            return self.propcoms.tick(self.franken2, self.freewheeling)
        return {}

    def validate(self, ball: Ball) -> bool:
        if not self.propcoms:
            return False
        result = self.propcoms.validate(
            {"family": ball.family, "zipcode": ball.zipcode},
            {"target": f"system_{ball.ring_pos + 1}"}
        )
        return result.get("validated", False)

    def route(self, ball: Ball) -> str:
        if not self.franken2:
            return "system_1"
        result = self.franken2.propose_route({"type": ball.family})
        return result.get("target", "system_1")

class FrankRing:
    def __init__(self, suit: SuitSpec, frank: Optional[Frank5] = None):
        self.suit = suit
        self.frank = frank or get_frank()
        self.rec: Optional[RingRecord] = None
        self.peers = RingPeers()
        self._alive = True
        self._result: Any = None
        self._lock = threading.Lock()
        log.info(f"FrankRing Active: {suit.name} [Sector {suit.sector} Ring {suit.ring_pos}]")

    def mount(self, channel: int = 1, data: bytes = b"") -> RingRecord:
        self.rec = self.frank.spawn_ring(
            process_name=self.suit.name,
            channel=channel,
            family=self.suit.family,
            permissions=self.suit.permissions,
            sector=self.suit.sector,
            ring_pos=self.suit.ring_pos,
        )
#        self._load_peers()  # helix_api not yet available

        if self.peers.propcoms:
            if not self.peers.validate(self.rec.ball):
                log.warning(f"Propcoms Clearance Block: Ring {self.rec.ring_id} dropped.")
                self.rec.state = RingState.DEAD
                return self.rec

        self.rec.ball.hand_off("frank5_core", self.suit.name)
        log.info(f"Ring {self.rec.ring_id} Mounted. PCS Identity Locked.")
        return self.rec

    def run(self, data: bytes = b"", **kwargs) -> Any:
        if not self.rec or self.rec.state == RingState.DEAD:
            raise RuntimeError("Execution Block: Ring unmounted or killed by Propcoms.")
        
        self.frank.mark_running(self.rec.ring_id, os.getpid())
        self.rec.call2(data or self.suit.name.encode())

        try:
            if self.suit.suit_type == SuitType.PYTHON:
                self._result = self._run_python(data, **kwargs)
            elif self.suit.suit_type in (SuitType.SHELL, SuitType.BINARY):
                self._result = self._run_subprocess(data)
            elif self.suit.suit_type == SuitType.NODE:
                self._result = self._run_node(data)
            elif self.suit.suit_type == SuitType.POWER:
                self._result = self._run_powershell(data)
            else:
                raise ValueError(f"Unknown execution lane: {self.suit.suit_type}")
        except Exception as e:
            log.error(f"Suit execution error on ring {self.rec.ring_id}: {e}")
            with self._lock:
                self.rec.state = RingState.DEAD
            self.frank.bus.write_ring_state(self.rec.ring_id, RingState.DEAD)
            raise
        return self._result

    def sync(self, final_data: bytes = b"") -> bool:
        if not self.rec:
            return False
        self.frank.mark_syncing(self.rec.ring_id)
        
        payload = final_data or json.dumps({"result": str(self._result)}).encode()
        self.rec.call3(payload)
        
        is_definitive = bool(self.rec.pcs.definitive)
        log.info(f"Ring {self.rec.ring_id} Syncing — p3={self.rec.pcs.p3} | Definitive={is_definitive}")
        
        if is_definitive:
            self._snap_clone()

        if self.peers.propcoms:
            self.peers.tick()
        return is_definitive

    def die(self):
        if not self.rec:
            return
        
        with self._lock:
            self.rec.died = time.monotonic()
            self.rec.state = RingState.DONE
            
        self.frank.bus.write_ring_state(self.rec.ring_id, RingState.DONE)
        custody = self.rec.to_custody_record()
        self.frank._commit_custody(custody)
        
        log.info(f"Ring {self.rec.ring_id} Terminated Cleanly. Memory slot opened.")
        self._alive = False

    def ride(self, data: bytes = b"", channel: int = 1, **kwargs) -> Any:
        try:
            self.mount(channel=channel, data=data)
            if not self.rec or self.rec.state == RingState.DEAD:
                return None
            result = self.run(data=data, **kwargs)
            self.sync(final_data=data)
            return result
        finally:
            self.die()

    # -------------------------------------------------------------------------
    # Option A — Python suit
    # -------------------------------------------------------------------------
    def _run_python(self, data: bytes, **kwargs) -> Any:
        entry = self.suit.entry
        try:
            mod = importlib.import_module(entry)
            log.debug(f"Python module attached: {entry}")
        except ImportError:
            spec = importlib.util.spec_from_file_location(self.suit.name, entry)
            if not spec:
                raise ImportError(f"Cannot load file path entry: {entry}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            log.debug(f"Python module initialized from file path: {entry}")

        if hasattr(mod, "run"):
            return mod.run(data, self.rec.ball, self.rec.pcs, **kwargs)
        elif hasattr(mod, "main"):
            return mod.main()
        else:
            raise AttributeError(f"Suit {entry} missing required run(data, ball, pcs) hook.")

    # -------------------------------------------------------------------------
    # Option B — Subprocess suits (any language)
    # -------------------------------------------------------------------------
    def _run_subprocess(self, data: bytes) -> bytes:
        env = os.environ.copy()
        env.update(self.suit.env)

        if self.rec.ball:
            env["FRANK_BALL_FAMILY"] = str(self.rec.ball.family)
            env["FRANK_BALL_ZIPCODE"] = str(self.rec.ball.zipcode)
            env["FRANK_BALL_SLOT"] = str(int(self.rec.ball.slot))
            env["FRANK_BALL_PERMS"] = json.dumps(self.rec.ball.permissions)
            
        if self.rec.pcs:
            env["FRANK_PCS"] = str(self.rec.pcs.string())
            env["FRANK_PCS_HASH"] = str(self.rec.pcs.hash)
            env["FRANK_RING_ID"] = str(self.rec.ring_id)
            env["FRANK_SUIT"] = str(self.suit.name)
            env["FRANK_SECTOR"] = str(self.suit.sector)

        cmd = self.suit.entry if isinstance(self.suit.entry, list) else [self.suit.entry]
        if self.suit.args:
            cmd += self.suit.args

        result = subprocess.run(
            cmd,
            input=data,
            capture_output=True,
            timeout=self.suit.timeout,
            env=env,
        )
        
        if result.returncode != 0:
            log.error(f"Suit {self.suit.name} execution error {result.returncode}: {result.stderr.decode('utf-8', errors='ignore')[:200]}")
        return result.stdout

    def _run_node(self, data: bytes) -> bytes:
        original_entry = self.suit.entry
        self.suit.entry = ["node", original_entry]
        return self._run_subprocess(data)

    def _run_powershell(self, data: bytes) -> bytes:
        original_entry = self.suit.entry


def suit_for(name: str, sector: int, ring_pos: int, family: str = "system") -> SuitSpec:
    """Helper — build a SuitSpec by name. Used by process_library."""
    from franken5 import FAMILY_SLOT, KernelSlot
    suit_type = SuitType.PYTHON
    if name.endswith(".sh"):  suit_type = SuitType.SHELL
    if name.endswith(".js"):  suit_type = SuitType.NODE
    if name.endswith(".ps1"): suit_type = SuitType.POWER
    if name.endswith(".c"):   suit_type = SuitType.BINARY
    return SuitSpec(
        name=name, suit_type=suit_type, entry=name,
        sector=sector, ring_pos=ring_pos, family=family
    )


def wear(suit: SuitSpec, data: bytes = b"", channel: int = 1, **kwargs):
    """Wear a suit — create a ring and ride it. One call. Frank wears it."""
    from franken5 import get_frank
    ring = FrankRing(suit, get_frank())
    return ring.ride(data=data, channel=channel, **kwargs)


def _load_peers(self):
    try:
        from helix_api import Franken2, Freewheeling, Propcoms
        self.peers.franken2 = Franken2()
        self.peers.freewheeling = Freewheeling()
        self.peers.propcoms = Propcoms()
    except ImportError:
        pass
    except Exception as e:
        import logging
        logging.getLogger("frank_ring").warning(f"Peer load error: {e}")
