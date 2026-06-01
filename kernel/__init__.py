"""
CoPES Kernel Package
Coordinated Process Engine Substrate

The kernel lives here. Frank and Helix grow up underneath the existing
operational layer. When ready, the existing frank.py imports from here
and the upgrade goes live in one line.

Usage (when the time comes):
    from kernel.frank import Frank, Ring, PacketType, build_ring_chain
    from kernel.helix import Helix, HelixMode, ReplicaTarget

Drop-in path:
    CoPES/src/kernel/__init__.py
    CoPES/src/kernel/frank.py
    CoPES/src/kernel/helix.py

Authors: Jerry Leftwich + Jerilynn Leftwich
License: GPL v3
"""

from kernel.frank import (
    Frank,
    Ring,
    PacketType,
    ImportRecord,
    HelixWindow,
    DoubleHelixPacket,
    build_ring_chain,
    build_packet,
)

from kernel.helix import (
    Helix,
    HelixMode,
    HelixEngine,
    ReplicaTarget,
    ReplicaRecord,
    SharedMemoryBus,
    from_environment,
)

__all__ = [
    # Frank
    "Frank",
    "Ring",
    "PacketType",
    "ImportRecord",
    "HelixWindow",
    "DoubleHelixPacket",
    "build_ring_chain",
    "build_packet",
    # Helix
    "Helix",
    "HelixMode",
    "HelixEngine",
    "ReplicaTarget",
    "ReplicaRecord",
    "SharedMemoryBus",
    "from_environment",
]
