"""
frank.py — CoPES Kernel Authority
Coordinated Process Engine Substrate

Frank is stationary and sovereign.
He does not move. Processes come to him.
He is the only one who writes imports.
He owns the TTL. He defines Helix's window.
There is one Frank per ring. He is King.

Architecture:
    Frank-0  (Kernel Ring) — top authority, spawns all below
    Frank-1  (Ring 1)      — sovereign in ring 1
    Frank-2  (Ring 2)      — sovereign in ring 2
    Frank-3  (Ring 3)      — ground level, work executes here

Packet structure (Double Helix — 8 lanes):
    Helix A:  A1=AUTH  A2=Payload  A3=Routing  A4=Overflow
    Helix B:  B1=MIRROR B2=Payload B3=Return   B4=AI-Extended

    Internal processes: A1/B1 auth only, lanes 2-4 carry work
    AI processes:       All 8 lanes fully utilized

Import lifecycle:
    Frank writes import → bound to process ID
    Helix window opens  → load defined, starts at bottleneck
    Process completes   → import expires, window closes
    Nothing left behind.

Authors: Jerry Leftwich + Jerilynn Leftwich
License: GPL v3
"""

import os
import sys
import uuid
import time
import struct
import logging
import threading
import subprocess
import multiprocessing
from enum import IntEnum
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="[FRANK-%(ring)s] %(asctime)s %(levelname)s — %(message)s",
    datefmt="%H:%M:%S",
)

def get_logger(ring: int) -> logging.LoggerAdapter:
    logger = logging.getLogger(f"frank.ring{ring}")
    return logging.LoggerAdapter(logger, {"ring": ring})


# ---------------------------------------------------------------------------
# Ring definitions
# ---------------------------------------------------------------------------

class Ring(IntEnum):
    KERNEL = 0   # Frank-0 — King, top authority
    ONE    = 1   # Frank-1
    TWO    = 2   # Frank-2
    THREE  = 3   # Frank-3 — ground level, work executes here


# ---------------------------------------------------------------------------
# Packet structure
# ---------------------------------------------------------------------------

# Double Helix packet — 8 lanes
# Layout (bytes):
#   [0:4]   Magic number         — 0xF4A0C0DE  (Frank's mark)
#   [4:8]   Packet version       — u32
#   [8:12]  Ring origin          — u32
#   [12:16] Packet type          — u32  (INTERNAL=0, AI=1)
#   [16:48] Lane A1 — AUTH token — 32 bytes fixed
#   [48:80] Lane B1 — MIRROR     — 32 bytes fixed
#   [80:84] Process ID           — u32
#   [84:88] Import ID            — u32  (assigned by Frank)
#   [88:92] Payload length A     — u32
#   [92:96] Payload length B     — u32
#   [96:]   Variable payload (Lane A2/A3/A4 + B2/B3/B4)

FRANK_MAGIC       = 0xF4A0C0DE
PACKET_VERSION    = 1
HEADER_SIZE       = 96
AUTH_LANE_SIZE    = 32  # bytes — fixed, always at front
TOKEN_OFFSET      = 16  # byte offset where A1 auth token begins
MIRROR_OFFSET     = 48  # byte offset where B1 mirror begins


class PacketType(IntEnum):
    INTERNAL = 0   # A1/B1 auth only, lanes 2-4 carry work
    AI       = 1   # All 8 lanes fully utilized


@dataclass
class DoubleHelixPacket:
    """
    Represents a parsed Double Helix packet.
    Raw bytes travel the pipe. Frank parses only what he needs.
    """
    magic:          int
    version:        int
    ring_origin:    int
    packet_type:    PacketType
    token_a1:       bytes          # Lane A1 — AUTH (32 bytes)
    token_b1:       bytes          # Lane B1 — MIRROR (32 bytes)
    process_id:     int
    import_id:      int
    payload_a:      bytes          # Lanes A2/A3/A4
    payload_b:      bytes          # Lanes B2/B3/B4

    @classmethod
    def from_bytes(cls, raw: bytes) -> "DoubleHelixPacket":
        if len(raw) < HEADER_SIZE:
            raise ValueError(f"Packet too small: {len(raw)} < {HEADER_SIZE}")

        (magic, version, ring_origin, ptype,
         token_a1_raw, token_b1_raw,
         pid, iid, len_a, len_b) = struct.unpack_from(
            ">IIII32s32sIIII", raw, 0
        )

        payload_start = HEADER_SIZE
        payload_a = raw[payload_start: payload_start + len_a]
        payload_b = raw[payload_start + len_a: payload_start + len_a + len_b]

        return cls(
            magic=magic,
            version=version,
            ring_origin=ring_origin,
            packet_type=PacketType(ptype),
            token_a1=token_a1_raw,
            token_b1=token_b1_raw,
            process_id=pid,
            import_id=iid,
            payload_a=payload_a,
            payload_b=payload_b,
        )

    def to_bytes(self) -> bytes:
        header = struct.pack(
            ">IIII32s32sIIII",
            FRANK_MAGIC,
            self.version,
            self.ring_origin,
            int(self.packet_type),
            self.token_a1,
            self.token_b1,
            self.process_id,
            self.import_id,
            len(self.payload_a),
            len(self.payload_b),
        )
        return header + self.payload_a + self.payload_b


def build_packet(
    ring_origin: int,
    packet_type: PacketType,
    token_a1: bytes,
    token_b1: bytes,
    process_id: int,
    import_id: int,
    payload_a: bytes = b"",
    payload_b: bytes = b"",
) -> bytes:
    """Build a raw Double Helix packet ready for the pipe."""
    pkt = DoubleHelixPacket(
        magic=FRANK_MAGIC,
        version=PACKET_VERSION,
        ring_origin=ring_origin,
        packet_type=packet_type,
        token_a1=token_a1.ljust(AUTH_LANE_SIZE, b"\x00")[:AUTH_LANE_SIZE],
        token_b1=token_b1.ljust(AUTH_LANE_SIZE, b"\x00")[:AUTH_LANE_SIZE],
        process_id=process_id,
        import_id=import_id,
        payload_a=payload_a,
        payload_b=payload_b,
    )
    return pkt.to_bytes()


# ---------------------------------------------------------------------------
# Import record — Frank writes, TTL bound to process completion
# ---------------------------------------------------------------------------

@dataclass
class ImportRecord:
    """
    Frank writes one of these for every authorized process.
    When the process completes, the import expires. Clean. Nothing left behind.
    """
    import_id:    int
    process_id:   int
    ring:         int
    packet_type:  PacketType
    token_a1:     bytes
    token_b1:     bytes
    created_at:   float = field(default_factory=time.monotonic)
    completed:    bool  = False
    helix_pid:    Optional[int] = None   # Helix subprocess PID if AI process

    def expire(self):
        """Process completed — import expires. Window closes."""
        self.completed = True

    @property
    def is_valid(self) -> bool:
        return not self.completed


# ---------------------------------------------------------------------------
# Helix window — load defined, starts at bottleneck, expands upward
# ---------------------------------------------------------------------------

@dataclass
class HelixWindow:
    """
    Frank defines this window for Helix.
    Helix replicates within it. When the process completes, it closes.
    Helix cannot write new imports — she can only replicate within her window.
    """
    import_id:      int
    ring:           int
    max_replicas:   int = 1          # load defined — expands as needed
    active_replicas: int = 0
    closed:         bool = False

    def can_replicate(self) -> bool:
        return not self.closed and self.active_replicas < self.max_replicas

    def expand(self, additional: int = 1):
        """Load increased — window expands upward."""
        self.max_replicas += additional

    def contract(self, by: int = 1):
        """Load dropped — window contracts."""
        self.max_replicas = max(1, self.max_replicas - by)

    def close(self):
        """Process completed — window closes."""
        self.closed = True
        self.active_replicas = 0


# ---------------------------------------------------------------------------
# Frank — stationary, sovereign, one per ring
# ---------------------------------------------------------------------------

class Frank:
    """
    Frank does not move. Processes come to him.
    He is the only one who writes imports.
    He validates Lane A1/B1 first — always.
    He spawns Helix and defines her window.
    He is King.
    """

    def __init__(self, ring: Ring):
        self.ring       = ring
        self.log        = get_logger(ring)
        self._lock      = threading.RLock()

        # Import registry — Frank owns this entirely
        self._imports:  Dict[int, ImportRecord] = {}
        self._windows:  Dict[int, HelixWindow]  = {}

        # Token registry — valid tokens Frank has issued
        self._tokens:   Dict[bytes, int] = {}  # token_a1 → import_id

        # Helix subprocess registry (AI processes)
        self._helix_procs: Dict[int, subprocess.Popen] = {}

        # Frank below this ring (Frank calls down, never up)
        self._frank_below: Optional["Frank"] = None

        self.log.info(f"Frank-{ring} online. Stationary. Sovereign.")

    # ------------------------------------------------------------------
    # Token generation — Frank is the only pen in the room
    # ------------------------------------------------------------------

    def _generate_token(self) -> bytes:
        """Generate a unique 32-byte auth token. Frank writes it. No one else."""
        return uuid.uuid4().bytes + uuid.uuid4().bytes  # 32 bytes

    def _generate_mirror(self, token_a1: bytes) -> bytes:
        """
        Generate B1 mirror from A1.
        Simple XOR fold for speed — proxy reads both, both must match the pair.
        """
        return bytes(b ^ 0xAA for b in token_a1)

    # ------------------------------------------------------------------
    # Import authority — Frank writes imports, binds TTL to process
    # ------------------------------------------------------------------

    def write_import(
        self,
        process_id: int,
        packet_type: PacketType,
    ) -> ImportRecord:
        """
        Frank writes a new import.
        This is the ONLY way an import enters the system.
        TTL is bound to process_id — when process completes, import expires.
        """
        with self._lock:
            import_id  = uuid.uuid4().int & 0xFFFFFFFF  # unique u32
            token_a1   = self._generate_token()
            token_b1   = self._generate_mirror(token_a1)

            record = ImportRecord(
                import_id=import_id,
                process_id=process_id,
                ring=self.ring,
                packet_type=packet_type,
                token_a1=token_a1,
                token_b1=token_b1,
            )

            self._imports[import_id] = record
            self._tokens[token_a1]   = import_id

            # Define Helix's window — starts at 1, load will expand it
            window = HelixWindow(import_id=import_id, ring=self.ring)
            self._windows[import_id] = window

            self.log.info(
                f"Import written | PID={process_id} | "
                f"IID={import_id} | type={packet_type.name}"
            )

            # If AI process — spawn dedicated Helix instance immediately
            if packet_type == PacketType.AI:
                self._spawn_helix(record)

            return record

    def expire_import(self, import_id: int):
        """
        Process completed — Frank expires the import.
        Window closes. Helix instance stopped if AI.
        Nothing left behind.
        """
        with self._lock:
            record = self._imports.get(import_id)
            if not record:
                return

            record.expire()

            # Close Helix's window
            window = self._windows.get(import_id)
            if window:
                window.close()

            # Remove token from valid set
            self._tokens.pop(record.token_a1, None)

            # Stop AI Helix subprocess if running
            if import_id in self._helix_procs:
                proc = self._helix_procs.pop(import_id)
                proc.terminate()
                self.log.info(f"Helix AI instance stopped | IID={import_id}")

            self.log.info(
                f"Import expired | PID={record.process_id} | IID={import_id}"
            )

    # ------------------------------------------------------------------
    # Proxy — Frank validates Lane A1/B1 first, always
    # Frank reads the first bytes only. Invalid = instant drop.
    # ------------------------------------------------------------------

    def validate_packet(self, raw: bytes) -> Tuple[bool, Optional[DoubleHelixPacket]]:
        """
        Lane A1 and B1 validation.
        Frank reads token at byte offset 16 (A1) and 48 (B1).
        If invalid — packet is dropped before payload is ever parsed.
        This is the wall.
        """
        if len(raw) < HEADER_SIZE:
            self.log.warning("Packet rejected — too small")
            return False, None

        # Read magic first — fast pre-check
        magic = struct.unpack_from(">I", raw, 0)[0]
        if magic != FRANK_MAGIC:
            self.log.warning(f"Packet rejected — bad magic: {magic:#010x}")
            return False, None

        # Read A1 token — first 32 bytes of auth lane
        token_a1 = raw[TOKEN_OFFSET: TOKEN_OFFSET + AUTH_LANE_SIZE]

        with self._lock:
            import_id = self._tokens.get(bytes(token_a1))
            if import_id is None:
                self.log.warning("Packet rejected — unknown token A1")
                return False, None

            record = self._imports.get(import_id)
            if not record or not record.is_valid:
                self.log.warning(f"Packet rejected — import expired | IID={import_id}")
                return False, None

            # Validate B1 mirror
            token_b1 = raw[MIRROR_OFFSET: MIRROR_OFFSET + AUTH_LANE_SIZE]
            expected_b1 = self._generate_mirror(record.token_a1)
            if bytes(token_b1) != expected_b1:
                self.log.warning("Packet rejected — B1 mirror mismatch")
                return False, None

        # Auth passed — now parse the full packet
        try:
            pkt = DoubleHelixPacket.from_bytes(raw)
        except Exception as e:
            self.log.warning(f"Packet rejected — parse error: {e}")
            return False, None

        return True, pkt

    def receive_packet(self, raw: bytes) -> Optional[DoubleHelixPacket]:
        """
        Frank's entry point for all incoming packets.
        Validate first. Forward to Helix or ring below if valid.
        Frank does not move to handle this — it comes to him.
        """
        valid, pkt = self.validate_packet(raw)
        if not valid:
            return None

        self.log.info(
            f"Packet accepted | PID={pkt.process_id} | "
            f"type={pkt.packet_type.name} | ring={pkt.ring_origin}"
        )

        # Route: AI packets go to Helix, internal go down the ring
        if pkt.packet_type == PacketType.AI:
            self._route_to_helix(pkt)
        else:
            self._route_to_ring(pkt)

        return pkt

    # ------------------------------------------------------------------
    # Helix — Frank spawns her, defines her window, keeps her registered
    # Helix is Queen. She is subprocess. She moves. Frank does not.
    # ------------------------------------------------------------------

    def _spawn_helix(self, record: ImportRecord):
        """
        Frank spawns a dedicated Double Helix instance for AI processes.
        All 8 lanes fully utilized. Kept alive by Frank's registry.
        Helix is subprocess — she has full mobility and queen status.
        Frank holds her registration. She always knows where home is.
        """
        self.log.info(
            f"Spawning Helix AI instance | IID={record.import_id} | "
            f"PID={record.process_id}"
        )

        # Helix receives her token pair and import ID via environment
        # She uses these to identify herself back to Frank
        env = os.environ.copy()
        env["HELIX_IMPORT_ID"]  = str(record.import_id)
        env["HELIX_PROCESS_ID"] = str(record.process_id)
        env["HELIX_RING"]       = str(self.ring)
        env["HELIX_TOKEN_A1"]   = record.token_a1.hex()
        env["HELIX_TOKEN_B1"]   = record.token_b1.hex()
        env["HELIX_MODE"]       = "AI"  # All 8 lanes

        # Helix subprocess — she runs, Frank registers her PID
        try:
            proc = subprocess.Popen(
                [sys.executable, "helix.py"],
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            record.helix_pid = proc.pid
            self._helix_procs[record.import_id] = proc
            self.log.info(
                f"Helix online | PID={proc.pid} | IID={record.import_id}"
            )
        except FileNotFoundError:
            # helix.py not yet present — log and continue
            # Frank's registry is ready when Helix arrives
            self.log.warning(
                "helix.py not found — Frank's registry is ready and waiting"
            )

    def _route_to_helix(self, pkt: DoubleHelixPacket):
        """Send validated AI packet to Helix subprocess via pipe."""
        proc = self._helix_procs.get(
            self._imports.get(
                self._tokens.get(pkt.token_a1), ImportRecord(0,0,0,PacketType.AI,b"",b"")
            ).import_id if pkt.token_a1 in self._tokens else -1
        )
        # Simplified direct lookup
        with self._lock:
            import_id = self._tokens.get(pkt.token_a1)
            if import_id and import_id in self._helix_procs:
                proc = self._helix_procs[import_id]
                try:
                    raw = pkt.to_bytes()
                    length = struct.pack(">I", len(raw))
                    proc.stdin.write(length + raw)
                    proc.stdin.flush()
                except BrokenPipeError:
                    self.log.warning(f"Helix pipe broken | IID={import_id}")

    def _route_to_ring(self, pkt: DoubleHelixPacket):
        """
        Frank calls DOWN to the ring below for internal packets.
        Frank does not move. He issues the call. Work comes back to him.
        """
        if self._frank_below:
            raw = pkt.to_bytes()
            self._frank_below.receive_packet(raw)
        else:
            self.log.info(
                f"Ring {self.ring} — ground level, executing work | "
                f"PID={pkt.process_id}"
            )

    # ------------------------------------------------------------------
    # Load management — Helix window expands/contracts with load
    # ------------------------------------------------------------------

    def adjust_window(self, import_id: int, load_delta: int):
        """
        Load increased or decreased — Frank adjusts Helix's window.
        Window starts at the bottleneck and expands upward as needed.
        Frank defines it. Helix operates within it.
        """
        with self._lock:
            window = self._windows.get(import_id)
            if not window or window.closed:
                return

            if load_delta > 0:
                window.expand(load_delta)
                self.log.info(
                    f"Window expanded | IID={import_id} | "
                    f"max={window.max_replicas}"
                )
            elif load_delta < 0:
                window.contract(abs(load_delta))
                self.log.info(
                    f"Window contracted | IID={import_id} | "
                    f"max={window.max_replicas}"
                )

    # ------------------------------------------------------------------
    # Ring chain — Frank-0 spawns Frank instances below
    # Each Frank is sovereign in his ring
    # ------------------------------------------------------------------

    def install_frank_below(self, frank: "Frank"):
        """
        Frank-0 installs Frank-1 below him.
        Frank-1 installs Frank-2. And so on.
        Frank calls down. Never up.
        """
        self._frank_below = frank
        self.log.info(f"Frank-{frank.ring} installed below Frank-{self.ring}")

    # ------------------------------------------------------------------
    # Status — Frank knows exactly what's running in his ring
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return {
                "ring":           self.ring,
                "active_imports": sum(
                    1 for r in self._imports.values() if r.is_valid
                ),
                "total_imports":  len(self._imports),
                "ai_instances":   len(self._helix_procs),
                "frank_below":    self._frank_below.ring if self._frank_below else None,
            }


# ---------------------------------------------------------------------------
# Ring chain builder — Frank-0 is King, all others answer to him
# ---------------------------------------------------------------------------

def build_ring_chain() -> Frank:
    """
    Build the full Frank ring chain.
    Frank-0 at top. Frank-3 at ground level.
    Returns Frank-0 — the only entry point.
    """
    frank3 = Frank(Ring.THREE)
    frank2 = Frank(Ring.TWO)
    frank1 = Frank(Ring.ONE)
    frank0 = Frank(Ring.KERNEL)

    frank2.install_frank_below(frank3)
    frank1.install_frank_below(frank2)
    frank0.install_frank_below(frank1)

    return frank0


# ---------------------------------------------------------------------------
# Entry point — Frank starts here and does not move
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("""
╔══════════════════════════════════════════════╗
║  CoPES — Coordinated Process Engine Substrate ║
║  Frank — Stationary. Sovereign. King.         ║
║  Phoenix DevOps OS | GPL v3                   ║
╚══════════════════════════════════════════════╝
    """)

    # Build the ring chain — Frank-0 is the only entry point
    frank0 = build_ring_chain()

    print(f"Ring chain online.")
    print(f"Status: {frank0.status()}")
    print()

    # --- Demo: write an internal import, build a packet, validate it ---

    print("[ DEMO ] Writing internal import for PID 1001...")
    record = frank0.write_import(process_id=1001, packet_type=PacketType.INTERNAL)
    print(f"  Import ID : {record.import_id}")
    print(f"  Token A1  : {record.token_a1.hex()[:16]}...")
    print()

    print("[ DEMO ] Building packet...")
    raw = build_packet(
        ring_origin=Ring.KERNEL,
        packet_type=PacketType.INTERNAL,
        token_a1=record.token_a1,
        token_b1=record.token_b1,
        process_id=1001,
        import_id=record.import_id,
        payload_a=b"Hello from Ring 0",
        payload_b=b"",
    )
    print(f"  Packet size: {len(raw)} bytes")
    print()

    print("[ DEMO ] Frank receiving packet (proxy validation)...")
    pkt = frank0.receive_packet(raw)
    if pkt:
        print(f"  Packet accepted. Payload: {pkt.payload_a}")
    print()

    print("[ DEMO ] Process completes — expiring import...")
    frank0.expire_import(record.import_id)
    print()

    print("[ DEMO ] Replaying packet with expired import (should be rejected)...")
    pkt2 = frank0.receive_packet(raw)
    if pkt2 is None:
        print("  Packet rejected. Import expired. Nothing left behind. ✓")
    print()

    print("[ DEMO ] Writing AI import for PID 2001...")
    ai_record = frank0.write_import(process_id=2001, packet_type=PacketType.AI)
    print(f"  AI Import ID: {ai_record.import_id}")
    print(f"  Helix PID   : {ai_record.helix_pid} (None if helix.py not yet built)")
    print()

    print("[ DEMO ] Simulating load increase — expanding Helix window...")
    frank0.adjust_window(ai_record.import_id, load_delta=3)
    print()

    print(f"Final status: {frank0.status()}")
    print()
    print("Frank is stationary. He is ready. Processes come to him.")
