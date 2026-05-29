#!/usr/bin/env python3
"""
helix_i.py — Helix Ingress
Phoenix DevOps OS | jwl247 | GPL v3

Helix-I is the left lung.
She breathes data IN.

Two strands. Four channels. One job:
Pull a stage of data and fire the interrupt that wakes Frank.

Helix-I does NOT make decisions.
Helix-I does NOT transform data.
Helix-I does NOT know what Frank will do with it.

She pulls. She signals. She's done.
Frank rides the lightning from there.

Strand A — channels 1, 2  (primary ingress)
Strand B — channels 3, 4  (overflow + priority)
"""

import os
import sys
import time
import signal
import select
import socket
import struct
import threading
import logging
from pathlib import Path
from typing import Optional, Callable
from dataclasses import dataclass
from enum import IntEnum

from franken5 import (
    Frank5, get_frank, SharedMemoryBus,
    FrankSignal, SHM_PATH, STAGE_SLOT_SIZE,
    FRANK_VERSION
)

log = logging.getLogger("helix_i")

HELIX_I_VERSION = "1.0.0-alpha"
STRAND_A_CHANNELS = (1, 2)
STRAND_B_CHANNELS = (3, 4)
ALL_CHANNELS      = STRAND_A_CHANNELS + STRAND_B_CHANNELS

SOCKET_BASE = int(os.environ.get("HELIX_I_PORT", 7700))
MAX_STAGE_BYTES = STAGE_SLOT_SIZE - 64   # leave header room
INTERRUPT_TARGET_PID = int(os.environ.get("FRANK5_PID", os.getpid()))


class ChannelState(IntEnum):
    IDLE     = 0
    PULLING  = 1
    STAGED   = 2
    SIGNALED = 3
    ERROR    = 4


@dataclass
class Channel:
    number:   int
    strand:   str          # 'A' or 'B'
    slot:     int          # shared memory slot index
    state:    ChannelState = ChannelState.IDLE
    pulled:   int          = 0    # bytes pulled this session
    stages:   int          = 0    # stages fired this session
    errors:   int          = 0
    _sock:    Optional[socket.socket] = None
    _thread:  Optional[threading.Thread] = None


class HelixI:
    """
    Helix Ingress — the left lung of Phoenix.

    Two strands, four channels.
    Pulls data into shared memory slots.
    Fires SIGUSR1 to Frank-core when a stage is ready.
    Frank wakes up. Frank rides. Helix-I is already pulling the next stage.
    """

    def __init__(self, frank: Optional[Frank5] = None):
        self.frank    = frank or get_frank()
        self.bus      = self.frank.bus
        self._alive   = True
        self._lock    = threading.Lock()

        self.strand_a: list[Channel] = [
            Channel(number=ch, strand='A', slot=ch - 1)
            for ch in STRAND_A_CHANNELS
        ]
        self.strand_b: list[Channel] = [
            Channel(number=ch, strand='B', slot=ch - 1)
            for ch in STRAND_B_CHANNELS
        ]
        self.channels: dict[int, Channel] = {
            ch.number: ch
            for ch in self.strand_a + self.strand_b
        }

        self._stage_callbacks: list[Callable] = []
        log.info(f"Helix-I v{HELIX_I_VERSION} — strands A+B — channels {ALL_CHANNELS}")

    def on_stage_ready(self, cb: Callable):
        """Register a callback fired after each stage lands in shared memory."""
        self._stage_callbacks.append(cb)

    def pull(self, channel_num: int, data: bytes, meta: dict = None) -> bool:
        """
        Core operation. Pull data into a shared memory slot.
        Fire the Frank interrupt when staged.

        This is the only thing Helix-I does.
        """
        ch = self.channels.get(channel_num)
        if not ch:
            log.error(f"Unknown channel {channel_num}")
            return False

        if len(data) > MAX_STAGE_BYTES:
            log.warning(f"Ch{channel_num} data {len(data)}b truncated to {MAX_STAGE_BYTES}b")
            data = data[:MAX_STAGE_BYTES]

        with self._lock:
            ch.state = ChannelState.PULLING

        try:
            payload = self._pack_stage(channel_num, data, meta or {})
            self.bus.write_stage(ch.slot, payload)

            with self._lock:
                ch.state  = ChannelState.STAGED
                ch.pulled += len(data)
                ch.stages += 1

            self._fire_interrupt()

            with self._lock:
                ch.state = ChannelState.SIGNALED

            for cb in self._stage_callbacks:
                try:
                    cb(channel_num, ch.slot, len(data))
                except Exception as e:
                    log.error(f"Stage callback error: {e}")

            log.debug(f"Ch{channel_num} staged {len(data)}b → slot {ch.slot} → Frank signaled")
            return True

        except Exception as e:
            with self._lock:
                ch.state  = ChannelState.ERROR
                ch.errors += 1
            log.error(f"Ch{channel_num} pull failed: {e}")
            return False

    def pull_stream(self, channel_num: int, stream, chunk_size: int = MAX_STAGE_BYTES):
        """
        Pull a stream in chunks — one stage per chunk.
        Frank gets an interrupt per chunk. Rings handle them in parallel.
        This is how large data flows through without blocking.
        """
        ch = self.channels.get(channel_num)
        if not ch:
            return

        stages_fired = 0
        while self._alive:
            chunk = stream.read(chunk_size) if hasattr(stream, 'read') else None
            if chunk is None:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode()
            if not chunk:
                break
            self.pull(channel_num, chunk, {"stream": True, "seq": stages_fired})
            stages_fired += 1
            time.sleep(0)  # yield — let Frank ride before next chunk

        log.info(f"Ch{channel_num} stream complete — {stages_fired} stages fired")

    def _fire_interrupt(self):
        """
        The one signal. Helix-I's only communication with Frank.
        SIGUSR1 → Frank wakes → Frank rides.
        """
        try:
            os.kill(INTERRUPT_TARGET_PID, FrankSignal.STAGE_READY)
        except ProcessLookupError:
            log.warning("Frank-core PID not found — interrupt dropped")
        except PermissionError:
            log.error("Cannot signal Frank-core — check permissions")

    def _pack_stage(self, channel: int, data: bytes, meta: dict) -> bytes:
        """
        Stage format:
        [4b magic][2b channel][2b strand][4b data_len][4b seq][data]
        """
        import json
        strand    = ord('A') if channel in STRAND_A_CHANNELS else ord('B')
        meta_b    = json.dumps(meta).encode()[:256]
        header    = struct.pack(
            "!4sBBHI",
            b"HISX",
            channel,
            strand,
            len(data),
            self.channels[channel].stages
        )
        return header + data + meta_b

    def start_socket_listeners(self):
        """
        Open a socket per channel so external processes can push data in.
        Each channel listens on SOCKET_BASE + channel_number.
        Non-blocking. Each channel runs its own thread.
        """
        for ch in self.channels.values():
            port = SOCKET_BASE + ch.number
            t = threading.Thread(
                target=self._socket_listener,
                args=(ch, port),
                daemon=True,
                name=f"helix-i-ch{ch.number}"
            )
            t.start()
            ch._thread = t
            log.info(f"Helix-I ch{ch.number} (strand {ch.strand}) listening on :{port}")

    def _socket_listener(self, ch: Channel, port: int):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(8)
        sock.setblocking(False)
        ch._sock = sock

        while self._alive:
            try:
                readable, _, _ = select.select([sock], [], [], 0.5)
                if not readable:
                    continue
                conn, addr = sock.accept()
                threading.Thread(
                    target=self._handle_connection,
                    args=(ch, conn, addr),
                    daemon=True
                ).start()
            except Exception as e:
                if self._alive:
                    log.error(f"Ch{ch.number} listener error: {e}")

    def _handle_connection(self, ch: Channel, conn: socket.socket, addr):
        try:
            chunks = []
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                chunks.append(data)
            if chunks:
                self.pull(ch.number, b"".join(chunks), {"src": str(addr)})
        except Exception as e:
            log.error(f"Ch{ch.number} connection error from {addr}: {e}")
        finally:
            conn.close()

    def stop(self):
        self._alive = False
        for ch in self.channels.values():
            if ch._sock:
                try:
                    ch._sock.close()
                except Exception:
                    pass
        log.info("Helix-I stopped")

    def status(self) -> dict:
        with self._lock:
            return {
                "version":  HELIX_I_VERSION,
                "channels": {
                    n: {
                        "strand":  ch.strand,
                        "state":   ch.state.name,
                        "pulled":  ch.pulled,
                        "stages":  ch.stages,
                        "errors":  ch.errors,
                    }
                    for n, ch in self.channels.items()
                }
            }


if __name__ == "__main__":
    import json
    frank = get_frank()
    frank.boot()

    helix_i = HelixI(frank)

    def on_stage(channel, slot, size):
        log.info(f"Stage landed — ch{channel} slot{slot} {size}b")

    helix_i.on_stage_ready(on_stage)
    helix_i.start_socket_listeners()

    log.info("Helix-I online — push data to ports 7701-7704")
    try:
        while True:
            time.sleep(1)
            s = helix_i.status()
            total_stages = sum(c["stages"] for c in s["channels"].values())
            if total_stages:
                log.info(f"Total stages fired: {total_stages}")
    except KeyboardInterrupt:
        helix_i.stop()
        frank.shutdown()

