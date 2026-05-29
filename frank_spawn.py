#!/usr/bin/env python3
"""
frank_spawn.py — The Spawn Handshake
Phoenix DevOps OS / Helix Lightning Kernel | jwl247 | GPL v3

Frank_spawn is the bridge.
Helix-I fires the interrupt.
Frank_spawn catches it.
A live ring is running before Helix-I fires again.

That is the ONLY job.

Speed is everything here.
No blocking. No waiting. No I/O on the critical path.
The process library is already in shared memory.
The suit is already hanging in the closet.
Frank just reaches in and puts it on.

Interrupt → suit identified → ring live.
As fast as Python can move.
"""

import os
import sys
import time
import signal
import logging
import threading
import json
import queue
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor

from franken5 import (
    Frank5, get_frank, RingRecord, RingState,
    DataFamily, FrankSignal,
    SHM_PATH, AUDIT_PATH
)
from frank_ring import FrankRing, SuitSpec, SuitType, wear, suit_for, SECTOR_MAP

SPAWN_VERSION = "1.0.0-alpha"

log = logging.getLogger("frank_spawn")


# =============================================================================
# Spawn metrics — how fast are we?
# =============================================================================

@dataclass
class SpawnMetrics:
    total_spawns:     int   = 0
    successful:       int   = 0
    failed:           int   = 0
    total_latency_ms: float = 0.0
    min_latency_ms:   float = float('inf')
    max_latency_ms:   float = 0.0
    _lock: threading.Lock   = field(default_factory=threading.Lock)

    def record(self, latency_ms: float, success: bool):
        with self._lock:
            self.total_spawns += 1
            if success:
                self.successful += 1
                self.total_latency_ms += latency_ms
                self.min_latency_ms    = min(self.min_latency_ms, latency_ms)
                self.max_latency_ms    = max(self.max_latency_ms, latency_ms)
            else:
                self.failed += 1

    @property
    def avg_latency_ms(self) -> float:
        if self.successful == 0:
            return 0.0
        return self.total_latency_ms / self.successful

    def report(self) -> dict:
        return {
            "total":       self.total_spawns,
            "successful":  self.successful,
            "failed":      self.failed,
            "avg_ms":      round(self.avg_latency_ms, 3),
            "min_ms":      round(self.min_latency_ms, 3) if self.min_latency_ms != float('inf') else 0,
            "max_ms":      round(self.max_latency_ms, 3),
        }


# =============================================================================
# Stage reader — reads what Helix-I put in shared memory
# =============================================================================

@dataclass
class StagePacket:
    """
    What Helix-I left in shared memory.
    Frank_spawn reads this and decides which suit to wear.
    """
    channel:    int
    slot:       int
    data:       bytes
    meta:       dict
    arrived_at: float = field(default_factory=time.monotonic)

    @property
    def age_ms(self) -> float:
        return (time.monotonic() - self.arrived_at) * 1000

    def family(self) -> str:
        """Derive data family from channel assignment."""
        channel_family = {
            1: DataFamily.SYSTEM,
            2: DataFamily.PHYSICS,
            3: DataFamily.NETWORK,
            4: DataFamily.ASSETS,
            5: DataFamily.USER,
            6: DataFamily.AI,
            7: DataFamily.NETWORK,
            8: DataFamily.SYSTEM,
        }
        return channel_family.get(self.channel, DataFamily.SYSTEM)

    def sector(self) -> int:
        """Derive target sector from channel."""
        channel_sector = {
            1: 4,   # core engine
            2: 1,   # boot/kernel
            3: 3,   # comms
            4: 3,   # comms
            5: 2,   # intake
            6: 2,   # intake
            7: 4,   # core engine
            8: 4,   # core engine
        }
        return channel_sector.get(self.channel, 4)


# =============================================================================
# Frank Spawn — the handshake
# =============================================================================

class FrankSpawn:
    """
    The bridge between Helix-I and a live ring.

    Helix-I fires SIGUSR1.
    FrankSpawn catches it in under a millisecond.
    Reads the stage from shared memory.
    Identifies the suit.
    Spawns the ring.
    Hands it to the executor.
    Already listening for the next interrupt.

    Non-blocking. Always ready. Never the bottleneck.
    """

    # How many rings can be in-flight simultaneously
    # 16 rings (4 sectors x 4 rings) x 4 concurrent = 64 max
    MAX_WORKERS = 64

    def __init__(self, frank: Optional[Frank5] = None,
                 process_library=None):
        self.frank    = frank or get_frank()
        self.library  = process_library   # set after process_library.py exists
        self.metrics  = SpawnMetrics()
        self._alive   = True
        self._lock    = threading.Lock()

        # The interrupt queue — signal handler puts channel here
        # spawn loop reads from here — never blocks the signal handler
        self._interrupt_queue: queue.Queue = queue.Queue(maxsize=256)

        # Thread pool — rings run here, never blocking the spawn loop
        self._executor = ThreadPoolExecutor(
            max_workers = self.MAX_WORKERS,
            thread_name_prefix = "frank-ring"
        )

        # Registered suit resolvers — how to find the right suit
        # for a given stage packet
        self._resolvers: list[Callable[[StagePacket], Optional[SuitSpec]]] = []

        # Default resolver — sector map
        self._resolvers.append(self._default_resolver)

        log.info(
            f"FrankSpawn v{SPAWN_VERSION} ready — "
            f"{self.MAX_WORKERS} workers — "
            f"interrupt queue depth 256"
        )

    def register_resolver(self, fn: Callable[[StagePacket], Optional[SuitSpec]]):
        """
        Register a custom suit resolver.
        Called before the default resolver.
        Return a SuitSpec or None to fall through to the next resolver.
        """
        self._resolvers.insert(0, fn)

    def install(self):
        """
        Install signal handlers.
        SIGUSR1 from Helix-I goes straight to the interrupt queue.
        Non-blocking. Returns immediately.
        """
        signal.signal(FrankSignal.STAGE_READY, self._on_interrupt)
        signal.signal(FrankSignal.RING_DONE,   self._on_ring_done)
        log.info("FrankSpawn signal handlers installed")

    def _on_interrupt(self, signum, frame):
        """
        SIGUSR1 fired by Helix-I.
        This runs in the signal handler — must be FAST.
        Just put the timestamp in the queue and return.
        The spawn loop reads it and does the real work.
        """
        try:
            self._interrupt_queue.put_nowait(time.monotonic())
        except queue.Full:
            log.warning("Interrupt queue full — stage dropped")

    def _on_ring_done(self, signum, frame):
        """SIGUSR2 — a ring finished. Frank-core handles the cleanup."""
        pass   # Frank5._on_ring_done handles this

    def loop(self):
        """
        The spawn loop.
        Reads interrupts from the queue.
        Reads stages from shared memory.
        Spawns rings in the thread pool.
        Never blocks. Never sleeps longer than 1ms.
        Runs until shutdown.
        """
        log.info("FrankSpawn loop running — listening for Helix-I")

        while self._alive:
            try:
                # Block for up to 1ms — then check _alive and loop
                interrupt_time = self._interrupt_queue.get(timeout=0.001)
                queue_latency  = (time.monotonic() - interrupt_time) * 1000
                log.debug(f"Interrupt dequeued — queue latency {queue_latency:.3f}ms")
                self._handle_interrupt(interrupt_time)
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"Spawn loop error: {e}")

    def _handle_interrupt(self, interrupt_time: float):
        """
        Interrupt arrived. Read ALL pending stages from shared memory.
        Spawn a ring for each one. Hand to executor. Done.
        """
        # Drain all pending stages — one interrupt may cover multiple channels
        packets = self._drain_stages()

        if not packets:
            log.debug("Interrupt with no staged data — skipped")
            return

        for packet in packets:
            self._executor.submit(self._spawn_ring, packet, interrupt_time)

    def _drain_stages(self) -> list[StagePacket]:
        """
        Read all non-empty stage slots from shared memory.
        Helix-I may have staged data on multiple channels.
        Read them all in one pass. Fast.
        """
        packets = []

        # Check all 8 Helix-I channels (slots 0-7)
        for slot in range(8):
            try:
                raw = self.frank.bus.read_stage(slot)
                if not raw:
                    continue
                packet = self._unpack_stage(slot, raw)
                if packet:
                    packets.append(packet)
            except Exception as e:
                log.debug(f"Stage read slot {slot}: {e}")

        return packets

    def _unpack_stage(self, slot: int, raw: bytes) -> Optional[StagePacket]:
        """Unpack a stage written by Helix-I."""
        import struct
        HEADER_SIZE = struct.calcsize("!4sBBHI")
        if len(raw) < HEADER_SIZE:
            return StagePacket(
                channel = slot + 1,
                slot    = slot,
                data    = raw,
                meta    = {}
            )
        try:
            magic, channel, strand, data_len, seq = struct.unpack(
                "!4sBBHI", raw[:HEADER_SIZE]
            )
            if magic != b"HISX":
                return StagePacket(
                    channel = slot + 1,
                    slot    = slot,
                    data    = raw,
                    meta    = {}
                )
            data   = raw[HEADER_SIZE:HEADER_SIZE + data_len]
            meta_b = raw[HEADER_SIZE + data_len:]
            try:
                meta = json.loads(meta_b.rstrip(b'\x00')) if meta_b.strip(b'\x00') else {}
            except Exception:
                meta = {}
            meta.update({"seq": seq, "strand": strand})
            return StagePacket(
                channel = channel,
                slot    = slot,
                data    = data,
                meta    = meta
            )
        except Exception as e:
            log.debug(f"Stage unpack error slot {slot}: {e}")
            return None

    def _spawn_ring(self, packet: StagePacket, interrupt_time: float):
        """
        Find the right suit. Spawn the ring. Ride.
        This runs in the thread pool — never on the main thread.
        """
        spawn_start = time.monotonic()

        try:
            # Resolve the suit
            suit = self._resolve_suit(packet)
            if not suit:
                log.warning(
                    f"No suit found for ch{packet.channel} "
                    f"sector{packet.sector()} — stage dropped"
                )
                self.metrics.record(0, False)
                return

            # Total latency from interrupt to ring live
            pre_spawn_ms = (time.monotonic() - interrupt_time) * 1000
            log.debug(
                f"Suit resolved: {suit.name} — "
                f"{pre_spawn_ms:.3f}ms since interrupt"
            )

            # Ride — mount, run, sync, die
            ring = FrankRing(suit, self.frank)
            result = ring.ride(
                data    = packet.data,
                channel = packet.channel,
            )

            # Total spawn-to-done latency
            total_ms = (time.monotonic() - spawn_start) * 1000
            self.metrics.record(total_ms, True)

            log.info(
                f"Ring complete — {suit.name} — "
                f"{total_ms:.1f}ms total — "
                f"interrupt→live: {pre_spawn_ms:.3f}ms"
            )

            # Clear the stage slot — Helix-I can reuse it
            try:
                self.frank.bus.write_stage(packet.slot, b"")
            except Exception:
                pass

            return result

        except Exception as e:
            total_ms = (time.monotonic() - spawn_start) * 1000
            self.metrics.record(total_ms, False)
            log.error(f"Spawn failed ch{packet.channel}: {e}")

    def _resolve_suit(self, packet: StagePacket) -> Optional[SuitSpec]:
        """
        Walk the resolver chain until we get a suit.
        First resolver to return a SuitSpec wins.
        Default resolver uses the sector map.
        """
        for resolver in self._resolvers:
            try:
                suit = resolver(packet)
                if suit:
                    return suit
            except Exception as e:
                log.debug(f"Resolver error: {e}")
        return None

    def _default_resolver(self, packet: StagePacket) -> Optional[SuitSpec]:
        """
        Default suit resolver — uses channel → sector → ring_pos mapping.
        Picks ring_pos 0 by default.
        Real resolution comes from the process library.
        """
        sector   = packet.sector()
        ring_pos = packet.meta.get("ring_pos", 0)
        family   = packet.family()

        # If process library is loaded use it
        if self.library:
            suit = self.library.resolve(
                sector   = sector,
                ring_pos = ring_pos,
                family   = family,
                data     = packet.data,
            )
            if suit:
                return suit

        # Fallback — bare suit from sector map
        return suit_for(
            sector    = sector,
            ring_pos  = ring_pos,
            suit_type = SuitType.PYTHON,
            entry     = SECTOR_MAP.get(sector, {})
                            .get("rings", {})
                            .get(ring_pos, ""),
        )

    def stop(self):
        """Shutdown the spawn loop and executor."""
        self._alive = False
        self._executor.shutdown(wait=False)
        log.info(f"FrankSpawn stopped — {self.metrics.report()}")

    def status(self) -> dict:
        return {
            "version":        SPAWN_VERSION,
            "alive":          self._alive,
            "queue_depth":    self._interrupt_queue.qsize(),
            "workers":        self.MAX_WORKERS,
            "metrics":        self.metrics.report(),
        }


# =============================================================================
# Convenience — start the spawn loop in a background thread
# =============================================================================

def start_spawn(frank: Optional[Frank5] = None,
                process_library=None) -> FrankSpawn:
    """
    Start FrankSpawn in a background thread.
    Returns the spawner so you can check status or stop it.

    Usage:
        spawner = start_spawn(frank)
        # Helix-I fires interrupts → rings spawn automatically
        # spawner.status() to see metrics
        # spawner.stop() to shut down
    """
    spawner = FrankSpawn(frank=frank, process_library=process_library)
    spawner.install()

    t = threading.Thread(
        target    = spawner.loop,
        daemon    = True,
        name      = "frank-spawn-loop"
    )
    t.start()

    log.info("FrankSpawn running in background")
    return spawner


# =============================================================================
# Demo
# =============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level   = logging.INFO,
        format  = "%(asctime)s [SPAWN] %(levelname)s %(message)s",
        handlers= [logging.StreamHandler()]
    )

    frank   = get_frank()
    frank.boot()
    spawner = start_spawn(frank)

    print("\n" + "="*60)
    print("FRANK SPAWN DEMO")
    print("Simulating Helix-I interrupts directly")
    print("="*60 + "\n")

    # Simulate what Helix-I does — write a stage and fire the interrupt
    import struct

    def simulate_helix_i(channel: int, message: str, slot: int = 0):
        data    = message.encode()
        strand  = 0x41 if channel <= 2 else 0x42
        header  = struct.pack("!4sBBHI", b"HISX", channel, strand, len(data), 0)
        payload = header + data
        frank.bus.write_stage(slot, payload)
        os.kill(os.getpid(), signal.SIGUSR1)
        log.info(f"Simulated Helix-I ch{channel}: '{message}'")

    # Fire 4 interrupts — one per sector
    time.sleep(0.1)  # let spawner settle

    simulate_helix_i(1, "system boot sequence",    slot=0)
    time.sleep(0.05)
    simulate_helix_i(3, "network comms init",      slot=2)
    time.sleep(0.05)
    simulate_helix_i(5, "user intake request",     slot=4)
    time.sleep(0.05)
    simulate_helix_i(7, "core engine pulse",       slot=6)

    # Let rings complete
    time.sleep(1.0)

    print("\n" + "="*60)
    print("SPAWN METRICS")
    status = spawner.status()
    m = status["metrics"]
    print(f"  Total spawns:  {m['total']}")
    print(f"  Successful:    {m['successful']}")
    print(f"  Failed:        {m['failed']}")
    print(f"  Avg latency:   {m['avg_ms']}ms")
    print(f"  Min latency:   {m['min_ms']}ms")
    print(f"  Max latency:   {m['max_ms']}ms")
    print("="*60)

    spawner.stop()
    frank.shutdown()
