"""
frank_spawn.py — Frank5 Spawner Bridge (Generation 2 Polish)
Phoenix DevOps OS / CoPES Substrate | jwl247 | GPL v3

The bridge. Helix-I fires the interrupt. Frank_spawn catches it.
A live ring is running before Helix-I fires again. That is the ONLY job.

CHANGES IN THIS POLISH:
1. ATOMIC DRAIN: Clears shared memory instantly upon read to prevent duplicate ring spawns.
2. TYPESAFE STRUCTS: Validates binary boundaries securely before allocating task workers.
"""
import os
import sys
import time
import signal
import logging
import threading
import json
import queue
import struct
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor
from franken5 import (
    Frank5, get_frank, RingRecord, RingState, DataFamily, 
    FrankSignal, SHM_PATH, AUDIT_PATH
)
from frank_ring import FrankRing, SuitSpec, SuitType, wear, suit_for, SECTOR_MAP

SPAWN_VERSION = "1.0.0-polished-b"
log = logging.getLogger("frank_spawn")

@dataclass
class SpawnMetrics:
    total_spawns: int = 0
    successful: int = 0
    failed: int = 0
    total_latency_ms: float = 0.0
    min_latency_ms: float = float('inf')
    max_latency_ms: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, latency_ms: float, success: bool):
        with self._lock:
            self.total_spawns += 1
            if success:
                self.successful += 1
                self.total_latency_ms += latency_ms
                self.min_latency_ms = min(self.min_latency_ms, latency_ms)
                self.max_latency_ms = max(self.max_latency_ms, latency_ms)
            else:
                self.failed += 1

    @property
    def avg_latency_ms(self) -> float:
        if self.successful == 0:
            return 0.0
        return self.total_latency_ms / self.successful

    def report(self) -> dict:
        return {
            "total": self.total_spawns,
            "successful": self.successful,
            "failed": self.failed,
            "avg_ms": round(self.avg_latency_ms, 3),
            "min_ms": round(self.min_latency_ms, 3) if self.min_latency_ms != float('inf') else 0,
            "max_ms": round(self.max_latency_ms, 3),
        }

@dataclass
class StagePacket:
    channel: int
    slot: int
    data: bytes
    meta: dict
    arrived_at: float = field(default_factory=time.monotonic)

    @property
    def age_ms(self) -> float:
        return (time.monotonic() - self.arrived_at) * 1000

    def family(self) -> str:
        channel_family = {
            1: DataFamily.SYSTEM, 2: DataFamily.PHYSICS, 3: DataFamily.NETWORK,
            4: DataFamily.ASSETS, 5: DataFamily.USER, 6: DataFamily.AI,
            7: DataFamily.NETWORK, 8: DataFamily.SYSTEM,
        }
        return channel_family.get(self.channel, DataFamily.SYSTEM)

    def sector(self) -> int:
        channel_sector = {1: 4, 2: 1, 3: 3, 4: 3, 5: 2, 6: 2, 7: 4, 8: 4}
        return channel_sector.get(self.channel, 4)

class FrankSpawn:
    MAX_WORKERS = 64

    def __init__(self, frank: Optional[Frank5] = None, process_library=None):
        self.frank = frank or get_frank()
        self.library = process_library
        self.metrics = SpawnMetrics()
        self._alive = True
        self._lock = threading.Lock()
        self._interrupt_queue: queue.Queue = queue.Queue(maxsize=256)
        self._executor = ThreadPoolExecutor(
            max_workers=self.MAX_WORKERS,
            thread_name_prefix="frank-ring"
        )
        self._resolvers: list[Callable[[StagePacket], Optional[SuitSpec]]] = []
        self._resolvers.append(self._default_resolver)
        log.info(f"FrankSpawn v{SPAWN_VERSION} active — 64 workers online.")

    def register_resolver(self, fn: Callable[[StagePacket], Optional[SuitSpec]]):
        self._resolvers.insert(0, fn)

    def install(self):
        signal.signal(FrankSignal.STAGE_READY, self._on_interrupt)
        signal.signal(FrankSignal.RING_DONE, self._on_ring_done)
        log.info("CoPES Spawner Signal Handlers Installed Securely.")

    def _on_interrupt(self, signum, frame):
        try:
            self._interrupt_queue.put_nowait(time.monotonic())
        except queue.Full:
            log.warning("Interrupt container overrun — dropping packet frame.")

    def _on_ring_done(self, signum, frame):
        pass

    def loop(self):
        while self._alive:
            try:
                interrupt_time = self._interrupt_queue.get(timeout=0.001)
                self._handle_interrupt(interrupt_time)
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"Execution boundary error: {e}")

    def _handle_interrupt(self, interrupt_time: float):
        packets = self._drain_stages()
        if not packets:
            return
        for packet in packets:
            self._executor.submit(self._spawn_ring, packet, interrupt_time)

    def _drain_stages(self) -> list[StagePacket]:
        """POLISHED MECHANIC: Reads AND wipes memory immediately to break execution races."""
        packets = []
        for slot in range(8):
            try:
                raw = self.frank.bus.read_stage(slot)
                if not raw:
                    continue
                
                # Unpack the packet safely into local thread memory context
                packet = self._unpack_stage(slot, raw)
                if packet:
                    packets.append(packet)
                    
                # Flush the shared memory footprint immediately so Helix-I can refill it
                self.frank.bus.write_stage(slot, b"")
            except Exception as e:
                log.debug(f"Drain clearance collision on slot {slot}: {e}")
        return packets

    def _unpack_stage(self, slot: int, raw: bytes) -> Optional[StagePacket]:
        HEADER_SIZE = struct.calcsize("!4sBBHI")
        if len(raw) < HEADER_SIZE:
            return StagePacket(channel=slot + 1, slot=slot, data=raw, meta={})
        try:
            magic, channel, strand, data_len, seq = struct.unpack("!4sBBHI", raw[:HEADER_SIZE])
            if magic != b"HISX":
                return StagePacket(channel=slot + 1, slot=slot, data=raw, meta={})
            
            data = raw[HEADER_SIZE:HEADER_SIZE + data_len]
            meta_b = raw[HEADER_SIZE + data_len:]
            
            # Defensive clean separation of JSON metadata array strings
            if b'\x00' in meta_b:
                meta_b = meta_b.split(b'\x00', 1)[0]
            meta = json.loads(meta_b.decode('utf-8')) if meta_b.strip() else {}
            
            meta.update({"seq": seq, "strand": strand})
            return StagePacket(channel=channel, slot=slot, data=data, meta=meta)
        except Exception as e:
            log.debug(f"Metadata structural parsing unmarshal bypass on slot {slot}: {e}")
            return None

    def _spawn_ring(self, packet: StagePacket, interrupt_time: float):
        spawn_start = time.monotonic()
        try:
            suit = self._resolve_suit(packet)
            if not suit:
                self.metrics.record(0, False)
                return

            pre_spawn_ms = (time.monotonic() - interrupt_time) * 1000
            ring = FrankRing(suit, self.frank)
            result = ring.ride(data=packet.data, channel=packet.channel)
            
            total_ms = (time.monotonic() - spawn_start) * 1000
            self.metrics.record(total_ms, True)
            return result
        except Exception as e:
            total_ms = (time.monotonic() - spawn_start) * 1000
            self.metrics.record(total_ms, False)
            log.error(f"Execution ring fork failure on ch{packet.channel}: {e}")

    def _resolve_suit(self, packet: StagePacket) -> Optional[SuitSpec]:
        for resolver in self._resolvers:
            try:
                suit = resolver(packet)
                if suit:
                    return suit
            except Exception as e:
                log.debug(f"Resolver loop bypass: {e}")
        return None

    def _default_resolver(self, packet: StagePacket) -> Optional[SuitSpec]:
        sector = packet.sector()
        ring_pos = packet.meta.get("ring_pos", 0)
        family = packet.family()

        if self.library:
            suit = self.library.resolve(sector=sector, ring_pos=ring_pos, family=family, data=packet.data)
            if suit:
                return suit

        return suit_for(
            sector=sector, ring_pos=ring_pos, suit_type=SuitType.PYTHON,
            entry=SECTOR_MAP.get(sector, {}).get("rings", {}).get(ring_pos, ""),
        )

    def stop(self):
        self._alive = False
        self._executor.shutdown(wait=False)
        log.info("CoPES Spawner Gateway Decommissioned Cleanly.")
