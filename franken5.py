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

FRANK_VERSION   = "5.1.0-alpha"
FRANK_IDENT     = "FRANK5"
SHM_PATH        = Path(os.environ.get("PHOENIX_SHM", "/tmp/phoenix_shm"))
STAGE_SLOT_SIZE = 4096
MAX_RINGS       = 64
AUDIT_PATH      = Path(os.environ.get("PHOENIX_AUDIT", "/tmp/phoenix_audit.log"))


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [FRANK5] %(levelname)s %(message)s",
    handlers=[logging.FileHandler(AUDIT_PATH), logging.StreamHandler()]
)
log = logging.getLogger("frank5")


class DataFamily(str):
    PHYSICS = "physics"
    NETWORK = "network"
    AI      = "ai"
    ASSETS  = "assets"
    SYSTEM  = "system"
    USER    = "user"


class KernelSlot(IntEnum):
    C_PURE       = 0
    C_SIDELOAD   = 1
    PYTHON_USER  = 2
    PYTHON_FULL  = 3


FAMILY_SLOT: dict = {
    DataFamily.PHYSICS: KernelSlot.C_PURE,
    DataFamily.SYSTEM:  KernelSlot.C_PURE,
    DataFamily.NETWORK: KernelSlot.C_SIDELOAD,
    DataFamily.ASSETS:  KernelSlot.C_SIDELOAD,
    DataFamily.USER:    KernelSlot.PYTHON_USER,
    DataFamily.AI:      KernelSlot.PYTHON_FULL,
}

FAMILY_ZONE: dict = {
    DataFamily.PHYSICS: "/mnt/clonepool/@red",
    DataFamily.NETWORK: "/mnt/clonepool/@green",
    DataFamily.AI:      "/mnt/clonepool/@blue",
    DataFamily.ASSETS:  "/mnt/clonepool/@cyan",
    DataFamily.SYSTEM:  "/mnt/clonepool/@magenta",
    DataFamily.USER:    "/mnt/clonepool/@yellow",
}


@dataclass
class Ball:
    family:      str
    zipcode:     str
    slot:        KernelSlot
    permissions: dict = field(default_factory=dict)
    destination: str  = ""
    sector:      int  = 4
    ring_pos:    int  = 0
    custody:     list = field(default_factory=list)
    metadata:    dict = field(default_factory=dict)

    def authorize(self, action: str) -> bool:
        return self.permissions.get(action, False)

    def hand_off(self, from_component: str, to_component: str):
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
        slot    = FAMILY_SLOT.get(family, KernelSlot.PYTHON_USER)
        zipcode = FAMILY_ZONE.get(family, "/mnt/clonepool/@yellow")
        return cls(
            family      = family,
            zipcode     = zipcode,
            slot        = slot,
            sector      = sector,
            ring_pos    = ring_pos,
            permissions = permissions or {
                "read":      True,
                "write":     True,
                "clone":     True,
                "translate": False,
                "delete":    False,
                "kernel":    False,
            }
        )


@dataclass
class PCS:
    _hash:       str  = ""
    zipcode:     str  = ""
    p1:          int  = 0
    p2:          int  = 0
    p3:          int  = 0
    definitive:  bool = False
    _orig_hash:  str  = ""

    @classmethod
    def born(cls, data: bytes, zipcode: str) -> "PCS":
        h  = hashlib.blake2s(data, digest_size=8).hexdigest()
        p1 = min(int(h[:2], 16) % 100, 99)
        return cls(_hash=h, _orig_hash=h, zipcode=zipcode, p1=p1)

    def call2(self, new_data: bytes) -> "PCS":
        combined   = (self._hash + new_data.hex()).encode()
        self._hash = hashlib.blake2s(combined, digest_size=8).hexdigest()
        self.p2    = min((self.p1 + int(self._hash[:2], 16) % 30), 99)
        return self

    def call3(self, final_data: bytes) -> "PCS":
        combined        = (self._hash + final_data.hex()).encode()
        self._hash      = hashlib.blake2s(combined, digest_size=8).hexdigest()
        self.p3         = min((self.p2 + int(self._hash[:2], 16) % 20), 100)
        self.definitive = self.p3 >= 90
        return self

    @property
    def hash(self) -> str:
        return self._hash

    @property
    def orig_hash(self) -> str:
        return self._orig_hash

    def string(self) -> str:
        zone = self.zipcode.split("@")[-1] if "@" in self.zipcode else self.zipcode
        return (f"{self._hash}:{zone}:"
                f"{self.p1}:{self.p2}:{self.p3}:"
                f"{'1' if self.definitive else '0'}")

    def to_dict(self) -> dict:
        return {
            "hash":       self._hash,
            "orig_hash":  self._orig_hash,
            "zipcode":    self.zipcode,
            "p1":         self.p1,
            "p2":         self.p2,
            "p3":         self.p3,
            "definitive": self.definitive,
            "pcs_string": self.string(),
        }


class RingState(IntEnum):
    IDLE     = 0
    SPAWNING = auto()
    RUNNING  = auto()
    SYNCING  = auto()
    DONE     = auto()
    DEAD     = auto()


class FrankSignal(IntEnum):
    STAGE_READY = signal.SIGUSR1
    RING_DONE   = signal.SIGUSR2
    SHUTDOWN    = signal.SIGTERM


@dataclass
class RingRecord:
    ring_id:  int
    process:  str
    channel:  int
    state:    RingState = RingState.IDLE
    stage:    int       = 0
    pid:      int       = 0
    born:     float     = field(default_factory=time.monotonic)
    died:     float     = 0.0
    ball:     Optional[Ball] = None
    pcs:      Optional[PCS]  = None

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
        if self.pcs:
            self.pcs.call2(data)
            if self.ball:
                self.ball.hand_off(self.process, "call2")

    def call3(self, data: bytes) -> bool:
        if self.pcs:
            self.pcs.call3(data)
            if self.ball:
                self.ball.hand_off("call2", "D1")
            return self.pcs.definitive
        return False

    def to_custody_record(self) -> dict:
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
    HEADER_FMT  = "!4sHHI"
    HEADER_SIZE = struct.calcsize("!4sHHI")
    MAGIC       = b"PHNX"

    def __init__(self, size_mb: int = 256):
        self.size  = size_mb * 1024 * 1024
        self.path  = SHM_PATH / "frank5.shm"
        self._mm:   Optional[mmap.mmap] = None
        self._fd:   Optional[int]       = None
        self._lock  = threading.Lock()

    def mount(self):
        SHM_PATH.mkdir(parents=True, exist_ok=True)
        existed  = self.path.exists()
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
            self._mm.write(struct.pack(self.HEADER_FMT, self.MAGIC, 5, ring_count, flags))

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
    def __init__(self):
        self.bus       = SharedMemoryBus()
        self.rings:    dict[int, RingRecord] = {}
        self._ring_seq = 0
        self._alive    = True
        self._lock     = threading.Lock()
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
        self._stage_ready_event.set()

    def _on_ring_done(self, signum, frame):
        pid = os.waitpid(-1, os.WNOHANG)[0] if os.getpid() != os.getppid() else 0
        with self._lock:
            for rec in self.rings.values():
                if rec.pid == pid or rec.state == RingState.SYNCING:
                    rec.state = RingState.DONE
                    rec.died  = time.monotonic()
                    self.bus.write_ring_state(rec.ring_id, RingState.DONE)
                    custody = rec.to_custody_record()
                    self._audit_record("RING_DONE", custody)
                    self._commit_custody(custody)
                    log.info(
                        f"Ring {rec.ring_id} ({rec.process}) done "
                        f"in {rec.age()*1000:.1f}ms"
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
        with self._lock:
            if len(self._live_rings()) >= MAX_RINGS:
                raise RuntimeError(f"Ring ceiling hit: {MAX_RINGS} rings alive")
            self._ring_seq += 1
            ball = Ball.for_family(
                family=family, sector=sector,
                ring_pos=ring_pos, permissions=permissions,
            )
            ball.hand_off("frank5_core", process_name)
            seed = f"{process_name}:{self._ring_seq}:{time.time()}".encode()
            pcs  = PCS.born(seed, ball.zipcode)
            rec  = RingRecord(
                ring_id=self._ring_seq, process=process_name,
                channel=channel, state=RingState.SPAWNING,
                stage=stage, ball=ball, pcs=pcs,
            )
            self.rings[rec.ring_id] = rec
            self.bus.write_ring_state(rec.ring_id, RingState.SPAWNING)

        self._audit_record("RING_SPAWN", {
            "ring_id":  rec.ring_id,
            "process":  process_name,
            "channel":  channel,
            "family":   family,
            "pcs":      pcs.string(),
            "stamp":    rec.stamp(),
        })
        log.info(f"Ring {rec.ring_id} spawning — {process_name} ch{channel} [{family}]")
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
        fired = self._stage_ready_event.wait(timeout=timeout)
        self._stage_ready_event.clear()
        return fired

    def conduct(self, dispatch: Callable[[RingRecord], None]):
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
        custody_path = Path(os.environ.get(
            "PHOENIX_CUSTODY", "/tmp/phoenix_custody.jsonl"
        ))
        try:
            with open(custody_path, "a") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as e:
            log.error(f"Custody commit failed: {e}")

    def _live_rings(self) -> list:
        return [r for r in self.rings.values()
                if r.state not in (RingState.DONE, RingState.DEAD)]

    def _pending_rings(self) -> list:
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
        entry = {"ts": time.time(), "event": event, **data}
        try:
            with open(AUDIT_PATH, "a") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


_frank: Optional[Frank5] = None


def get_frank() -> Frank5:
    global _frank
    if _frank is None:
        _frank = Frank5()
    return _frank


if __name__ == "__main__":
    frank = get_frank()
    frank.boot()
    log.info(f"Frank5 v{FRANK_VERSION} standing by")
    try:
        frank.conduct(lambda rec: log.info(f"Ring {rec.ring_id} — {rec.process}"))
    except KeyboardInterrupt:
        frank.shutdown()
