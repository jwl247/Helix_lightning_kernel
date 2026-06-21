"""
helix_suit_override.py
Points all core suits to actual files that exist.
Imported by main_kernel.py after boot_library().
"""
import os, logging
from pathlib import Path

log = logging.getLogger("helix_suit")

def apply(library):
    from frank_ring import SuitSpec, SuitType
    from franken5 import DataFamily

    repo = Path(os.environ.get("PHOENIX_SUITS", Path(__file__).parents[1]))
    s1 = Path(os.environ.get("PHOENIX_SECTOR1", repo / "sector1"))
    s2 = Path(os.environ.get("PHOENIX_SECTOR2", repo / "sector2"))
    s3 = Path(os.environ.get("PHOENIX_SECTOR3", repo / "sector3"))

    stack = str(s1 / "helix" / "helix_complete_stack.py")

    overrides = [
        ("frank3_slot_a", stack,                                              1, 0, DataFamily.SYSTEM),
        ("frank3_slot_b", stack,                                              1, 1, DataFamily.SYSTEM),
        ("concierge",     stack,                                              1, 3, DataFamily.SYSTEM),
        ("clone_pool",    stack,                                              2, 1, DataFamily.USER),
        ("packages_worker", stack,                                            2, 3, DataFamily.USER),
        ("romeo",         str(s3/"romeo_juliet"/"romeo.py"),                  3, 0, DataFamily.NETWORK),
        ("juliet",        str(s3/"romeo_juliet"/"juliet.py"),                 3, 1, DataFamily.NETWORK),
        ("dbl_juliet",    str(s3/"romeo_juliet"/"dbl_juliet.py"),             3, 2, DataFamily.NETWORK),
        ("quadengine",    str(s3/"quadengine"/"quadengine.py"),               3, 3, DataFamily.NETWORK),
        ("helix",         stack,                                              4, 0, DataFamily.SYSTEM),
        ("freewheeling",  stack,                                              4, 1, DataFamily.SYSTEM),
        ("propcoms",      stack,                                              4, 2, DataFamily.SYSTEM),
        ("conductor",     stack,                                              4, 3, DataFamily.SYSTEM),
    ]

    for name, entry_path, sector, ring_pos, family in overrides:
        if not Path(entry_path).exists():
            log.warning(f"Suit {name} → {entry_path} not found — skipping")
            continue
        suit_type = SuitType.SHELL if entry_path.endswith(".sh") else SuitType.PYTHON
        spec = SuitSpec(
            name=name,
            suit_type=suit_type,
            entry=entry_path,
            sector=sector,
            ring_pos=ring_pos,
            family=family,
            permissions={"read": True, "write": True, "clone": True,
                         "translate": sector == 3, "delete": False, "kernel": sector == 1},
        )
        library._register(spec, tags=["core", f"sector{sector}"])
        log.info(f"Suit {name} → {entry_path}")

    library._write_index()
    log.info("Suit overrides applied")
