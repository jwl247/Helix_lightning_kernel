#!/usr/bin/env python3
"""
frank_ring.py — The Frank Ring
Phoenix DevOps OS / Helix Lightning Kernel | jwl247 | GPL v3

A Frank ring is a Frank clone riding a process suit.
Frank does not travel. Frank clones.
The clone wears the suit. The suit does the work.
When the work is done the clone dies. The suit drops.
The kernel cleans up. D1 keeps the record. Nothing leaks.

The ring has three peers:
  - Franken2    — load balancer / router
  - Freewheeling — memory / storage controller
  - Propcoms    — ring validator / heartbeat

Frank clones into the ring. The ring wears the suit.
Frank IS the PCS. The ball travels with him.
When p3 >= 90 — definitive. Snap-clone fires. Frank dies clean.

Option A — Python suit: imported as module. Sub-millisecond spawn.
Option B — Any language suit: subprocess. Same handshake. Same cleanup.
Frank does not care which. The ring handles it transparently.
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
    Frank5, get_frank, RingRecord, RingState,
    Ball, PCS, DataFamily, KernelSlot,
    FRANK_VERSION, AUDIT_PATH, SHM_PATH
)

RING_VERSION = "1.0.0-alpha"

log = logging.getLogger("frank_ring")


class SuitType(IntEnum):
    """
    Option A — Python module. Fast. Pure Python.
    Option B — Subprocess. Any language. Same handshake.
    Frank does not care which. Ring handles it.
    """
    PYTHON  = 0   # import as module — sub-millisecond
    SHELL   = 1   # bash/sh script
    BINARY  = 2   # compiled binary
    NODE    = 3   # javascript/node
    POWER   = 4   # powershell


# Sector definitions — 4 sectors, 4 rings each
SECTOR_MAP = {
    1: {
        "name": "Boot/Kernel",
        "rings": {
            0: "frank3_slot_a",
            1: "frank3_slot_b",
            2: "phoenix_auth",
            3: "concierge",
        }
    },
    2: {
        "name": "Intake/Package",
        "rings": {
            0: "intake",
            1: "clone_pool",
            2: "propagator",
            3: "packages_worker",
        }
    },
    3: {
        "name": "Comms/Network",
        "rings": {
            0: "romeo",
            1: "juliet",
            2: "dbl_juliet",
            3: "quadengine",
        }
    },
    4: {
        "name": "Core Engine",
        "rings": {
            0: "helix",
            1: "freewheeling",
            2: "propcoms",
            3: "conductor",
        }
    },
}


@dataclass
class SuitSpec:
    """
    A suit hanging in the process library closet.
    Frank grabs one of these at spawn time.
    The spec tells Frank everything he needs to wear it.
    """
    name:        str
    suit_type:   SuitType
    entry:       str          # module path or script path or binary
    sector:      int          # which sector this suit belongs to
    ring_pos:    int          # ring position within sector
    family:      str          = DataFamily.SYSTEM
    permissions: dict         = field(default_factory=dict)
    args:        list         = field(default_factory=list)
    env:         dict         = field(default_factory=dict)
    timeout:     float        = 30.0
    description: str          = ""


@dataclass
class RingPeers:
    """
    The three peers that make a ring.
    Frank clones into the ring. The peers do their jobs.
    Franken2 routes. Freewheeling stores. Propcoms validates.
    """
    franken2:     Any = None    # load balancer — routes balls
    freewheeling: Any = None    # memory bank — warm/cold storage
    propcoms:     Any = None    # ring validator — heartbeat + tick

    def all_alive(self) -> bool:
        return all([
            self.franken2     is not None,
            self.freewheeling is not None,
            self.propcoms     is not None,
        ])

    def tick(self) -> dict:
        if self.propcoms:
            return self.propcoms.tick(self.franken2, self.freewheeling)
        return {}

    def validate(self, ball: Ball) -> bool:
        """Nothing reaches a ring without Propcoms clearance."""
        if not self.propcoms:
            return False
        result = self.propcoms.validate(
            {"family": ball.family, "zipcode": ball.zipcode},
            {"target": f"system_{ball.ring_pos + 1}"}
        )
        return result.get("validated", False)

    def route(self, ball: Ball) -> str:
        """Franken2 decides which system target handles the ball."""
        if not self.franken2:
            return "system_1"
        result = self.franken2.propose_route({"type": ball.family})
        return result.get("target", "system_1")


class FrankRing:
    """
    A Frank clone wearing a process suit.

    Frank does not travel. Frank clones.
    The clone wears the suit. Does the work. Dies clean.

    This is the only unit that should be instantiated per process.
    Never instantiate Frank5 directly in a ring.
    Always import get_frank() for the core.
    """

    def __init__(self, suit: SuitSpec, frank: Optional[Frank5] = None):
        self.suit   = suit
        self.frank  = frank or get_frank()
        self.rec:   Optional[RingRecord] = None
        self.peers  = RingPeers()
        self._alive = True
        self._result: Any = None
        self._lock  = threading.Lock()

        log.info(
            f"FrankRing init — {suit.name} "
            f"sector{suit.sector} ring{suit.ring_pos} "
            f"[{suit.suit_type.name}]"
        )

    def mount(self, channel: int = 1, data: bytes = b"") -> RingRecord:
        """
        Mount the ring. Frank clones. Ball and PCS born.
        Propcoms must clear the ball before the suit goes on.
        """
        # Spawn the ring record in Frank-core
        self.rec = self.frank.spawn_ring(
            process_name = self.suit.name,
            channel      = channel,
            family       = self.suit.family,
            permissions  = self.suit.permissions,
            sector       = self.suit.sector,
            ring_pos     = self.suit.ring_pos,
        )

        # Load the ring peers
        self._load_peers()

        # Propcoms gate — nothing rides without clearance
        if self.peers.propcoms:
            if not self.peers.validate(self.rec.ball):
                log.warning(
                    f"Ring {self.rec.ring_id} blocked by Propcoms — "
                    f"ball not cleared for {self.suit.name}"
                )
                self.rec.state = RingState.DEAD
                return self.rec

        # PCS call1 — ring born, slot reserved
        # Ball hands off from frank5_core to this ring
        self.rec.ball.hand_off("frank5_core", self.suit.name)

        log.info(
            f"Ring {self.rec.ring_id} mounted — "
            f"{self.suit.name} wearing suit — "
            f"pcs={self.rec.pcs.string()[:24]}…"
        )
        return self.rec

    def run(self, data: bytes = b"", **kwargs) -> Any:
        """
        Wear the suit. Do the work.
        Frank does not care what language. Ring handles it.
        Option A — Python. Option B — subprocess.
        """
        if not self.rec or self.rec.state == RingState.DEAD:
            raise RuntimeError("Ring not mounted or blocked by Propcoms")

        self.frank.mark_running(self.rec.ring_id, os.getpid())

        # PCS call2 — running, data accumulates
        self.rec.call2(data or self.suit.name.encode())

        log.info(
            f"Ring {self.rec.ring_id} running — "
            f"suit={self.suit.name} "
            f"p2={self.rec.pcs.p2}"
        )

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
                raise ValueError(f"Unknown suit type: {self.suit.suit_type}")

        except Exception as e:
            log.error(f"Ring {self.rec.ring_id} suit error: {e}")
            self.rec.state = RingState.DEAD
            self.frank.bus.write_ring_state(self.rec.ring_id, RingState.DEAD)
            raise

        return self._result

    def sync(self, final_data: bytes = b"") -> bool:
        """
        PCS call3 — final accumulation.
        If definitive (p3 >= 90) — snap-clone fires.
        Frank syncs. Frank dies. Kernel cleans up.
        Returns True if definitive commit fired.
        """
        if not self.rec:
            return False

        self.frank.mark_syncing(self.rec.ring_id)

        # PCS call3 — definitive check
        definitive = self.rec.call3(
            final_data or json.dumps({"result": str(self._result)}).encode()
        )

        log.info(
            f"Ring {self.rec.ring_id} syncing — "
            f"p3={self.rec.pcs.p3} "
            f"definitive={definitive}"
        )

        if definitive:
            self._snap_clone()

        # Peers tick — ring heartbeat
        if self.peers.propcoms:
            tick = self.peers.tick()
            log.debug(f"Ring {self.rec.ring_id} tick: {tick}")

        return definitive

    def die(self):
        """
        Frank dies clean. Kernel handles the rest.
        Custody record committed to D1.
        Suit drops. Ring slot opens.
        """
        if not self.rec:
            return

        self.rec.died  = time.monotonic()
        self.rec.state = RingState.DONE

        self.frank.bus.write_ring_state(self.rec.ring_id, RingState.DONE)

        # Commit custody to D1
        custody = self.rec.to_custody_record()
        self.frank._commit_custody(custody)
        self.frank._audit_record("RING_DIED_CLEAN", {
            "ring_id":    self.rec.ring_id,
            "suit":       self.suit.name,
            "age_ms":     round(self.rec.age() * 1000, 2),
            "definitive": self.rec.pcs.definitive if self.rec.pcs else False,
            "pcs":        self.rec.pcs.string() if self.rec.pcs else "",
        })

        log.info(
            f"Ring {self.rec.ring_id} died clean — "
            f"{self.suit.name} — "
            f"{self.rec.age()*1000:.1f}ms — "
            f"custody committed"
        )

        self._alive = False

    def ride(self, data: bytes = b"", channel: int = 1, **kwargs) -> Any:
        """
        Full lifecycle in one call.
        Mount → Run → Sync → Die.
        Frank rides the lightning.
        """
        try:
            self.mount(channel=channel, data=data)
            if self.rec.state == RingState.DEAD:
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
        """
        Import the Python module. Call its main entry point.
        Sub-millisecond spawn. Pure Python.
        Frank wears it like a suit. Module does the work.
        """
        entry = self.suit.entry

        # Try as module path first (e.g. "sector2.intake")
        try:
            mod = importlib.import_module(entry)
            log.debug(f"Python suit imported: {entry}")
        except ImportError:
            # Try as file path
            spec = importlib.util.spec_from_file_location(
                self.suit.name,
                entry
            )
            if not spec:
                raise ImportError(f"Cannot load suit: {entry}")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            log.debug(f"Python suit loaded from file: {entry}")

        # Call the suit's entry point
        # Convention: every suit exposes run(data, ball, pcs) -> Any
        if hasattr(mod, "run"):
            return mod.run(data, self.rec.ball, self.rec.pcs, **kwargs)
        elif hasattr(mod, "main"):
            return mod.main()
        else:
            raise AttributeError(
                f"Suit {entry} has no run() or main() — "
                f"every suit must expose run(data, ball, pcs)"
            )

    # -------------------------------------------------------------------------
    # Option B — Subprocess suits (any language)
    # -------------------------------------------------------------------------

    def _run_subprocess(self, data: bytes) -> bytes:
        """
        Shell/binary suit. Any language.
        Same handshake as Python. Same cleanup.
        Ball and PCS passed as environment variables.
        """
        env = os.environ.copy()
        env.update(self.suit.env)

        # Pass Ball and PCS to the subprocess as env vars
        # The suit reads these to know its permissions and identity
        if self.rec.ball:
            env["FRANK_BALL_FAMILY"]  = self.rec.ball.family
            env["FRANK_BALL_ZIPCODE"] = self.rec.ball.zipcode
            env["FRANK_BALL_SLOT"]    = str(int(self.rec.ball.slot))
            env["FRANK_BALL_PERMS"]   = json.dumps(self.rec.ball.permissions)
        if self.rec.pcs:
            env["FRANK_PCS"]          = self.rec.pcs.string()
            env["FRANK_PCS_HASH"]     = self.rec.pcs.hash
        env["FRANK_RING_ID"]          = str(self.rec.ring_id)
        env["FRANK_SUIT"]             = self.suit.name
        env["FRANK_SECTOR"]           = str(self.suit.sector)

        cmd = [self.suit.entry] + self.suit.args

        result = subprocess.run(
            cmd,
            input          = data,
            capture_output = True,
            timeout        = self.suit.timeout,
            env            = env,
        )

        if result.returncode != 0:
            log.error(
                f"Suit {self.suit.name} exited {result.returncode}: "
                f"{result.stderr.decode()[:200]}"
            )

        return result.stdout

    def _run_node(self, data: bytes) -> bytes:
        """Node.js suit. Same handshake as shell."""
        self.suit.entry = f"node {self.suit.entry}"
        return self._run_subprocess(data)

    def _run_powershell(self, data: bytes) -> bytes:
        """PowerShell suit. For Windows-side processes."""
        cmd_original = self.suit.entry
        self.suit.entry = "pwsh"
        self.suit.args  = ["-File", cmd_original] + self.suit.args
        return self._run_subprocess(data)

    # -------------------------------------------------------------------------
    # Ring peers
    # -------------------------------------------------------------------------

    def _load_peers(self):
        """
        Load the three ring peers from helix_api.
        Franken2, Freewheeling, Propcoms.
        If they can't load — ring runs without them but logs the gap.
        """
        try:
            from helix_api import Franken2, Freewheeling, Propcoms
            self.peers.franken2     = Franken2()
            self.peers.freewheeling = Freewheeling()
            self.peers.propcoms     = Propcoms()
            log.debug(
                f"Ring {self.rec.ring_id if self.rec else '?'} "
                f"peers loaded — Franken2 + Freewheeling + Propcoms"
            )
        except ImportError:
            log.warning("helix_api not found — ring running without peers")
        except Exception as e:
            log.warning(f"Peer load error: {e} — ring running without peers")

    # -------------------------------------------------------------------------
    # Snap-clone
    # -------------------------------------------------------------------------

    def _snap_clone(self):
        """
        Definitive commit. p3 >= 90.
        Snap-clone to the right clonepool zone.
        This is where the ball's zipcode proves its worth.
        """
        if not self.rec or not self.rec.ball:
            return

        zone    = self.rec.ball.zipcode
        stamp   = self.rec.stamp()
        pcs_str = self.rec.pcs.string() if self.rec.pcs else ""

        log.info(
            f"SNAP-CLONE fired — "
            f"ring={self.rec.ring_id} "
            f"zone={zone} "
            f"stamp={stamp} "
            f"pcs={pcs_str[:24]}…"
        )

        # Write snap record to shared memory for Helix-E to flush
        snap = json.dumps({
            "event":   "SNAP_CLONE",
            "ring_id": self.rec.ring_id,
            "suit":    self.suit.name,
            "zone":    zone,
            "stamp":   stamp,
            "pcs":     pcs_str,
            "ts":      time.time(),
        }).encode()

        try:
            self.frank.bus.write_stage(self.rec.ring_id % 64, snap)
        except Exception as e:
            log.error(f"Snap-clone write failed: {e}")

        # Signal Helix-E — Frank commands the flush
        try:
            import signal as _signal
            frank_pid = int(os.environ.get("FRANK5_PID", os.getpid()))
            os.kill(frank_pid, _signal.SIGUSR2)
        except Exception as e:
            log.warning(f"Helix-E signal failed: {e}")


# =============================================================================
# Convenience — spawn a ring for a suit in one call
# =============================================================================

def wear(suit: SuitSpec, data: bytes = b"",
         channel: int = 1, **kwargs) -> Any:
    """
    The one-liner. Frank wears a suit. Gets the result. Dies clean.

    Usage:
        result = wear(suit_spec, data=b"my data")

    That's it. Frank handles everything else.
    """
    ring = FrankRing(suit)
    return ring.ride(data=data, channel=channel, **kwargs)


def suit_for(sector: int, ring_pos: int,
             suit_type: SuitType = SuitType.PYTHON,
             entry: str = "") -> SuitSpec:
    """
    Build a SuitSpec from the sector map.
    If entry not provided, uses the process name from SECTOR_MAP.
    """
    sector_info = SECTOR_MAP.get(sector, {})
    rings       = sector_info.get("rings", {})
    name        = rings.get(ring_pos, f"sector{sector}_ring{ring_pos}")

    family_map = {
        1: DataFamily.SYSTEM,
        2: DataFamily.USER,
        3: DataFamily.NETWORK,
        4: DataFamily.SYSTEM,
    }

    return SuitSpec(
        name      = name,
        suit_type = suit_type,
        entry     = entry or name,
        sector    = sector,
        ring_pos  = ring_pos,
        family    = family_map.get(sector, DataFamily.SYSTEM),
    )


# =============================================================================
# Demo
# =============================================================================

if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s [RING] %(levelname)s %(message)s",
        handlers= [logging.StreamHandler()]
    )

    frank = get_frank()
    frank.boot()

    print("\n" + "="*60)
    print("FRANK RING DEMO — Riding the Lightning")
    print("="*60 + "\n")

    # Demo suit — inline Python function as a suit
    # In production this would be a real module from the process library
    demo_mod_code = '''
def run(data, ball, pcs, **kwargs):
    import time
    print(f"  Suit running — family={ball.family} slot={int(ball.slot)}")
    print(f"  PCS: {pcs.string()}")
    print(f"  Data: {data.decode()}")
    time.sleep(0.01)  # simulate work
    return {"status": "done", "processed": len(data)}
'''
    # Write temp suit module
    import tempfile
    tmp = tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False
    )
    tmp.write(demo_mod_code)
    tmp.close()

    # One ring per sector
    for sector in range(1, 5):
        for ring_pos in range(4):
            spec = suit_for(
                sector   = sector,
                ring_pos = ring_pos,
                suit_type= SuitType.PYTHON,
                entry    = tmp.name,
            )
            spec.family = [
                DataFamily.SYSTEM,
                DataFamily.USER,
                DataFamily.NETWORK,
                DataFamily.AI,
            ][sector - 1]

            print(f"Sector {sector} Ring {ring_pos} — {spec.name}")
            ring = FrankRing(spec, frank)
            ring.mount(channel=sector, data=b"hello phoenix")
            if ring.rec and ring.rec.state != RingState.DEAD:
                result = ring.run(data=f"sector{sector} ring{ring_pos}".encode())
                ring.sync(final_data=b"sync complete")
                print(f"  Result: {result}")
            ring.die()
            print()

    import os
    os.unlink(tmp.name)

    print("="*60)
    print(f"All 16 rings rode the lightning.")
    print(f"Frank-core status: {frank.status()}")
    print("="*60)

    frank.shutdown()
