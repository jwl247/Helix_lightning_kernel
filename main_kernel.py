#!/usr/bin/env python3
"""
main_kernel.py — CoPES Application Kernel Boot Loader
Phoenix DevOps OS | jwl247 | GPL v3
"""

import logging
import time
import sys

from franken5 import get_frank
from process_library import boot_library
from frank_spawn import start_spawn
from helix_i import HelixI
from helix_e import HelixE

log = logging.getLogger("copes_kernel")


def boot_substrate():
    """Main kernel entry point"""
    log.info("=== CoPES / Phoenix Kernel Booting ===")
    
    # 1. Frank Core Conductor
    frank = get_frank()
    frank.boot()
    
    # 2. Process Library (The Closet)
    library = boot_library(frank)
    
    # 3. Frank Spawner
    spawner = start_spawn(frank, process_library=library)
    
    # 4. Helix Ingress + Egress
    helix_i = HelixI(frank)
    helix_i.start_socket_listeners()
    
    helix_e = HelixE(frank)
    helix_e.start_output_sockets()
    
    log.info("=== Phoenix Kernel FULLY OPERATIONAL ===")
    log.info("→ Helix-I  listening on 7701-7704")
    log.info("→ Helix-E  output on 7805-7808")
    log.info("→ Ready for remote desktop suits, data flow, etc.")
    
    # Keep the kernel alive
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutdown requested...")
        spawner.stop()
        helix_i.stop()
        helix_e.stop()
        frank.shutdown()
        log.info("Kernel shutdown complete.")


if __name__ == "__main__":
    # Setup root logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)]
    )
    boot_substrate()
