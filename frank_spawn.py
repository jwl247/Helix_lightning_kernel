#!/usr/bin/env python3
"""
frank_spawn.py — Fixed Frank Spawner
Phoenix DevOps OS | jwl247 | GPL v3
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
from frank_ring import FrankRing, SuitSpec, SuitType, suit_for, SECTOR_MAP

SPAWN_VERSION = "1.1.0-fixed"

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
        channel_family = {1: DataFamily.SYSTEM, 2: DataFamily.PHYSICS, 3: DataFamily.NETWORK,
                         4: DataFamily.ASSETS, 5: DataFamily.USER, 6: DataFamily.AI,
                         7: DataFamily.NETWORK, 8: DataFamily.SYSTEM}
        return channel_family.get(self.channel, DataFamily.SYSTEM)

    def sector(self) -> int:
        channel_sector = {1: 4, 2: 1, 3: 3, 4: 3, 5: 2, 6: 2, 7: 4, 8: 4}
        return channel_sector.get(self.channel, 4)


class FrankSpawn:
    MAX_WORKERS = 32

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
        self._resolvers: list[Callable[[StagePacket], Optional[SuitSpec]]] = [self._default_resolver]
        log.info(f"FrankSpawn v{SPAWN_VERSION} — {self.MAX_WORKERS} workers ready")

    def register_resolver(self, fn: Callable[[StagePacket], Optional[SuitSpec]]):
        self._resolvers.insert(0, fn)

    def install(self):
        signal.signal(FrankSignal.STAGE_READY, self._on_interrupt)
        signal.signal(FrankSignal.RING_DONE, self._on_ring_done)
        log.info("FrankSpawn signal handlers installed")

    def _on_interrupt(self, signum, frame):
        try:
            self._interrupt_queue.put_nowait(time.monotonic())
        except queue.Full:
            log.warning("Interrupt queue full — dropping signal")

    def _on_ring_done(self, signum, frame):
        pass  # Can be extended later

    def loop(self):
        while self._alive:
            try:
                interrupt_time = self._interrupt_queue.get(timeout=0.05)
                self._handle_interrupt(interrupt_time)
            except queue.Empty:
                continue
            except Exception as e:
                log.error(f"Spawner loop error: {e}")

    def _handle_interrupt(self, interrupt_time: float):
        packets = self._drain_stages()
        for packet in packets:
            self._executor.submit(self._spawn_ring, packet, interrupt_time)

    def _drain_stages(self):
        packets = []
        for slot in range(8):
            try:
                raw = self.frank.bus.read_stage(slot)
                if not raw:
                    continue
                packet = self._unpack_stage(slot, raw)
                if packet:
                    packets.append(packet)
                self.frank.bus.write_stage(slot, b"")  # Atomic clear
            except Exception as e:
                log.debug(f"Drain error on slot {slot}: {e}")
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
            meta = json.loads(meta_b.rstrip(b'\x00').decode('utf-8', errors='replace')) if meta_b.strip() else {}
            meta.update({"seq": seq})
            return StagePacket(channel=channel, slot=slot, data=data, meta=meta)
        except Exception:
            return StagePacket(channel=slot + 1, slot=slot, data=raw, meta={})

    def _spawn_ring(self, packet: StagePacket, interrupt_time: float):
        start = time.monotonic()
        try:
            suit = self._resolve_suit(packet)
            if not suit:
                self.metrics.record(0, False)
                return

            ring = FrankRing(suit, self.frank)
            result = ring.ride(data=packet.data, channel=packet.channel)
            latency = (time.monotonic() - start) * 1000
            self.metrics.record(latency, True)
            return result
        except Exception as e:
            latency = (time.monotonic() - start) * 1000
            self.metrics.record(latency, False)
            log.error(f"Ring spawn failed on ch{packet.channel}: {e}")

    def _resolve_suit(self, packet: StagePacket) -> Optional[SuitSpec]:
        for resolver in self._resolvers:
            try:
                suit = resolver(packet)
                if suit:
                    return suit
            except Exception as e:
                log.debug(f"Resolver failed: {e}")
        return None

    def _default_resolver(self, packet: StagePacket) -> Optional[SuitSpec]:
        if self.library:
            spec = self.library.resolve(
                sector   = packet.sector(),
                ring_pos = packet.meta.get("ring_pos", 0),
                family   = packet.family(),
                data     = packet.data,
            )
            if spec:
                return spec
        return suit_for(
            sector   = packet.sector(),
            ring_pos = packet.meta.get("ring_pos", 0),
            suit_type = SuitType.PYTHON
        )

    def stop(self):
        self._alive = False
        self._executor.shutdown(wait=False)
        log.info("FrankSpawn stopped")


def start_spawn(frank: Optional[Frank5] = None, process_library=None) -> FrankSpawn:
    """Called by main_kernel.py"""
    spawner = FrankSpawn(frank=frank, process_library=process_library)
    spawner.install()
    t = threading.Thread(target=spawner.loop, daemon=True, name="frank-spawner")
    t.start()
    log.info("FrankSpawn background loop started")
    return spawner
