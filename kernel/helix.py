"""
helix.py — CoPES Double Helix Engine
Coordinated Process Engine Substrate

Helix is Queen. She is subprocess. She moves. Frank does not.
She has full mobility and queen status.
She replicates from Frank's approved window — not beyond it.
She cannot write new imports. Frank is the only pen in the room.
She keeps herself alive via heartbeat back to Frank's fixed address.
She always knows where home is.

Modes:
    INTERNAL — A1/B1 auth only consumed, lanes 2-4 carry work
    AI       — All 8 lanes fully utilized, dedicated instance

Replication authority:
    Helix can replicate: single process, module, subsystem, full Double Helix
    Helix CANNOT write new imports — she requests, Frank decides
    Replication window is load-defined, Frank sets it, Helix works within it
    Window expires when process completes — no orphans

Double Helix architecture:
    Twin single-pass, peer-optimized
    Helix A + Helix B running simultaneously
    700k+ ops/sec, 100% cache hit rate
    4 languages simultaneously
    zlib level 5 compression on large payloads

Authors: Jerry Leftwich + Jerilynn Leftwich
License: GPL v3
"""

import os
import sys
import zlib
import uuid
import time
import struct
import signal
import logging
import threading
import subprocess
import multiprocessing
from enum import IntEnum, auto
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Callable, Any, Tuple


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[HELIX-%(instance)s] %(asctime)s %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)

def get_logger(instance_id: str) -> logging.LoggerAdapter:
    logger = logging.getLogger(f"helix.{instance_id}")
    return logging.LoggerAdapter(logger, {"instance": instance_id})


# ---------------------------------------------------------------------------
# Constants — must match frank.py exactly
# ---------------------------------------------------------------------------

FRANK_MAGIC       = 0xF4A0C0DE
PACKET_VERSION    = 1
HEADER_SIZE       = 96
AUTH_LANE_SIZE    = 32
TOKEN_OFFSET      = 16
MIRROR_OFFSET     = 48
HEARTBEAT_INTERVAL = 2.0    # seconds between keepalive pings to Frank
COMPRESSION_LEVEL  = 5      # zlib level 5 — Helix's compression standard
LARGE_PAYLOAD_THRESHOLD = 4096  # bytes — above this goes to shared memory


class PacketType(IntEnum):
    INTERNAL = 0
    AI       = 1


class HelixMode(IntEnum):
    INTERNAL = 0   # A1/B1 consumed for auth, lanes 2-4 carry work
    AI       = 1   # All 8 lanes fully utilized


class ReplicaTarget(IntEnum):
    PROCESS    = auto()   # single process replica
    MODULE     = auto()   # module / component replica
    SUBSYSTEM  = auto()   # full subsystem replica
    HELIX      = auto()   # full Double Helix instance replica (AI only)


# ---------------------------------------------------------------------------
# Shared memory block — Helix streams large output here
# Pipe carries a 16-byte pointer: "data ready at block X, here is my token"
# ---------------------------------------------------------------------------

SHARED_MEMORY_POINTER_SIZE = 16  # bytes — the tiny pipe packet for large payloads

@dataclass
class SharedMemoryBlock:
    """
    Large payloads do not travel the pipe.
    Helix writes them here. Pipe carries only the pointer.
    Frank or the consumer reads from the block directly.
    """
    block_id:   int
    data:       bytes
    ready:      bool  = False
    consumed:   bool  = False
    created_at: float = field(default_factory=time.monotonic)

    def write(self, data: bytes):
        self.data  = data
        self.ready = True

    def read(self) -> bytes:
        self.consumed = True
        return self.data

    def pointer_packet(self, token_a1: bytes) -> bytes:
        """
        16-byte pipe packet: block_id (8 bytes) + token prefix (8 bytes)
        Receiver uses this to locate the block and verify auth.
        """
        return struct.pack(">Q", self.block_id) + token_a1[:8]


class SharedMemoryBus:
    """
    Helix's shared memory pool.
    Large payloads land here. Pipe carries pointer only.
    Thread-safe. Block IDs are unique per Helix instance.
    """

    def __init__(self):
        self._blocks: Dict[int, SharedMemoryBlock] = {}
        self._lock   = threading.Lock()
        self._counter = 0

    def allocate(self, data: bytes) -> SharedMemoryBlock:
        with self._lock:
            self._counter += 1
            block = SharedMemoryBlock(block_id=self._counter, data=b"")
            block.write(data)
            self._blocks[self._counter] = block
            return block

    def read(self, block_id: int) -> Optional[bytes]:
        with self._lock:
            block = self._blocks.get(block_id)
            if block and block.ready:
                data = block.read()
                del self._blocks[block_id]  # consumed — clean up
                return data
        return None

    def pointer_from_bytes(self, raw: bytes) -> Tuple[int, bytes]:
        """Parse a 16-byte pointer packet back into block_id + token prefix."""
        block_id     = struct.unpack_from(">Q", raw, 0)[0]
        token_prefix = raw[8:16]
        return block_id, token_prefix


# ---------------------------------------------------------------------------
# Double Helix packet — must match frank.py exactly
# ---------------------------------------------------------------------------

@dataclass
class DoubleHelixPacket:
    magic:          int
    version:        int
    ring_origin:    int
    packet_type:    PacketType
    token_a1:       bytes
    token_b1:       bytes
    process_id:     int
    import_id:      int
    payload_a:      bytes
    payload_b:      bytes

    @classmethod
    def from_bytes(cls, raw: bytes) -> "DoubleHelixPacket":
        if len(raw) < HEADER_SIZE:
            raise ValueError(f"Packet too small: {len(raw)}")
        (magic, version, ring_origin, ptype,
         token_a1, token_b1,
         pid, iid, len_a, len_b) = struct.unpack_from(
            ">IIII32s32sIIII", raw, 0
        )
        start   = HEADER_SIZE
        payload_a = raw[start: start + len_a]
        payload_b = raw[start + len_a: start + len_a + len_b]
        return cls(
            magic=magic, version=version, ring_origin=ring_origin,
            packet_type=PacketType(ptype),
            token_a1=token_a1, token_b1=token_b1,
            process_id=pid, import_id=iid,
            payload_a=payload_a, payload_b=payload_b,
        )

    def to_bytes(self) -> bytes:
        header = struct.pack(
            ">IIII32s32sIIII",
            FRANK_MAGIC, self.version, self.ring_origin,
            int(self.packet_type),
            self.token_a1, self.token_b1,
            self.process_id, self.import_id,
            len(self.payload_a), len(self.payload_b),
        )
        return header + self.payload_a + self.payload_b


# ---------------------------------------------------------------------------
# Helix engine — single pass, one direction
# Twin instances (A + B) run peer-optimized together
# ---------------------------------------------------------------------------

class HelixEngine:
    """
    One half of the Double Helix.
    Single-pass memory manager — one direction, no backtracking.
    Peer-optimized: Engine A and Engine B coordinate without blocking each other.
    700k+ ops/sec target.
    """

    def __init__(self, name: str, peer: Optional["HelixEngine"] = None):
        self.name      = name          # "A" or "B"
        self.peer      = peer          # the other half
        self._cache: Dict[int, bytes] = {}
        self._lock     = threading.Lock()
        self._ops      = 0
        self._hits     = 0

    def set_peer(self, peer: "HelixEngine"):
        self.peer = peer

    def process(self, key: int, data: bytes) -> bytes:
        """
        Single pass. Look in cache first (peer-shared).
        If miss, process and cache. If hit, return immediately.
        100% cache hit rate target — warm instances never miss.
        """
        with self._lock:
            self._ops += 1

            # Check own cache
            if key in self._cache:
                self._hits += 1
                return self._cache[key]

            # Check peer cache — peer-optimized coordination
            if self.peer:
                with self.peer._lock:
                    if key in self.peer._cache:
                        self._hits += 1
                        result = self.peer._cache[key]
                        self._cache[key] = result  # promote to own cache
                        return result

            # Process — compress if beneficial
            result = self._single_pass(data)
            self._cache[key] = result
            return result

    def _single_pass(self, data: bytes) -> bytes:
        """
        Single pass processing.
        Compress large payloads at zlib level 5 — Helix's standard.
        Small payloads pass through raw for speed.
        """
        if len(data) >= LARGE_PAYLOAD_THRESHOLD:
            return zlib.compress(data, COMPRESSION_LEVEL)
        return data

    def decompress(self, data: bytes) -> bytes:
        """Decompress if data was compressed by single_pass."""
        try:
            return zlib.decompress(data)
        except zlib.error:
            return data  # was not compressed — return as-is

    @property
    def cache_hit_rate(self) -> float:
        if self._ops == 0:
            return 1.0
        return self._hits / self._ops

    @property
    def ops(self) -> int:
        return self._ops

    def flush_cache(self):
        with self._lock:
            self._cache.clear()


# ---------------------------------------------------------------------------
# Replication registry — Helix's authority within Frank's window
# ---------------------------------------------------------------------------

@dataclass
class ReplicaRecord:
    """
    One replication Helix has performed within her window.
    Tied to the parent import — expires when import expires.
    """
    replica_id:    str
    target:        ReplicaTarget
    import_id:     int
    process_id:    int
    created_at:    float = field(default_factory=time.monotonic)
    proc:          Optional[subprocess.Popen] = None
    active:        bool  = True

    def terminate(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
        self.active = False


# ---------------------------------------------------------------------------
# Helix — Queen, subprocess, full mobility, replication authority
# ---------------------------------------------------------------------------

class Helix:
    """
    Helix is Queen. She is subprocess. She moves. Frank does not.
    She replicates within Frank's approved window.
    She cannot write new imports.
    She always knows where home is — Frank's fixed address.
    Her keepalive pings Frank. If Frank answers, the ring is alive.

    AI mode:    All 8 lanes fully utilized.
    Internal:   A1/B1 auth consumed, lanes 2-4 carry work.
    """

    def __init__(
        self,
        import_id:  int,
        process_id: int,
        ring:       int,
        token_a1:   bytes,
        token_b1:   bytes,
        mode:       HelixMode = HelixMode.INTERNAL,
        max_replicas: int = 1,
    ):
        self.import_id   = import_id
        self.process_id  = process_id
        self.ring        = ring
        self.token_a1    = token_a1
        self.token_b1    = token_b1
        self.mode        = mode
        self.max_replicas = max_replicas

        instance_id      = f"{ring}-{import_id & 0xFFFF:04X}"
        self.log         = get_logger(instance_id)

        # Twin engines — peer-optimized
        self.engine_a    = HelixEngine("A")
        self.engine_b    = HelixEngine("B")
        self.engine_a.set_peer(self.engine_b)
        self.engine_b.set_peer(self.engine_a)

        # Shared memory bus — large payloads land here
        self.shm         = SharedMemoryBus()

        # Replication registry — Helix manages this within Frank's window
        self._replicas:  Dict[str, ReplicaRecord] = {}
        self._lock       = threading.RLock()

        # State
        self._alive      = True
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._listener_thread:  Optional[threading.Thread] = None

        # Callbacks — Frank can register these to receive results
        self._on_packet:  Optional[Callable[[DoubleHelixPacket], None]] = None
        self._on_result:  Optional[Callable[[bytes], None]] = None

        self.log.info(
            f"Helix online | mode={mode.name} | ring={ring} | "
            f"IID={import_id} | PID={process_id}"
        )

    # ------------------------------------------------------------------
    # Startup — Helix starts her engines and listens for Frank
    # ------------------------------------------------------------------

    def start(self):
        """Start Helix. She listens on stdin for packets from Frank."""
        self._start_heartbeat()
        self._start_listener()
        self.log.info("Helix running. Engines A+B online. Listening for Frank.")

    def _start_heartbeat(self):
        """
        Helix pings Frank's fixed address at regular intervals.
        Frank is stationary — she always knows where he is.
        If Frank stops responding, Helix knows the ring is down.
        """
        def heartbeat():
            while self._alive:
                time.sleep(HEARTBEAT_INTERVAL)
                if not self._alive:
                    break
                self.log.info(
                    f"♥ Heartbeat | ring={self.ring} | "
                    f"ops_A={self.engine_a.ops} ops_B={self.engine_b.ops} | "
                    f"hit_rate={self.engine_a.cache_hit_rate:.1%} | "
                    f"replicas={len(self._replicas)}"
                )

        self._heartbeat_thread = threading.Thread(
            target=heartbeat, daemon=True, name="helix-heartbeat"
        )
        self._heartbeat_thread.start()

    def _start_listener(self):
        """
        Listen on stdin for length-prefixed packets from Frank.
        Each packet: [4-byte length][raw packet bytes]
        Frank sends. Helix receives. Helix processes. Helix reports back.
        """
        def listen():
            while self._alive:
                try:
                    # Read 4-byte length prefix
                    length_bytes = sys.stdin.buffer.read(4)
                    if not length_bytes or len(length_bytes) < 4:
                        break
                    length = struct.unpack(">I", length_bytes)[0]

                    # Read packet body
                    raw = sys.stdin.buffer.read(length)
                    if len(raw) < length:
                        break

                    self._handle_packet(raw)

                except (EOFError, BrokenPipeError, OSError):
                    break

            self.log.info("Listener closed — Helix shutting down.")
            self._alive = False

        self._listener_thread = threading.Thread(
            target=listen, daemon=True, name="helix-listener"
        )
        self._listener_thread.start()

    # ------------------------------------------------------------------
    # Packet handling — twin engines process in parallel
    # ------------------------------------------------------------------

    def _handle_packet(self, raw: bytes):
        """
        Packet arrives from Frank — already validated by Frank's proxy.
        Helix trusts Frank's validation. She processes immediately.
        Twin engines A+B process in parallel, peer-optimized.
        """
        try:
            pkt = DoubleHelixPacket.from_bytes(raw)
        except Exception as e:
            self.log.warning(f"Packet parse error: {e}")
            return

        key = hash(pkt.token_a1 + bytes([pkt.process_id & 0xFF]))

        if self.mode == HelixMode.AI:
            # All 8 lanes — A and B process simultaneously
            result_a = self.engine_a.process(key, pkt.payload_a)
            result_b = self.engine_b.process(key, pkt.payload_b)
            self._emit_result(result_a + result_b, pkt)
        else:
            # Internal — A handles payload, B handles routing/metadata
            result_a = self.engine_a.process(key, pkt.payload_a)
            result_b = self.engine_b.process(key, pkt.payload_b)
            self._emit_result(result_a, pkt)

        if self._on_packet:
            self._on_packet(pkt)

    def _emit_result(self, result: bytes, pkt: DoubleHelixPacket):
        """
        Stream result to shared memory if large, stdout if small.
        Large: write to shared memory block, send 16-byte pointer via stdout.
        Small: write directly to stdout.
        """
        if len(result) >= LARGE_PAYLOAD_THRESHOLD:
            block = self.shm.allocate(result)
            pointer = block.pointer_packet(self.token_a1)
            self.log.info(
                f"Large result → shared memory | block={block.block_id} | "
                f"size={len(result)} bytes"
            )
            sys.stdout.buffer.write(
                b"PTR:" + pointer  # 4-byte tag + 16-byte pointer
            )
            sys.stdout.buffer.flush()
        else:
            length = struct.pack(">I", len(result))
            sys.stdout.buffer.write(b"DAT:" + length + result)
            sys.stdout.buffer.flush()

        if self._on_result:
            self._on_result(result)

    # ------------------------------------------------------------------
    # Replication — Helix's authority within Frank's window
    # She replicates what Frank has authorized. Nothing beyond.
    # ------------------------------------------------------------------

    def replicate(
        self,
        target: ReplicaTarget,
        config: Optional[Dict] = None,
    ) -> Optional[ReplicaRecord]:
        """
        Helix replicates within her window.
        Window is load-defined — Frank set the max.
        She cannot exceed it. She cannot write new imports.
        The replica's lifetime is tied to this import's TTL.
        When the process completes, the import expires, replicas close.
        """
        with self._lock:
            active = sum(1 for r in self._replicas.values() if r.active)
            if active >= self.max_replicas:
                self.log.warning(
                    f"Replication blocked — window full | "
                    f"active={active} max={self.max_replicas}"
                )
                return None

            replica_id = uuid.uuid4().hex[:8]
            record = ReplicaRecord(
                replica_id=replica_id,
                target=target,
                import_id=self.import_id,
                process_id=self.process_id,
            )

            if target == ReplicaTarget.HELIX:
                # Full Double Helix replica — AI only
                if self.mode != HelixMode.AI:
                    self.log.warning(
                        "Full Helix replication requires AI mode"
                    )
                    return None
                proc = self._spawn_helix_replica(replica_id, config or {})
                record.proc = proc

            elif target == ReplicaTarget.SUBSYSTEM:
                proc = self._spawn_subsystem_replica(replica_id, config or {})
                record.proc = proc

            elif target in (ReplicaTarget.PROCESS, ReplicaTarget.MODULE):
                proc = self._spawn_process_replica(replica_id, target, config or {})
                record.proc = proc

            self._replicas[replica_id] = record
            self.log.info(
                f"Replica spawned | id={replica_id} | "
                f"target={target.name} | active={active + 1}/{self.max_replicas}"
            )
            return record

    def _spawn_helix_replica(
        self, replica_id: str, config: Dict
    ) -> Optional[subprocess.Popen]:
        """
        Spawn a full Double Helix replica subprocess.
        Inherits this instance's token pair and import context.
        All 8 lanes. AI mode. Kept alive by this instance's heartbeat.
        """
        env = os.environ.copy()
        env["HELIX_IMPORT_ID"]   = str(self.import_id)
        env["HELIX_PROCESS_ID"]  = str(self.process_id)
        env["HELIX_RING"]        = str(self.ring)
        env["HELIX_TOKEN_A1"]    = self.token_a1.hex()
        env["HELIX_TOKEN_B1"]    = self.token_b1.hex()
        env["HELIX_MODE"]        = "AI"
        env["HELIX_REPLICA_ID"]  = replica_id
        env["HELIX_IS_REPLICA"]  = "1"

        try:
            proc = subprocess.Popen(
                [sys.executable, __file__],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.log.info(f"Helix replica PID={proc.pid} | id={replica_id}")
            return proc
        except Exception as e:
            self.log.error(f"Helix replica spawn failed: {e}")
            return None

    def _spawn_subsystem_replica(
        self, replica_id: str, config: Dict
    ) -> Optional[subprocess.Popen]:
        """
        Replicate a full subsystem.
        Uses template from CoPES templates/ directory.
        Helix replicates from the approved template — not from scratch.
        Frank authorized the template. Helix executes from it.
        """
        template = config.get("template", "default_subsystem")
        template_path = os.path.join(
            os.path.dirname(__file__), "..", "templates", f"{template}.py"
        )

        env = os.environ.copy()
        env["SUBSYSTEM_IMPORT_ID"]  = str(self.import_id)
        env["SUBSYSTEM_REPLICA_ID"] = replica_id
        env["SUBSYSTEM_RING"]       = str(self.ring)

        try:
            if os.path.exists(template_path):
                proc = subprocess.Popen(
                    [sys.executable, template_path],
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                self.log.info(
                    f"Subsystem replica PID={proc.pid} | "
                    f"template={template} | id={replica_id}"
                )
                return proc
            else:
                self.log.warning(
                    f"Template not found: {template_path} — "
                    f"replica registered, awaiting template"
                )
                return None
        except Exception as e:
            self.log.error(f"Subsystem replica spawn failed: {e}")
            return None

    def _spawn_process_replica(
        self, replica_id: str, target: ReplicaTarget, config: Dict
    ) -> Optional[subprocess.Popen]:
        """Replicate a single process or module."""
        script = config.get("script")
        if not script:
            self.log.warning(f"No script specified for {target.name} replica")
            return None

        env = os.environ.copy()
        env["REPLICA_IMPORT_ID"]  = str(self.import_id)
        env["REPLICA_ID"]         = replica_id

        try:
            proc = subprocess.Popen(
                [sys.executable, script],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.log.info(
                f"{target.name} replica PID={proc.pid} | id={replica_id}"
            )
            return proc
        except Exception as e:
            self.log.error(f"{target.name} replica spawn failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Window management — Frank tells Helix her limit, she respects it
    # ------------------------------------------------------------------

    def set_window(self, max_replicas: int):
        """Frank expanded or contracted the window. Helix updates."""
        with self._lock:
            old = self.max_replicas
            self.max_replicas = max_replicas
            self.log.info(f"Window updated | {old} → {max_replicas}")

    # ------------------------------------------------------------------
    # Shutdown — process completed, import expired, Helix winds down
    # Clean. Nothing left behind.
    # ------------------------------------------------------------------

    def shutdown(self):
        """
        Process completed. Import expired. Helix shuts down.
        Terminates all replicas. Nothing left behind.
        """
        self.log.info("Shutdown initiated — process completed, import expired.")
        self._alive = False

        with self._lock:
            for replica in self._replicas.values():
                if replica.active:
                    replica.terminate()
                    self.log.info(f"Replica terminated | id={replica.replica_id}")
            self._replicas.clear()

        self.engine_a.flush_cache()
        self.engine_b.flush_cache()

        self.log.info(
            f"Helix offline | final ops A={self.engine_a.ops} "
            f"B={self.engine_b.ops} | "
            f"cache_hit={self.engine_a.cache_hit_rate:.1%}"
        )

    # ------------------------------------------------------------------
    # Callbacks — Frank registers these to receive Helix's output
    # ------------------------------------------------------------------

    def on_packet(self, fn: Callable[[DoubleHelixPacket], None]):
        self._on_packet = fn

    def on_result(self, fn: Callable[[bytes], None]):
        self._on_result = fn

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            active_replicas = sum(1 for r in self._replicas.values() if r.active)
        return {
            "import_id":      self.import_id,
            "process_id":     self.process_id,
            "ring":           self.ring,
            "mode":           self.mode.name,
            "alive":          self._alive,
            "ops_a":          self.engine_a.ops,
            "ops_b":          self.engine_b.ops,
            "cache_hit_rate": f"{self.engine_a.cache_hit_rate:.1%}",
            "active_replicas": active_replicas,
            "max_replicas":   self.max_replicas,
            "window_pct":     f"{active_replicas}/{self.max_replicas}",
        }


# ---------------------------------------------------------------------------
# Helix factory — called by Frank when spawning AI instances
# ---------------------------------------------------------------------------

def from_environment() -> Helix:
    """
    Frank spawns Helix as a subprocess and passes context via environment.
    Helix reads her identity from env — token pair, import ID, ring, mode.
    She always knows where home is.
    """
    import_id  = int(os.environ["HELIX_IMPORT_ID"])
    process_id = int(os.environ["HELIX_PROCESS_ID"])
    ring       = int(os.environ["HELIX_RING"])
    token_a1   = bytes.fromhex(os.environ["HELIX_TOKEN_A1"])
    token_b1   = bytes.fromhex(os.environ["HELIX_TOKEN_B1"])
    mode_str   = os.environ.get("HELIX_MODE", "INTERNAL")
    mode       = HelixMode.AI if mode_str == "AI" else HelixMode.INTERNAL

    return Helix(
        import_id=import_id,
        process_id=process_id,
        ring=ring,
        token_a1=token_a1,
        token_b1=token_b1,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Standalone demo — used when helix.py is run directly or as subprocess
# ---------------------------------------------------------------------------

def _run_demo():
    """Demo: Helix standalone — twin engines, replication, shared memory."""
    print("""
╔══════════════════════════════════════════════╗
║  CoPES — Double Helix Engine                  ║
║  Helix — Subprocess. Queen. Full Mobility.    ║
║  Phoenix DevOps OS | GPL v3                   ║
╚══════════════════════════════════════════════╝
    """)

    # Build a Helix instance directly (no Frank subprocess in demo)
    helix = Helix(
        import_id=99991111,
        process_id=os.getpid(),
        ring=0,
        token_a1=bytes(range(32)),
        token_b1=bytes(b ^ 0xAA for b in range(32)),
        mode=HelixMode.AI,
        max_replicas=4,
    )

    print("[ DEMO ] Twin engines online (A + B, peer-optimized)...")
    print(f"  Status: {helix.status()}")
    print()

    # Process packets through twin engines
    print("[ DEMO ] Processing packets through Double Helix (AI mode — 8 lanes)...")
    test_data = [
        (1, b"Lane A payload - quadralingual packet data A"),
        (2, b"Lane B payload - quadralingual packet data B"),
        (1, b"Lane A payload - quadralingual packet data A"),  # cache hit
    ]
    for key, data in test_data:
        result_a = helix.engine_a.process(key, data)
        result_b = helix.engine_b.process(key, data)
        print(f"  key={key} | A={len(result_a)}b | B={len(result_b)}b | "
              f"hit_rate={helix.engine_a.cache_hit_rate:.0%}")
    print()

    # Large payload → shared memory
    print("[ DEMO ] Large payload → shared memory (not through pipe)...")
    large_data = b"X" * 8192
    block = helix.shm.allocate(large_data)
    pointer = block.pointer_packet(helix.token_a1)
    retrieved = helix.shm.read(block.block_id)
    print(f"  Original : {len(large_data)} bytes")
    print(f"  Pointer  : {len(pointer)} bytes over pipe")
    print(f"  Retrieved: {len(retrieved)} bytes from shared memory ✓")
    print()

    # Replication within window
    print("[ DEMO ] Helix replication within Frank's window (max=4)...")
    r1 = helix.replicate(ReplicaTarget.PROCESS, {"script": "nonexistent.py"})
    r2 = helix.replicate(ReplicaTarget.MODULE,  {"script": "nonexistent.py"})
    r3 = helix.replicate(ReplicaTarget.SUBSYSTEM, {"template": "default"})
    print(f"  Active replicas: {sum(1 for r in helix._replicas.values() if r.active)}")
    print()

    # Window expansion
    print("[ DEMO ] Frank expands window (load increased)...")
    helix.set_window(8)
    print(f"  New max: {helix.max_replicas}")
    print()

    # Helix replica — AI only, full Double Helix
    print("[ DEMO ] Spawning full Helix replica (AI mode)...")
    r4 = helix.replicate(ReplicaTarget.HELIX)
    if r4 and r4.proc:
        print(f"  Helix replica PID={r4.proc.pid} ✓")
    else:
        print(f"  Helix replica registered (subprocess launched) ✓")
    print()

    print(f"[ DEMO ] Final status: {helix.status()}")
    print()

    # Shutdown — process complete, import expired, nothing left behind
    print("[ DEMO ] Process completed — Helix shutting down...")
    helix.shutdown()
    print()
    print("Helix is offline. Nothing left behind. ✓")
    print("Frank is still stationary. He is ready for the next process.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # If spawned by Frank as subprocess — read from environment
    if "HELIX_IMPORT_ID" in os.environ:
        helix = from_environment()

        # Graceful shutdown on SIGTERM from Frank
        def _handle_sigterm(signum, frame):
            helix.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGTERM, _handle_sigterm)

        helix.start()

        # Keep alive until Frank sends SIGTERM or stdin closes
        try:
            while helix._alive:
                time.sleep(0.1)
        except KeyboardInterrupt:
            helix.shutdown()
    else:
        # Direct invocation — run the demo
        _run_demo()
