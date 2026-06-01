#!/usr/bin/env python3
"""
main_kernel.py — Phoenix DevOps OS Application Kernel
Phoenix DevOps OS | jwl247 | GPL v3

The one entry point.
Frank and Helix run everything from here.

Boot order:
  1. Frank-core boots — SHM bus mounted, signal handlers installed
  2. Process library boots — clonepool scanned, suits registered
  3. Helix-I starts — ingress strands A+B listening on channels 1-4
  4. Helix-E starts — egress strands A+B output on channels 5-8
  5. frank.conduct() — the heartbeat loop, runs forever

Frank's heartbeat IS this loop.
Kill the process to shut down clean.
"""

import os
import sys
import time
import signal
import logging
import threading
from pathlib import Path

# ── Kernel components ─────────────────────────────────────────
from franken5 import get_frank, FRANK_VERSION
from helixi import HelixI
from helixe import HelixE
from process_library import get_library,boot_library,LIBRARY_VERSION
from frank_spawn import FrankSpawn

KERNEL_VERSION = "2.0.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [KERNEL] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(
            Path(os.environ.get("PHOENIX_AUDIT", "/tmp/phoenix_audit.log"))
        ),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("main_kernel")


def boot():
    log.info(f"Phoenix DevOps OS — Kernel v{KERNEL_VERSION} booting")
    log.info(f"Frank v{FRANK_VERSION} | Library v{LIBRARY_VERSION}")
    log.info(f"PID {os.getpid()}")

    # ── Step 1: Frank-core ────────────────────────────────────
    frank = get_frank()
    frank.boot()
    log.info("Frank-core online")

    # Publish Frank's PID so Helix-I can signal him
    os.environ["FRANK5_PID"] = str(os.getpid())

    # ── Step 2: Process Library ───────────────────────────────
    library = boot_library()
    status = library.status()
    log.info(f"Process library ready — {status['suit_count']} suits registered")

    # ── Step 3: Helix-I ingress ───────────────────────────────
    helix_i = HelixI(frank)

    def on_stage(channel, slot, size):
        log.debug(f"Stage landed — ch{channel} slot{slot} {size}b")

    helix_i.on_stage_ready(on_stage)
    helix_i.start_socket_listeners()
    log.info("Helix-I online — ingress channels 1-4 listening")

    # ── Step 4: Helix-E egress ────────────────────────────────
    helix_e = HelixE(frank)

    def on_output(channel, data, meta):
        log.debug(f"Output ch{channel}: {len(data)}b")

    helix_e.on_output(on_output)
    helix_e.start_output_sockets()
    log.info("Helix-E online — egress channels 5-8 ready")

    # ── Step 5: Status heartbeat thread ──────────────────────
    def heartbeat():
        while True:
            time.sleep(30)
            status = frank.status()
            log.info(
                f"HEARTBEAT — rings live:{status['rings_live']} "
                f"total:{status['rings_total']} done:{status['rings_done']}"
            )
            i_status = helix_i.status()
            e_status = helix_e.status()
            i_stages = sum(c["stages"] for c in i_status["channels"].values())
            e_outputs = sum(c["outputs"] for c in e_status["channels"].values())
            log.info(f"HEARTBEAT — stages fired:{i_stages} outputs flushed:{e_outputs}")

    hb = threading.Thread(target=heartbeat, daemon=True, name="heartbeat")
    hb.start()

    # ── Shutdown handler ──────────────────────────────────────
    def shutdown(signum, frame):
        log.info("Shutdown signal received — cleaning up")
        helix_i.stop()
        helix_e.stop()
        frank.shutdown()
        log.info("Phoenix kernel shutdown complete")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # ── Step 6: Frank conducts — this is the heartbeat ───────
    log.info("Phoenix kernel fully operational — Frank conducting")

    spawn = FrankSpawn(frank, library)
    spawn.install()
    log.info("FrankSpawn online — rings ready")
# Boot paging manager in background thread
    import helix_memory
    paging_thread = threading.Thread(
        target=helix_memory.run,
        daemon=True,
        name="helix-paging-manager"
    )
    paging_thread.start()
    log.info("Helix paging manager started — 512MB L1 / 2GB L2 / 8GB L3 / 512GB vRAM")
    spawn.loop()


if __name__ == "__main__":
    boot()
