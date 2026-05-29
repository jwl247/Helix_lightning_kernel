#!/usr/bin/env python3
"""
helix_e.py — Helix Egress
Phoenix DevOps OS | jwl247 | GPL v3

Helix-E is the right lung.
She breathes data OUT.

Two strands. Four channels. One job:
When Frank says go — translate and flush.

Helix-E does NOT decide when to run.
Helix-E does NOT pull data.
Helix-E does NOT interrupt Frank.

Frank commands. Helix-E executes. She's done.

Strand A — channels 5, 6  (primary egress)
Strand B — channels 7, 8  (overflow + priority out)

Critical rule from CLAUDE.md:
  translator.sh fires on OUTPUT ONLY — never on intake or clone.
  Helix-E IS that rule in Python form.
"""

import os
import sys
import time
import struct
import socket
import threading
import logging
from pathlib import Path
from typing import Optional, Callable, Any
from dataclasses import dataclass
from enum import IntEnum

from franken5 import (
    Frank5, get_frank, SharedMemoryBus,
    SHM_PATH, STAGE_SLOT_SIZE,
    FRANK_VERSION
)

log = logging.getLogger("helix_e")

HELIX_E_VERSION   = "1.0.0-alpha"
STRAND_A_CHANNELS = (5, 6)
STRAND_B_CHANNELS = (7, 8)
ALL_CHANNELS      = STRAND_A_CHANNELS + STRAND_B_CHANNELS

SOCKET_BASE     = int(os.environ.get("HELIX_E_PORT", 7800))
TRANSLATOR_PATH = Path(os.environ.get(
    "TRANSLATOR_SH",
    "/etc/systemd/system/translator/translator.sh"
))


class ChannelState(IntEnum):
    IDLE        = 0
    TRANSLATING = 1
    FLUSHING    = 2
    DONE        = 3
    ERROR       = 4


@dataclass
class EgressChannel:
    number:   int
    strand:   str
    slot:     int
    state:    ChannelState = ChannelState.IDLE
    flushed:  int = 0     # bytes flushed this session
    outputs:  int = 0     # successful outputs this session
    errors:   int = 0
    _sock:    Optional[socket.socket] = None


class HelixE:
    """
    Helix Egress — the right lung of Phoenix.

    Two strands, four channels.
    Frank commands a flush — Helix-E translates and sends it out.
    Frank does not wait. Helix-E handles the rest.

    Translation happens HERE at the sector3 boundary.
    Nowhere else. Ever.
    """

    def __init__(self, frank: Optional[Frank5] = None):
        self.frank  = frank or get_frank()
        self.bus    = self.frank.bus
        self._alive = True
        self._lock  = threading.Lock()

        self.strand_a: list[EgressChannel] = [
            EgressChannel(number=ch, strand='A', slot=ch - 1)
            for ch in STRAND_A_CHANNELS
        ]
        self.strand_b: list[EgressChannel] = [
            EgressChannel(number=ch, strand='B', slot=ch - 1)
            for ch in STRAND_B_CHANNELS
        ]
        self.channels: dict[int, EgressChannel] = {
            ch.number: ch
            for ch in self.strand_a + self.strand_b
        }

        self._translators: dict[str, Callable] = {}
        self._output_handlers: list[Callable]  = []
        self._register_default_translators()

        log.info(f"Helix-E v{HELIX_E_VERSION} — strands A+B — channels {ALL_CHANNELS}")

    def flush(self, channel_num: int, ring_id: int, target_lang: str = "auto") -> bool:
        """
        Frank calls this. That's the only way flush happens.
        Helix-E does NOT call this itself.

        Reads the stage from shared memory.
        Translates to target language.
        Pushes to output handlers.
        Marks the ring done.
        """
        ch = self.channels.get(channel_num)
        if not ch:
            log.error(f"Unknown egress channel {channel_num}")
            return False

        with self._lock:
            ch.state = ChannelState.TRANSLATING

        try:
            raw = self.bus.read_stage(ch.slot)
            if not raw:
                log.warning(f"Ch{channel_num} slot {ch.slot} empty — nothing to flush")
                ch.state = ChannelState.IDLE
                return False

            data, meta = self._unpack_stage(raw)

            translated = self._translate(data, target_lang, meta)

            with self._lock:
                ch.state = ChannelState.FLUSHING

            self._push_output(channel_num, translated, meta)

            self.bus.write_stage(ch.slot, b"")

            with self._lock:
                ch.state    = ChannelState.DONE
                ch.flushed += len(translated)
                ch.outputs += 1

            self.frank.mark_syncing(ring_id)

            log.debug(
                f"Ch{channel_num} flushed {len(translated)}b "
                f"→ {target_lang} — ring {ring_id} syncing"
            )
            return True

        except Exception as e:
            with self._lock:
                ch.state  = ChannelState.ERROR
                ch.errors += 1
            log.error(f"Ch{channel_num} flush failed: {e}")
            return False

    def flush_async(self, channel_num: int, ring_id: int, target_lang: str = "auto"):
        """
        Non-blocking flush. Frank fires and forgets.
        Helix-E handles it in her own thread.
        Frank is already conducting the next ring.
        """
        t = threading.Thread(
            target=self.flush,
            args=(channel_num, ring_id, target_lang),
            daemon=True,
            name=f"helix-e-flush-ch{channel_num}"
        )
        t.start()

    def register_translator(self, lang: str, fn: Callable[[bytes, dict], bytes]):
        """
        Register a translation function for a target language.
        This is how the quadralingual system hooks in.
        Frank doesn't care what language. Helix-E handles it.
        """
        self._translators[lang] = fn
        log.info(f"Translator registered: {lang}")

    def on_output(self, cb: Callable[[int, bytes, dict], None]):
        """Register a handler for translated output."""
        self._output_handlers.append(cb)

    def _translate(self, data: bytes, target_lang: str, meta: dict) -> bytes:
        """
        Translation happens HERE. Sector3 boundary. Nowhere else.
        If no translator registered for target_lang, pass through raw.
        If translator.sh exists, shell out to it for complex translations.
        """
        if target_lang == "auto":
            target_lang = meta.get("lang", "raw")

        if target_lang in self._translators:
            return self._translators[target_lang](data, meta)

        if target_lang not in ("raw", "bytes") and TRANSLATOR_PATH.exists():
            return self._shell_translate(data, target_lang)

        return data

    def _shell_translate(self, data: bytes, target_lang: str) -> bytes:
        """
        Hand off to translator.sh for language-specific translation.
        This is the bridge to the existing tested translator.
        OUTPUT ONLY. Never called on intake or clone.
        """
        import subprocess
        try:
            result = subprocess.run(
                [str(TRANSLATOR_PATH), target_lang],
                input=data,
                capture_output=True,
                timeout=10
            )
            if result.returncode == 0:
                return result.stdout
            else:
                log.error(f"translator.sh error: {result.stderr.decode()}")
                return data
        except subprocess.TimeoutExpired:
            log.error("translator.sh timed out — returning raw")
            return data
        except Exception as e:
            log.error(f"translator.sh failed: {e}")
            return data

    def _push_output(self, channel_num: int, data: bytes, meta: dict):
        for handler in self._output_handlers:
            try:
                handler(channel_num, data, meta)
            except Exception as e:
                log.error(f"Output handler error on ch{channel_num}: {e}")

    def _unpack_stage(self, raw: bytes) -> tuple[bytes, dict]:
        """Unpack a stage written by Helix-I."""
        import json
        HEADER_SIZE = struct.calcsize("!4sBBHI")
        if len(raw) < HEADER_SIZE:
            return raw, {}
        try:
            magic, channel, strand, data_len, seq = struct.unpack(
                "!4sBBHI", raw[:HEADER_SIZE]
            )
            if magic != b"HISX":
                return raw, {}
            data    = raw[HEADER_SIZE:HEADER_SIZE + data_len]
            meta_b  = raw[HEADER_SIZE + data_len:]
            meta    = json.loads(meta_b.rstrip(b'\x00')) if meta_b.strip(b'\x00') else {}
            meta.update({"channel": channel, "seq": seq})
            return data, meta
        except Exception:
            return raw, {}

    def _register_default_translators(self):
        """
        Default pass-through translators.
        Real translators (Python, JS, bash, PowerShell) registered by the caller.
        """
        self.register_translator("raw",   lambda d, m: d)
        self.register_translator("bytes", lambda d, m: d)
        self.register_translator("utf8",  lambda d, m: d.decode("utf-8", errors="replace").encode())
        self.register_translator("json",  self._to_json)

    def _to_json(self, data: bytes, meta: dict) -> bytes:
        import json
        try:
            parsed = json.loads(data)
            return json.dumps(parsed, indent=2).encode()
        except Exception:
            return data

    def start_output_sockets(self):
        """
        Open a socket per channel for downstream consumers.
        Push translated output to anything listening.
        """
        for ch in self.channels.values():
            port = SOCKET_BASE + ch.number
            t = threading.Thread(
                target=self._output_server,
                args=(ch, port),
                daemon=True,
                name=f"helix-e-ch{ch.number}"
            )
            t.start()
            log.info(f"Helix-E ch{ch.number} (strand {ch.strand}) output on :{port}")

    def _output_server(self, ch: EgressChannel, port: int):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", port))
        sock.listen(8)
        ch._sock = sock
        log.info(f"Helix-E ch{ch.number} server ready on :{port}")
        while self._alive:
            try:
                conn, addr = sock.accept()
                log.debug(f"Helix-E ch{ch.number} consumer connected: {addr}")
                conn.close()
            except Exception:
                break

    def stop(self):
        self._alive = False
        for ch in self.channels.values():
            if ch._sock:
                try:
                    ch._sock.close()
                except Exception:
                    pass
        log.info("Helix-E stopped")

    def status(self) -> dict:
        with self._lock:
            return {
                "version":  HELIX_E_VERSION,
                "channels": {
                    n: {
                        "strand":  ch.strand,
                        "state":   ch.state.name,
                        "flushed": ch.flushed,
                        "outputs": ch.outputs,
                        "errors":  ch.errors,
                    }
                    for n, ch in self.channels.items()
                }
            }


if __name__ == "__main__":
    frank   = get_frank()
    frank.boot()
    helix_e = HelixE(frank)

    def print_output(channel, data, meta):
        log.info(f"OUTPUT ch{channel}: {len(data)}b — {meta}")

    helix_e.on_output(print_output)
    helix_e.start_output_sockets()

    log.info("Helix-E online — waiting for Frank to command flushes")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        helix_e.stop()
        frank.shutdown()

