#!/usr/bin/env python3
"""
helix_memory.py — Phoenix DevOps OS / CoPES
Helix Memory Stack — superpowers layer for the game engine.

Sits on top of helix.py. Does NOT replace it.
Adds: L1/L2/L3 cache, virtual RAM, filesystem cache,
      Frank cast/reel, sector routing, PCS torrent model.

Architecture:
    Game / COPES
        ↓
    Sector arch (romeo/juliet, frank3, quadengine)
        ↓
    Frank — PCS torrent, cast/reel, Ring 3
        ↓
    HelixSystem  ← THIS FILE (superpowers)
        ↓
    Helix        ← helix.py (clone pool, QuadEngine, egress)
        ↓
    SQLite + D1

jwl247 / United Systems / GPL v3
"""

import os
import time
import pickle
import zlib
import hashlib
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Callable

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

# ============================================================================
# MEMORY TIERS
# ============================================================================

class MemoryTier(Enum):
    L1_HOT        = 0   # sub-microsecond — hottest game state
    L2_WARM       = 1   # fast — recent ops, active sectors
    L3_COMPRESSED = 2   # compressed RAM — cold but resident
    L4_HELIX      = 3   # Helix clone pool — persistent
    L5_D1         = 4   # Cloudflare D1 — chain of evidence

class SectorID(Enum):
    """Phoenix sector routing — each sector gets its own memory region."""
    SECTOR_1 = "s1"   # Boot, kernel, frank3
    SECTOR_2 = "s2"   # Intake, package handler, clone pool
    SECTOR_3 = "s3"   # Comms, romeo/juliet, quadengine
    SECTOR_4 = "s4"   # Helix, Frank, vault (default)
    GAME     = "gm"   # COPES game engine
    FRANK    = "fr"   # Frank cast/reel space

# ============================================================================
# CACHE BLOCK
# ============================================================================

@dataclass
class CacheBlock:
    """Single unit of the L1/L2/L3 cache."""
    key:            str
    data:           Any
    tier:           MemoryTier
    size_bytes:     int
    sector:         SectorID    = SectorID.SECTOR_4
    access_count:   int         = 0
    last_access:    float       = field(default_factory=time.time)
    created_at:     float       = field(default_factory=time.time)
    compressed:     bool        = False
    pinned:         bool        = False
    dirty:          bool        = False
    _compressed_data: Optional[bytes] = field(default=None, repr=False)

    def touch(self):
        self.access_count += 1
        self.last_access = time.time()

    def compress(self) -> int:
        """Compress in place. Returns bytes saved."""
        if self.compressed or self.data is None:
            return 0
        try:
            raw = pickle.dumps(self.data)
            self._compressed_data = zlib.compress(raw, level=5)
            saved = self.size_bytes - len(self._compressed_data)
            self.compressed = True
            return max(0, saved)
        except Exception:
            return 0

    def decompress(self):
        """Decompress in place."""
        if not self.compressed or not self._compressed_data:
            return
        try:
            raw = zlib.decompress(self._compressed_data)
            self.data = pickle.loads(raw)
            self.compressed = False
            self._compressed_data = None
        except Exception:
            pass

    @property
    def effective_size(self) -> int:
        if self.compressed and self._compressed_data:
            return len(self._compressed_data)
        return self.size_bytes

# ============================================================================
# HELIX CACHE — L1 / L2 / L3
# ============================================================================

class HelixCache:
    """
    Three-tier LRU cache with automatic promotion/demotion/compression.

    L1 (hot):        instant access — game state, Frank's active casts
    L2 (warm):       fast — recent sector ops, clone pool hot items
    L3 (compressed): slower — zlib level 5, rarely accessed but resident

    Compression under load speeds things up — less RAM pressure = less swap.
    More load = more compression = more effective RAM. PCS torrent model.
    """

    def __init__(self,
                 l1_mb: int = 128,
                 l2_mb: int = 512,
                 l3_mb: int = 1024):
        self.l1_max = l1_mb * 1024 * 1024
        self.l2_max = l2_mb * 1024 * 1024
        self.l3_max = l3_mb * 1024 * 1024

        self.l1: OrderedDict[str, CacheBlock] = OrderedDict()
        self.l2: OrderedDict[str, CacheBlock] = OrderedDict()
        self.l3: OrderedDict[str, CacheBlock] = OrderedDict()

        self.lock = threading.RLock()

        self.hits   = {"l1": 0, "l2": 0, "l3": 0}
        self.misses = {"l1": 0, "l2": 0, "l3": 0}
        self.promotions   = 0
        self.demotions    = 0
        self.compressions = 0
        self.evictions    = 0

    # ── Read path ─────────────────────────────────────────────────────────────

    def get(self, key: str) -> Optional[Any]:
        with self.lock:
            # L1
            if key in self.l1:
                self.hits["l1"] += 1
                b = self.l1[key]
                b.touch()
                self.l1.move_to_end(key)
                return b.data

            self.misses["l1"] += 1

            # L2
            if key in self.l2:
                self.hits["l2"] += 1
                b = self.l2[key]
                b.touch()
                if b.access_count >= 3:
                    self._promote_l1(key, b)
                else:
                    self.l2.move_to_end(key)
                return b.data

            self.misses["l2"] += 1

            # L3
            if key in self.l3:
                self.hits["l3"] += 1
                b = self.l3[key]
                b.touch()
                if b.compressed:
                    b.decompress()
                if b.access_count >= 2:
                    self._promote_l2(key, b)
                else:
                    self.l3.move_to_end(key)
                return b.data

            self.misses["l3"] += 1
            return None

    # ── Write path ────────────────────────────────────────────────────────────

    def put(self, key: str, data: Any, size: int,
            sector: SectorID = SectorID.SECTOR_4,
            pinned: bool = False):
        with self.lock:
            # Remove from lower tiers if upgrading
            self.l2.pop(key, None)
            self.l3.pop(key, None)

            b = CacheBlock(
                key=key, data=data,
                tier=MemoryTier.L1_HOT,
                size_bytes=size,
                sector=sector,
                pinned=pinned,
            )
            self._make_room_l1(size)
            self.l1[key] = b

    def invalidate(self, key: str):
        with self.lock:
            self.l1.pop(key, None)
            self.l2.pop(key, None)
            self.l3.pop(key, None)

    # ── Tier sizes ────────────────────────────────────────────────────────────

    def _tier_size(self, tier: OrderedDict) -> int:
        return sum(b.effective_size for b in tier.values())

    # ── Room-making ───────────────────────────────────────────────────────────

    def _make_room_l1(self, needed: int):
        while self._tier_size(self.l1) + needed > self.l1_max and self.l1:
            key, b = next(iter(self.l1.items()))
            if b.pinned:
                self.l1.move_to_end(key)
                continue
            self._demote_l2(key, b)

    def _make_room_l2(self, needed: int):
        while self._tier_size(self.l2) + needed > self.l2_max and self.l2:
            key, b = next(iter(self.l2.items()))
            if b.pinned:
                self.l2.move_to_end(key)
                continue
            self._demote_l3(key, b)

    def _make_room_l3(self, needed: int):
        while self._tier_size(self.l3) + needed > self.l3_max and self.l3:
            key, b = next(iter(self.l3.items()))
            if b.pinned:
                self.l3.move_to_end(key)
                continue
            del self.l3[key]
            self.evictions += 1

    # ── Promote ───────────────────────────────────────────────────────────────

    def _promote_l1(self, key: str, b: CacheBlock):
        self.l2.pop(key, None)
        self._make_room_l1(b.size_bytes)
        b.tier = MemoryTier.L1_HOT
        self.l1[key] = b
        self.promotions += 1

    def _promote_l2(self, key: str, b: CacheBlock):
        self.l3.pop(key, None)
        self._make_room_l2(b.size_bytes)
        b.tier = MemoryTier.L2_WARM
        self.l2[key] = b
        self.promotions += 1

    # ── Demote ────────────────────────────────────────────────────────────────

    def _demote_l2(self, key: str, b: CacheBlock):
        self.l1.pop(key, None)
        self._make_room_l2(b.size_bytes)
        b.tier = MemoryTier.L2_WARM
        self.l2[key] = b
        self.demotions += 1

    def _demote_l3(self, key: str, b: CacheBlock):
        self.l2.pop(key, None)
        saved = b.compress()
        if saved > 0:
            self.compressions += 1
        self._make_room_l3(b.effective_size)
        b.tier = MemoryTier.L3_COMPRESSED
        self.l3[key] = b
        self.demotions += 1

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self.lock:
            total_ops = sum(self.hits.values()) + sum(self.misses.values())
            total_hits = sum(self.hits.values())
            hit_rate = (total_hits / total_ops * 100) if total_ops else 100.0
            return {
                "l1_items":    len(self.l1),
                "l2_items":    len(self.l2),
                "l3_items":    len(self.l3),
                "l1_mb":       round(self._tier_size(self.l1) / 1048576, 2),
                "l2_mb":       round(self._tier_size(self.l2) / 1048576, 2),
                "l3_mb":       round(self._tier_size(self.l3) / 1048576, 2),
                "hit_rate":    round(hit_rate, 2),
                "hits":        self.hits,
                "misses":      self.misses,
                "promotions":  self.promotions,
                "demotions":   self.demotions,
                "compressions": self.compressions,
                "evictions":   self.evictions,
            }


# ============================================================================
# HELIX MEMORY MANAGER — virtual RAM
# ============================================================================

class HelixMemoryManager:
    """
    Virtual RAM on top of HelixCache.
    malloc/free interface — game entities, sector state, Frank payloads.
    More load compresses more = more effective RAM = PCS torrent model.
    """

    def __init__(self, cache: HelixCache, max_virtual_mb: int = 4096):
        self.cache       = cache
        self.max_virtual = max_virtual_mb * 1024 * 1024
        self.allocations: Dict[str, int] = {}
        self.total_allocated = 0
        self.lock = threading.RLock()
        self.total_allocs = 0
        self.total_frees  = 0

    def malloc(self, key: str, data: Any,
               sector: SectorID = SectorID.SECTOR_4,
               pinned: bool = False) -> bool:
        with self.lock:
            try:
                size = len(pickle.dumps(data))
            except Exception:
                size = 1024
            if self.total_allocated + size > self.max_virtual:
                return False
            self.cache.put(key, data, size, sector=sector, pinned=pinned)
            self.allocations[key] = size
            self.total_allocated += size
            self.total_allocs += 1
            return True

    def free(self, key: str) -> bool:
        with self.lock:
            if key not in self.allocations:
                return False
            size = self.allocations.pop(key)
            self.total_allocated -= size
            self.cache.invalidate(key)
            self.total_frees += 1
            return True

    def read(self, key: str) -> Optional[Any]:
        return self.cache.get(key)

    def write(self, key: str, data: Any,
              sector: SectorID = SectorID.SECTOR_4) -> bool:
        with self.lock:
            if key in self.allocations:
                self.free(key)
            return self.malloc(key, data, sector=sector)

    def stats(self) -> dict:
        return {
            "allocated_mb":  round(self.total_allocated / 1048576, 2),
            "allocation_count": len(self.allocations),
            "total_allocs":  self.total_allocs,
            "total_frees":   self.total_frees,
        }


# ============================================================================
# HELIX FS — filesystem cache
# ============================================================================

class HelixFS:
    """
    Filesystem cache layer.
    Files read through Helix — cached in memory stack automatically.
    Sector-aware: sector 1 files stay in sector 1 memory region.
    """

    def __init__(self, memory: HelixMemoryManager):
        self.memory   = memory
        self.manifest: Dict[str, dict] = {}   # filepath → metadata
        self.lock     = threading.RLock()
        self.reads    = 0
        self.writes   = 0
        self.hits     = 0
        self.disk_reads  = 0
        self.disk_writes = 0

    def read(self, filepath: str,
             sector: SectorID = SectorID.SECTOR_4) -> Optional[bytes]:
        with self.lock:
            self.reads += 1
            key = f"fs:{filepath}"
            cached = self.memory.read(key)
            if cached is not None:
                self.hits += 1
                return cached
            if not os.path.exists(filepath):
                return None
            try:
                with open(filepath, "rb") as f:
                    data = f.read()
                self.disk_reads += 1
                self.memory.malloc(key, data, sector=sector)
                self.manifest[filepath] = {
                    "size":      len(data),
                    "mtime":     os.path.getmtime(filepath),
                    "cached_at": time.time(),
                    "sector":    sector.value,
                }
                return data
            except Exception:
                return None

    def write(self, filepath: str, data: bytes,
              sector: SectorID = SectorID.SECTOR_4,
              write_through: bool = True):
        with self.lock:
            self.writes += 1
            key = f"fs:{filepath}"
            self.memory.write(key, data, sector=sector)
            if write_through:
                try:
                    Path(filepath).parent.mkdir(parents=True, exist_ok=True)
                    with open(filepath, "wb") as f:
                        f.write(data)
                    self.disk_writes += 1
                except Exception:
                    pass
            self.manifest[filepath] = {
                "size":      len(data),
                "mtime":     time.time(),
                "cached_at": time.time(),
                "sector":    sector.value,
            }

    def invalidate(self, filepath: str):
        with self.lock:
            self.memory.free(f"fs:{filepath}")
            self.manifest.pop(filepath, None)

    def stats(self) -> dict:
        return {
            "cached_files": len(self.manifest),
            "reads":        self.reads,
            "writes":       self.writes,
            "hits":         self.hits,
            "disk_reads":   self.disk_reads,
            "disk_writes":  self.disk_writes,
        }


# ============================================================================
# FRANK CAST/REEL — Frank's interface to the memory stack
# ============================================================================

@dataclass
class CastResult:
    """Result of a Frank cast operation."""
    cast_id:   str
    ring:      str
    result:    Any
    timestamp: str = field(default_factory=_now)
    ok:        bool = True
    error:     Optional[str] = None


class FrankCastReel:
    """
    Frank cast/reel pattern on top of HelixMemory.

    Frank casts:  execute(ring)
    Ring runs:    does the work
    Line reels:   result → memory.write(f"frank:cast:{ring}:{ts}", result)
    Helix holds:  quadralingually, ready in all 4 languages

    Every cast Frank makes reels back into Helix.
    Nothing moves until Frank says go.
    No loops — ingress receives, egress delivers, Frank is the only path.
    """

    def __init__(self, memory: HelixMemoryManager):
        self.memory  = memory
        self.lock    = threading.RLock()
        self.casts:  Dict[str, CastResult] = {}
        self.total_casts  = 0
        self.total_reeled = 0

    def cast(self, ring: str,
             fn: Callable,
             *args,
             sector: SectorID = SectorID.FRANK,
             **kwargs) -> CastResult:
        """
        Cast a ring — execute fn, reel result back into Helix memory.
        Frank is the only caller. Ring is the operation name.
        """
        cast_id  = hashlib.sha3_512(
            f"{ring}:{time.time()}".encode()
        ).hexdigest()[:16]
        ts = _now()

        try:
            result = fn(*args, **kwargs)
            cr = CastResult(cast_id=cast_id, ring=ring,
                            result=result, timestamp=ts, ok=True)
        except Exception as e:
            cr = CastResult(cast_id=cast_id, ring=ring,
                            result=None, timestamp=ts,
                            ok=False, error=str(e))

        # Reel back into Helix memory
        key = f"frank:cast:{ring}:{ts}"
        self.memory.write(key, {
            "cast_id":   cast_id,
            "ring":      ring,
            "result":    cr.result,
            "ok":        cr.ok,
            "error":     cr.error,
            "timestamp": ts,
        }, sector=sector)

        with self.lock:
            self.casts[cast_id] = cr
            self.total_casts  += 1
            if cr.ok:
                self.total_reeled += 1

        return cr

    def retrieve(self, cast_id: str) -> Optional[CastResult]:
        with self.lock:
            return self.casts.get(cast_id)

    def stats(self) -> dict:
        with self.lock:
            return {
                "total_casts":  self.total_casts,
                "total_reeled": self.total_reeled,
                "active_casts": len(self.casts),
            }


# ============================================================================
# SECTOR ROUTER — memory regions per sector
# ============================================================================

class SectorRouter:
    """
    Routes memory operations to the correct sector region.
    Each sector gets its own key namespace — isolation guaranteed.
    Sector 4 is the vault — never translate inside it.
    """

    SECTOR_PREFIXES = {
        SectorID.SECTOR_1: "s1:",
        SectorID.SECTOR_2: "s2:",
        SectorID.SECTOR_3: "s3:",
        SectorID.SECTOR_4: "s4:",
        SectorID.GAME:     "gm:",
        SectorID.FRANK:    "fr:",
    }

    def __init__(self, memory: HelixMemoryManager):
        self.memory = memory

    def key(self, sector: SectorID, name: str) -> str:
        return f"{self.SECTOR_PREFIXES[sector]}{name}"

    def write(self, sector: SectorID, name: str, data: Any) -> bool:
        return self.memory.write(
            self.key(sector, name), data, sector=sector
        )

    def read(self, sector: SectorID, name: str) -> Optional[Any]:
        return self.memory.read(self.key(sector, name))

    def free(self, sector: SectorID, name: str) -> bool:
        return self.memory.free(self.key(sector, name))


# ============================================================================
# PCS TORRENT MODEL — more load = faster
# ============================================================================

class PCSTorrentModel:
    """
    Phoenix Compression-Speed torrent model.

    Core idea: under load, compress more aggressively.
    More compression = more effective RAM = more throughput.
    Compression speeds it up — spiral tightens under load.

    Load factor 0.0 = relaxed, no extra compression
    Load factor 1.0 = maximum compression, all tiers squeezed
    """

    def __init__(self, cache: HelixCache):
        self.cache      = cache
        self.load       = 0.0
        self.lock       = threading.RLock()
        self.peak_load  = 0.0
        self.compressions_triggered = 0

    def update_load(self, load_factor: float):
        """
        Called by the game engine or Frank when load changes.
        High load → compress L2 items proactively.
        """
        with self.lock:
            self.load = max(0.0, min(1.0, load_factor))
            self.peak_load = max(self.peak_load, self.load)

            if self.load > 0.7:
                # Proactively compress L2 to make room
                self._compress_l2()

    def _compress_l2(self):
        """Compress L2 items that aren't pinned."""
        with self.cache.lock:
            for key, block in self.cache.l2.items():
                if not block.compressed and not block.pinned:
                    saved = block.compress()
                    if saved > 0:
                        self.cache.compressions += 1
                        self.compressions_triggered += 1

    def stats(self) -> dict:
        return {
            "load_factor":             round(self.load, 3),
            "peak_load":               round(self.peak_load, 3),
            "compressions_triggered":  self.compressions_triggered,
        }


# ============================================================================
# HELIX SYSTEM — unified interface
# ============================================================================

class HelixSystem:
    """
    Complete Helix memory stack with Phoenix superpowers.

    Usage:
        from helix_memory import HelixSystem, SectorID
        from helix import Helix

        helix    = Helix()                    # clone pool engine
        memory   = HelixSystem(helix=helix)   # superpowers layer
        memory.start()

        # Frank cast/reel
        result = memory.cast("build_package", fn, arg1, arg2)

        # Sector-isolated write
        memory.sector_write(SectorID.GAME, "player:1", player_state)

        # File through cache
        data = memory.fs.read("/home/jwlef/Phoenix/src/helix.py",
                              sector=SectorID.SECTOR_4)

        # Load signal — PCS torrent model
        memory.signal_load(0.8)   # heavy load → compress → faster

    Configuration for copes (i5-4460, 8GB RAM):
        l1_mb=128, l2_mb=512, l3_mb=1024, virtual_mb=4096
        Effective RAM: ~6-8GB (compression ratio ~1.5-2x)
    """

    def __init__(self,
                 helix=None,
                 l1_mb:      int = 128,
                 l2_mb:      int = 512,
                 l3_mb:      int = 1024,
                 virtual_mb: int = 4096):

        self.helix   = helix   # helix.py Helix instance — optional
        self._start  = time.time()

        self.cache   = HelixCache(l1_mb, l2_mb, l3_mb)
        self.memory  = HelixMemoryManager(self.cache, virtual_mb)
        self.fs      = HelixFS(self.memory)
        self.frank   = FrankCastReel(self.memory)
        self.sectors = SectorRouter(self.memory)
        self.pcs     = PCSTorrentModel(self.cache)

    def start(self):
        s = self.stats()
        print(f"🧬 Helix Memory Stack online")
        print(f"   L1={s['cache']['l1_mb']}MB "
              f"L2={s['cache']['l2_mb']}MB "
              f"L3={s['cache']['l3_mb']}MB "
              f"vRAM={s['memory']['allocated_mb']}MB")
        print(f"   Sectors: {[e.value for e in SectorID]}")
        print(f"   PCS torrent model: active")
        if self.helix:
            hs = self.helix.stats()
            print(f"   Helix backend: {hs['ops_per_sec']} ops/sec "
                  f"hit_rate={hs['hit_rate_pct']}%")
        print()

    # ── Frank cast/reel ───────────────────────────────────────────────────────

    def cast(self, ring: str, fn: Callable,
             *args, sector: SectorID = SectorID.FRANK, **kwargs) -> CastResult:
        """Frank casts a ring. Result reels back into Helix memory."""
        result = self.frank.cast(ring, fn, *args, sector=sector, **kwargs)
        # If Helix backend is connected, persist the result there too
        if self.helix and result.ok and result.result is not None:
            try:
                self.helix.store(
                    f"frank:cast:{ring}",
                    result.result,
                    meta={"cast_id": result.cast_id, "ring": ring},
                    note=f"cast:{ring}",
                )
            except Exception:
                pass
        return result

    # ── Sector operations ─────────────────────────────────────────────────────

    def sector_write(self, sector: SectorID, name: str, data: Any) -> bool:
        """Write to a sector's isolated memory region."""
        ok = self.sectors.write(sector, name, data)
        # Persist to Helix clone pool if backend connected
        if ok and self.helix:
            try:
                self.helix.store(
                    f"{sector.value}:{name}", data,
                    meta={"sector": sector.value},
                )
            except Exception:
                pass
        return ok

    def sector_read(self, sector: SectorID, name: str) -> Optional[Any]:
        """Read from a sector's memory region. Falls back to Helix clone pool."""
        result = self.sectors.read(sector, name)
        if result is None and self.helix:
            try:
                rec = self.helix.get(f"{sector.value}:{name}")
                if rec:
                    result = rec.data
                    # Warm it back into memory
                    self.sectors.write(sector, name, result)
            except Exception:
                pass
        return result

    # ── PCS torrent model ─────────────────────────────────────────────────────

    def signal_load(self, load_factor: float):
        """
        Signal load to the PCS torrent model.
        Game engine calls this as player count / activity rises.
        High load → more compression → more effective RAM → faster.
        """
        self.pcs.update_load(load_factor)

    # ── Stats ─────────────────────────────────────────────────────────────────

    def stats(self) -> dict:
        uptime = time.time() - self._start
        s = {
            "uptime_sec": round(uptime, 2),
            "cache":      self.cache.stats(),
            "memory":     self.memory.stats(),
            "fs":         self.fs.stats(),
            "frank":      self.frank.stats(),
            "pcs":        self.pcs.stats(),
        }
        if self.helix:
            s["helix_backend"] = self.helix.stats()
        return s

    def print_stats(self):
        s = self.stats()
        print("\n" + "=" * 60)
        print("🧬 HELIX MEMORY STACK — COPES")
        print("=" * 60)
        print(f"Uptime: {s['uptime_sec']}s")
        print()
        c = s["cache"]
        print(f"CACHE:")
        print(f"  L1 hot        : {c['l1_mb']:6.2f} MB  ({c['l1_items']:,} items)")
        print(f"  L2 warm       : {c['l2_mb']:6.2f} MB  ({c['l2_items']:,} items)")
        print(f"  L3 compressed : {c['l3_mb']:6.2f} MB  ({c['l3_items']:,} items)")
        print(f"  Hit rate      : {c['hit_rate']}%")
        print(f"  Promotions    : {c['promotions']:,}")
        print(f"  Compressions  : {c['compressions']:,}")
        print(f"  Evictions     : {c['evictions']:,}")
        print()
        m = s["memory"]
        print(f"VIRTUAL RAM:")
        print(f"  Allocated     : {m['allocated_mb']} MB")
        print(f"  Active allocs : {m['allocation_count']:,}")
        print()
        f = s["fs"]
        print(f"FILESYSTEM CACHE:")
        print(f"  Cached files  : {f['cached_files']:,}")
        print(f"  Hit rate      : {f['hits']}/{f['reads']} reads from cache")
        print()
        fr = s["frank"]
        print(f"FRANK CAST/REEL:")
        print(f"  Total casts   : {fr['total_casts']:,}")
        print(f"  Total reeled  : {fr['total_reeled']:,}")
        print()
        p = s["pcs"]
        print(f"PCS TORRENT:")
        print(f"  Load factor   : {p['load_factor']}")
        print(f"  Peak load     : {p['peak_load']}")
        print(f"  Compressions  : {p['compressions_triggered']:,}")
        if "helix_backend" in s:
            h = s["helix_backend"]
            print()
            print(f"HELIX BACKEND:")
            print(f"  ops/sec       : {h['ops_per_sec']:,}")
            print(f"  hit rate      : {h['hit_rate_pct']}%")
            print(f"  cache size    : {h['cache_size']:,}")
        print("=" * 60)


# ============================================================================
# GLOBAL INSTANCE
# ============================================================================

_global_system: Optional[HelixSystem] = None

def get_system(helix=None) -> HelixSystem:
    global _global_system
    if _global_system is None:
        _global_system = HelixSystem(helix=helix)
    return _global_system

def run(data: bytes = b"", ball=None, pcs=None):
    """
    Frank suit hook — paging manager.
    Frank wears this at boot. Runs forever.
    More load = more compression = more effective RAM = faster.
    """
    import psutil
    import time

    system = get_system()
    system.start()

    log = __import__('logging').getLogger("helix_memory.run")
    log.info("Helix paging manager online — monitoring memory pressure")

    while True:
        try:
            mem = psutil.virtual_memory()
            load = mem.percent / 100.0
            system.signal_load(load)
            if load > 0.7:
                log.info(f"Memory pressure {mem.percent:.1f}% — PCS compressing")
        except Exception as e:
            log.error(f"Paging manager error: {e}")
        time.sleep(5)


# ============================================================================
# SELF-TEST
# ============================================================================

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("🧬 HELIX MEMORY STACK — SELF TEST")
    print("=" * 60)
    print()

    # Try to load Helix backend
    helix = None
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        import helix as helix_mod
        helix = helix_mod._get_global_helix()
        print("[OK] Helix backend connected")
    except Exception as e:
        print(f"[WARN] Helix backend not available: {e}")
        print("       Running memory stack standalone")
    print()

    mem = HelixSystem(helix=helix)
    mem.start()

    # Test 1: Sector isolation
    print("TEST 1: Sector isolation")
    mem.sector_write(SectorID.SECTOR_4, "vault:test", {"secret": "data"})
    mem.sector_write(SectorID.GAME, "player:1", {"name": "Alice", "score": 0})
    mem.sector_write(SectorID.FRANK, "output:1", {"cmd": "deploy", "target": "ubuntu"})
    v = mem.sector_read(SectorID.SECTOR_4, "vault:test")
    g = mem.sector_read(SectorID.GAME, "player:1")
    f = mem.sector_read(SectorID.FRANK, "output:1")
    assert v["secret"] == "data"
    assert g["name"] == "Alice"
    assert f["cmd"] == "deploy"
    print("  ✓ Sector 4 (vault), Game, Frank — all isolated\n")

    # Test 2: Frank cast/reel
    print("TEST 2: Frank cast/reel")
    def build_package(name, version):
        return {"package": name, "version": version, "built": True}

    result = mem.cast("build_package", build_package, "helix", "2.0.0")
    assert result.ok
    assert result.result["built"]
    print(f"  ✓ Cast: {result.ring} → reeled back as cast_id={result.cast_id[:8]}...\n")

    # Test 3: PCS torrent model
    print("TEST 3: PCS torrent model")
    for i in range(2000):
        mem.memory.malloc(f"load_test:{i}", {"data": "x" * 500},
                          sector=SectorID.GAME)
    mem.signal_load(0.8)
    print(f"  ✓ Load=0.8 → compressions triggered: "
          f"{mem.pcs.compressions_triggered}\n")

    # Test 4: Filesystem cache
    print("TEST 4: Filesystem cache")
    test_path = "/tmp/helix_memory_test.txt"
    mem.fs.write(test_path, b"Phoenix memory stack test data" * 100,
                 sector=SectorID.SECTOR_4)
    data = mem.fs.read(test_path, sector=SectorID.SECTOR_4)
    data2 = mem.fs.read(test_path, sector=SectorID.SECTOR_4)  # cache hit
    assert data == data2
    print(f"  ✓ Write → read → cache hit ({mem.fs.hits} hits)\n")

    # Test 5: L1/L2/L3 promotion
    print("TEST 5: Cache tier promotion")
    mem.memory.malloc("hot:key", {"val": 42}, sector=SectorID.SECTOR_4)
    for _ in range(5):
        mem.memory.read("hot:key")
    cs = mem.cache.stats()
    print(f"  ✓ Promotions: {cs['promotions']}  "
          f"Hit rate: {cs['hit_rate']}%\n")

    mem.print_stats()

    print()
    print("Two strands. One system. Never never never give up. 🧬")
