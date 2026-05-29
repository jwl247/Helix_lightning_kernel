#!/usr/bin/env python3
"""
franken5.py — Frank5 Core Conductor
Phoenix DevOps OS | jwl247 | GPL v3

Frank is not a process manager.
Frank is not a daemon.
Frank does not hold processes.

Frank is imported. Frank rides. Frank dies clean.

Frank-core's four jobs:
  1. Know which rings are alive
  2. Know which stage each ring is on
  3. Fire the next interrupt when Helix-I signals stage ready
  4. Confirm to Helix-E when a ring is done

Everything else is done by the suit Frank is wearing.
The kernel cleans up. Frank never leaks.
"""

import os
import sys
import time
import signal
import logging
import hashlib
import mmap
import struct
import threading
from pathlib import Path
from typing import Callable, Optional
from dataclasses import dataclass, field
from enum import IntEnum, auto
import json
import hashlib

FRANK_VERSION   = "5.1.0-alpha"
FRANK_IDENT     = "FRANK5"
SHM_PATH        = Path(os.environ.get("PHOENIX_SHM", "/tmp/phoenix_shm"))
STAGE_SLOT_SIZE = 4096        # bytes per stage slot in shared memory
MAX_RINGS       = 64          # max concurrent Frank rings alive at once
AUDIT_PATH      = Path(os.environ.get("PHOENIX_AUDIT", "/tmp/phoenix_audit.log"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FRANK5] %(levelname)s %(message)s",
    handlers=[logging.FileHandler(AUDIT_PATH), logging.StreamHandler()]
)
log = logging.getLogger("frank5")


class DataFamily(str):
    """
    Every signal has a family. Family determines slot and clonepool zone.
    Frank knows his family at birth. It never changes.
    """
    PHYSICS = "physics"   # slot 0 — VECTOR      — /mnt/clonepool/@red
    NETWORK = "network"   # slot 1 — NOSQL        — /mnt/clonepool/@green
    AI      = "ai"        # slot 3 — TIMESERIES   — /mnt/clonepool/@blue
    ASSETS  = "assets"    # slot 1 — NOSQL        — /mnt/clonepool/@cyan
    SYSTEM  = "system"    # slot 0 — VECTOR       — /mnt/clonepool/@magenta
    USER    = "user"      # slot 2 — RELATIONAL   — /mnt/clonepool/@yellow


class KernelSlot(IntEnum):
    """
    4 kernel slots. Set at birth from family. Never changes.
    Frank wears the right slot for the work he's doing.
    """
    C_PURE       = 0   # physics/system — VECTOR      — max speed, peak traffic
    C_SIDELOAD   = 1   # network/assets — NOSQL        — balanced, extended calls
    PYTHON_USER  = 2   # user           — RELATIONAL   — flexible, moderate load
    PYTHON_FULL  = 3   # ai             — TIMESERIES   — full flexibility, dev


# Family → slot mapping
FAMILY_SLOT: dict[str, KernelSlot] = {
    DataFamily.PHYSICS: KernelSlot.C_PURE,
    DataFamily.SYSTEM:  KernelSlot.C_PURE,
    DataFamily.NETWORK: KernelSlot.C_SIDELOAD,
    DataFamily.ASSETS:  KernelSlot.C_SIDELOAD,
    DataFamily.USER:    KernelSlot.PYTHON_USER,
    DataFamily.AI:      KernelSlot.PYTHON_FULL,
}

# Family → clonepool zone
FAMILY_ZONE: dict[str, str] = {
    DataFamily.PHYSICS: "/mnt/clonepool/@red",
    DataFamily.NETWORK: "/mnt/clonepool/@green",
    DataFamily.AI:      "/mnt/clonepool/@blue",
    DataFamily.ASSETS:  "/mnt/clonepool/@cyan",
    DataFamily.SYSTEM:  "/mnt/clonepool/@magenta",
    DataFamily.USER:    "/mnt/clonepool/@yellow",
}


@dataclass
class Ball:
    """
    The ball travels with Frank through every process he wears.
    It never leaves him. It IS the permission system.
    No process Frank wears can do anything the ball doesn't authorize.

    Born at ring spawn. Committed to D1 when Frank dies.
    The ball IS the chain of evidence.
    """
    family:      str                        # DataFamily
    zipcode:     str                        # clonepool zone
    slot:        KernelSlot                 # kernel slot — set at birth, never changes
    permissions: dict  = field(default_factory=dict)   # what Frank can do
    destination: str   = ""                 # where he's going
    sector:      int   = 4                  # which sector — 1,2,3,4
    ring_pos:    int   = 0                  # ring position within sector
    custody:     list  = field(default_factory=list)   # every hand it passed through
    metadata:    dict  = field(default_factory=dict)   # anything extra

    def authorize(self, action: str) -> bool:
        """Ball says what Frank CAN do. If it's not in here, Frank can't do it."""
        return self.permissions.get(action, False)

    def hand_off(self, from_component: str, to_component: str):
        """Record every hand the ball passes through. Immutable custody chain."""
        self.custody.append({
            "from": from_component,
            "to":   to_component,
            "ts":   time.time()
        })

    def to_dict(self) -> dict:
        return {
            "family":      self.family,
            "zipcode":     self.zipcode,
            "slot":        int(self.slot),
            "permissions": self.permissions,
            "destination": self.destination,
            "sector":      self.sector,
            "ring_pos":    self.ring_pos,
            "custody":     self.custody,
            "metadata":    self.metadata,
        }

    @classmethod
    def for_family(cls, family: str, sector: int = 4,
                   ring_pos: int = 0, permissions: dict = None) -> "Ball":
        """
        Born at ring spawn. Family determines everything else.
        Birds of a feather flock together.
        """
        slot    = FAMILY_SLOT.get(family, KernelSlot.PYTHON_USER)
        zipcode = FAMILY_ZONE.get(family, "/mnt/clonepool/@yellow")
        return cls(
            family      = family,
            zipcode     = zipcode,
            slot        = slot,
            sector      = sector,
            ring_pos    = ring_pos,
            permissions = permissions or {
                "read":       True,
                "write":      True,
                "clone":      True,
                "translate":  False,   # only at sector3 boundary
                "delete":     False,   # never
                "kernel":     False,   # never from userspace
            }
        )


@dataclass
class PCS:
    """
    Proximity Control String.
    Frank IS the PCS. Every Frank clone has one.
    Born at ring spawn. Travels with Frank. Committed to D1 on death.

    Format: {hash16}:{zipcode}:{p1}:{p2}:{p3}:{definitive}
    Example: a1b2c3d4e5f6a7b8:red:72:85:94:1

    The 3-call lifecycle:
      Call 1 — SPAWNING  — p1 derived from hash
      Call 2 — RUNNING   — hash re-hashed with new data, p2 calculated
      Call 3 — SYNCING   — p3 calculated — if p3 >= 90: definitive → snap-clone fires
    """
    _hash:       str   = ""
    zipcode:     str   = ""
    p1:          int   = 0
    p2:          int   = 0
    p3:          int   = 0
    definitive:  bool  = False
    _orig_hash:  str   = ""    # save original — hash mutates on call2/call3

    @classmethod
    def born(cls, data: bytes, zipcode: str) -> "PCS":
        """Call 1 — PCS born. Slot reserved. p1 derived from hash."""
        import hashlib
        h = hashlib.blake2s(data, digest_size=8).hexdigest()
        p1 = min(int(h[:2], 16) % 100, 99)
        pcs = cls(_hash=h, _orig_hash=h, zipcode=zipcode, p1=p1)
        return pcs

    def call2(self, new_data: bytes) -> "PCS":
        """Call 2 — flock accumulates. Hash absorbs new data. p2 calculated."""
        combined = (self._hash + new_data.hex()).encode()
        self._hash = hashlib.blake2s(combined, digest_size=8).hexdigest()
        self.p2    = min((self.p1 + int(self._hash[:2], 16) % 30), 99)
        return self

    def call3(self, final_data: bytes) -> "PCS":
        """
        Call 3 — final accumulation. Definitive check.
        If p3 >= 90: definitive = True → snap-clone fires.
        """
        combined = (self._hash + final_data.hex()).encode()
        self._hash   = hashlib.blake2s(combined, digest_size=8).hexdigest()
        self.p3      = min((self.p2 + int(self._hash[:2], 16) % 20), 100)
        self.definitive = self.p3 >= 90
        return self

    @property
    def hash(self) -> str:
        return self._hash

    @property
    def orig_hash(self) -> str:
        return self._orig_hash

    def string(self) -> str:
        """The PCS string. Frank's identity in transit."""
        zone = self.zipcode.split("@")[-1] if "@" in self.zipcode else self.zipcode
        return (f"{self._hash}:{zone}:"
                f"{self.p1}:{self.p2}:{self.p3}:"
                f"{'1' if self.definitive else '0'}")

    def to_dict(self) -> dict:
        return {
            "hash":        self._hash,
            "orig_hash":   self._orig_hash,
            "zipcode":     self.zipcode,
            "p1":          self.p1,
            "p2":          self.p2,
            "p3":          self.p3,
            "definitive":  self.definitive,
            "pcs_string":  self.string(),
        }


class RingState(IntEnum):
    IDLE      = 0
    SPAWNING  = auto()
    RUNNING   = auto()
    SYNCING   = auto()
    DONE      = auto()
    DEAD      = auto()


class FrankSignal(IntEnum):
    STAGE_READY   = signal.SIGUSR1   # Helix-I fires this
    RING_DONE     = signal.SIGUSR2   # Frank-ring fires this on exit
    SHUTDOWN      = signal.SIGTERM


@dataclass
class RingRecord:
    """
    A Frank clone riding a process suit.
    Carries his Ball (permissions) and PCS (identity) everywhere he goes.
    Dies clean. Kernel cleans up. Ball and PCS go to D1.
    """
    ring_id:    int
    process:    str
    channel:    int
    state:      RingState = RingState.IDLE
    stage:      int       = 0
    pid:        int       = 0
    born:       float     = field(default_factory=time.monotonic)
    died:       float     = 0.0
    ball:       Optional[Ball] = None    # permissions + destination — travels with Frank
    pcs:        Optional[PCS]  = None    # identity + probability chain — Frank IS the PCS

    def age(self) -> float:
        if self.died:
            return self.died - self.born
        return time.monotonic() - self.born

    def stamp(self) -> str:
        h = hashlib.sha3_256(
            f"{self.ring_id}:{self.process}:{self.born}".encode()
        ).hexdigest()[:16]
        return f"FRANK5:{h}"

    def call2(self, data: bytes):
        """Frank is running. PCS absorbs new data. p2 calculated."""
        if self.pcs:
            self.pcs.call2(data)
            if self.ball:
                self.ball.hand_off(self.process, "call2")

    def call3(self, data: bytes) -> bool:
        """
        Frank is syncing. Final accumulation.
        If definitive — snap-clone fires. Frank dies clean.
        Returns True if definitive.
        """
        if self.pcs:
            self.pcs.call3(data)
            if self.ball:
                self.ball.hand_off("call2", "D1")
            return self.pcs.definitive
        return False

    def to_custody_record(self) -> dict:
        """
        The complete custody record committed to D1 when Frank dies.
        This is the chain of evidence. Immutable forever.
        """
        return {
            "stamp":   self.stamp(),
            "ring_id": self.ring_id,
            "process": self.process,
            "channel": self.channel,
            "born":    self.born,
            "died":    self.died,
            "age_ms":  round(self.age() * 1000, 2),
            "ball":    self.ball.to_dict() if self.ball else {},
            "pcs":     self.pcs.to_dict()  if self.pcs  else {},
        }


class SharedMemoryBus:
    """
    The 256GB SSD spine.
    Frank, Helix-I, and Helix-E all read/write here.
    Stage data lives here. Ring state lives here.
    Nothing goes to disk at runtime — it's already here.
    """

    HEADER_FMT  = "!4sHHI"   # magic(4) version(2) ring_count(2) flags(4)
    HEADER_SIZE = struct.calcsize(HEADER_FMT)
    MAGIC       = b"PHNX"

    def __init__(self, size_mb: int = 256):
        self.size  = size_mb * 1024 * 1024
        self.path  = SHM_PATH / "frank5.shm"
        self._mm: Optional[mmap.mmap] = None
        self._fd: Optional[int]       = None
        self._lock = threading.Lock()

    def mount(self):
        SHM_PATH.mkdir(parents=True, exist_ok=True)
        existed = self.path.exists()
        self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT)
        if not existed:
            os.ftruncate(self._fd, self.size)
            log.info(f"SHM created: {self.path} ({self.size // 1024 // 1024}MB)")
        self._mm = mmap.mmap(self._fd, self.size)
        if not existed:
            self._write_header(ring_count=0, flags=0)
        log.info("SHM bus mounted")

    def unmount(self):
        if self._mm:
            self._mm.flush()
            self._mm.close()
        if self._fd:
            os.close(self._fd)
        log.info("SHM bus unmounted")

    def _write_header(self, ring_count: int, flags: int):
        with self._lock:
            self._mm.seek(0)
            self._mm.write(struct.pack(
                self.HEADER_FMT,
                self.MAGIC, 5, ring_count, flags
            ))

    def write_stage(self, slot: int, data: bytes):
        if len(data) > STAGE_SLOT_SIZE:
            raise ValueError(f"Stage data {len(data)}b exceeds slot {STAGE_SLOT_SIZE}b")
        offset = self.HEADER_SIZE + (slot * STAGE_SLOT_SIZE)
        with self._lock:
            self._mm.seek(offset)
            self._mm.write(data.ljust(STAGE_SLOT_SIZE, b'\x00'))

    def read_stage(self, slot: int) -> bytes:
        offset = self.HEADER_SIZE + (slot * STAGE_SLOT_SIZE)
        with self._lock:
            self._mm.seek(offset)
            return self._mm.read(STAGE_SLOT_SIZE).rstrip(b'\x00')

    def write_ring_state(self, ring_id: int, state: RingState):
        state_base = self.HEADER_SIZE + (MAX_RINGS * STAGE_SLOT_SIZE)
        offset     = state_base + (ring_id * 4)
        with self._lock:
            self._mm.seek(offset)
            self._mm.write(struct.pack("!I", int(state)))

    def read_ring_state(self, ring_id: int) -> RingState:
        state_base = self.HEADER_SIZE + (MAX_RINGS * STAGE_SLOT_SIZE)
        offset     = state_base + (ring_id * 4)
        with self._lock:
            self._mm.seek(offset)
            raw = struct.unpack("!I", self._mm.read(4))[0]
            return RingState(raw)


class Frank5:
    """
    Frank-core. The conductor.

    Does not process data.
    Does not translate.
    Does not manage files.

    Watches the clock. Keeps the beat.
    Fires interrupts. Confirms completions.
    Knows every ring that is alive.
    """

    def __init__(self):
        self.bus        = SharedMemoryBus()
        self.rings:     dict[int, RingRecord] = {}
        self._ring_seq  = 0
        self._alive     = True
        self._lock      = threading.Lock()
        self._stage_ready_event = threading.Event()

        self._audit_record("FRANK5_BOOT", {
            "version": FRANK_VERSION,
            "pid":     os.getpid(),
            "shm":     str(SHM_PATH),
        })

    def boot(self):
        self.bus.mount()
        self._install_signal_handlers()
        log.info(f"Frank5 v{FRANK_VERSION} online — PID {os.getpid()}")

    def shutdown(self):
        self._alive = False
        self.bus.unmount()
        self._audit_record("FRANK5_SHUTDOWN", {"rings_alive": len(self._live_rings())})
        log.info("Frank5 shutdown complete")

    def _install_signal_handlers(self):
        signal.signal(FrankSignal.STAGE_READY, self._on_stage_ready)
        signal.signal(FrankSignal.RING_DONE,   self._on_ring_done)
        signal.signal(FrankSignal.SHUTDOWN,    self._on_shutdown)

    def _on_stage_ready(self, signum, frame):
        """Helix-I fired. A stage of data is waiting. Wake the conductor."""
        self._stage_ready_event.set()

    def _on_ring_done(self, signum, frame):
        """A Frank-ring finished. Find it, mark it dead, commit custody to D1."""
        pid = os.waitpid(-1, os.WNOHANG)[0] if os.getpid() != os.getppid() else 0
        with self._lock:
            for rec in self.rings.values():
                if rec.pid == pid or rec.state == RingState.SYNCING:
                    rec.state = RingState.DONE
                    rec.died  = time.monotonic()
                    self.bus.write_ring_state(rec.ring_id, RingState.DONE)
                    # Custody record — Ball and PCS committed forever
                    custody = rec.to_custody_record()
                    self._audit_record("RING_DONE", custody)
                    self._commit_custody(custody)
                    log.info(
                        f"Ring {rec.ring_id} ({rec.process}) done "
                        f"in {rec.age()*1000:.1f}ms — "
                        f"pcs={'definitive' if rec.pcs and rec.pcs.definitive else 'open'}"
                    )
                    break

    def _on_shutdown(self, signum, frame):
        log.info("Shutdown signal received")
        self.shutdown()
        sys.exit(0)

    def spawn_ring(self, process_name: str, channel: int,
                   stage: int = 0, family: str = DataFamily.SYSTEM,
                   permissions: dict = None, sector: int = 4,
                   ring_pos: int = 0) -> RingRecord:
        """
        Spawn a Frank-ring wearing a process suit.
        Ball and PCS born here. Travel with Frank forever.
        Frank-core registers it and gets out of the way.
        The ring does the work. Frank conducts.
        """
        with self._lock:
            if len(self._live_rings()) >= MAX_RINGS:
                raise RuntimeError(f"Ring ceiling hit: {MAX_RINGS} rings alive")

            self._ring_seq += 1

            # Ball born — permissions set at birth
            ball = Ball.for_family(
                family      = family,
                sector      = sector,
                ring_pos    = ring_pos,
                permissions = permissions,
            )
            ball.hand_off("frank5_core", process_name)

            # PCS born — Frank IS the PCS
            seed = f"{process_name}:{self._ring_seq}:{time.time()}".encode()
            pcs  = PCS.born(seed, ball.zipcode)

            rec = RingRecord(
                ring_id  = self._ring_seq,
                process  = process_name,
                channel  = channel,
                state    = RingState.SPAWNING,
                stage    = stage,
                ball     = ball,
                pcs      = pcs,
            )
            self.rings[rec.ring_id] = rec
            self.bus.write_ring_state(rec.ring_id, RingState.SPAWNING)

        self._audit_record("RING_SPAWN", {
            "ring_id":    rec.ring_id,
            "process":    process_name,
            "channel":    channel,
            "stage":      stage,
            "family":     family,
            "slot":       int(ball.slot),
            "zipcode":    ball.zipcode,
            "pcs":        pcs.string(),
            "stamp":      rec.stamp(),
        })
        log.info(
            f"Ring {rec.ring_id} spawning — {process_name} "
            f"ch{channel} slot{int(ball.slot)} [{family}] "
            f"pcs={pcs.string()[:24]}…"
        )
        return rec

    def mark_running(self, ring_id: int, pid: int):
        with self._lock:
            if ring_id in self.rings:
                self.rings[ring_id].state = RingState.RUNNING
                self.rings[ring_id].pid   = pid
                self.bus.write_ring_state(ring_id, RingState.RUNNING)

    def mark_syncing(self, ring_id: int):
        with self._lock:
            if ring_id in self.rings:
                self.rings[ring_id].state = RingState.SYNCING
                self.bus.write_ring_state(ring_id, RingState.SYNCING)

    def wait_for_stage(self, timeout: float = 5.0) -> bool:
        """Block until Helix-I signals a stage is ready. Returns True if stage arrived."""
        fired = self._stage_ready_event.wait(timeout=timeout)
        self._stage_ready_event.clear()
        return fired

    def conduct(self, dispatch: Callable[[RingRecord], None]):
        """
        Main conductor loop.
        Wait for Helix-I interrupt.
        Fire dispatch for each pending stage.
        That's it. That's the whole job.
        """
        log.info("Frank5 conducting — waiting for Helix-I")
        while self._alive:
            if self.wait_for_stage(timeout=1.0):
                pending = self._pending_rings()
                for rec in pending:
                    try:
                        dispatch(rec)
                    except Exception as e:
                        log.error(f"Dispatch failed for ring {rec.ring_id}: {e}")
                        rec.state = RingState.DEAD
                        self.bus.write_ring_state(rec.ring_id, RingState.DEAD)

    def _commit_custody(self, record: dict):
        """
        Commit custody record to D1.
        For now writes to audit log — D1 worker wires in here.
        This is the chain of evidence. Immutable forever.
        """
        custody_path = Path(os.environ.get(
            "PHOENIX_CUSTODY", "/tmp/phoenix_custody.jsonl"
        ))
        try:
            with open(custody_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            log.error(f"Custody commit failed: {e}")

    def _live_rings(self) -> list[RingRecord]:
        return [r for r in self.rings.values()
                if r.state not in (RingState.DONE, RingState.DEAD)]

    def _pending_rings(self) -> list[RingRecord]:
        return [r for r in self.rings.values()
                if r.state == RingState.SPAWNING]

    def status(self) -> dict:
        with self._lock:
            return {
                "version":     FRANK_VERSION,
                "pid":         os.getpid(),
                "rings_total": len(self.rings),
                "rings_live":  len(self._live_rings()),
                "rings_done":  len([r for r in self.rings.values()
                                    if r.state == RingState.DONE]),
            }

    def _audit_record(self, event: str, data: dict):
        entry = {
            "ts":    time.time(),
            "event": event,
            **data
        }
        try:
            with open(AUDIT_PATH, "a") as f:
                import json
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


_frank: Optional[Frank5] = None


def get_frank() -> Frank5:
    """
    The one true Frank-core.
    Import this in any process that needs to talk to Frank-core.
    Do not instantiate Frank5 directly in a ring — use frank_ring.py.
    """
    global _frank
    if _frank is None:
        _frank = Frank5()
    return _frank


if __name__ == "__main__":
    frank = get_frank()
    frank.boot()

    def demo_dispatch(rec: RingRecord):
        log.info(f"Conducting ring {rec.ring_id} — {rec.process}")
        log.info(f"  Ball: family={rec.ball.family} slot={int(rec.ball.slot)} zip={rec.ball.zipcode}")
        log.info(f"  PCS:  {rec.pcs.string()}")
        frank.mark_running(rec.ring_id, os.getpid())
        # Simulate 3-call lifecycle
        rec.call2(b"data:accumulating")
        log.info(f"  PCS call2: p2={rec.pcs.p2}")
        rec.call3(b"data:final")
        log.info(f"  PCS call3: p3={rec.pcs.p3} definitive={rec.pcs.definitive}")
        frank.mark_syncing(rec.ring_id)

    # Demo — spawn a ring for each family
    for family in [DataFamily.PHYSICS, DataFamily.NETWORK,
                   DataFamily.USER, DataFamily.AI]:
        frank.spawn_ring(
            process_name = f"demo_{family}",
            channel      = 1,
            family       = family,
            sector       = 4,
        )

    try:
        frank.conduct(demo_dispatch)
    except KeyboardInterrupt:
        frank.shutdown()
