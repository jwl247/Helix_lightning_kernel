#!/usr/bin/env python3
# =============================================================================
# watcher.py — Phoenix DevOps OS / COPES
# Inotify watcher — auto-intake on file create/modify/move-in.
# Every file that lands in the watch path goes through ph intake automatically.
# Edited directly in GitHub? git pull + watcher picks it up.
# =============================================================================
# Author:  jwl247 / Phoenix DevOps LLC
# License: GPL v3
#
# USAGE:
#   watch-phoenix                        # watch ~/Phoenix (default)
#   watch-phoenix --path ~/incoming      # watch a specific dir
#   watch-phoenix --path ~/Phoenix/src --path ~/incoming  # multiple dirs
#   watch-phoenix --no-recursive         # flat watch only
#   watch-phoenix --pull                 # git pull before starting + on SIGHUP
#
# SYSTEMD (run at boot):
#   sudo cp /path/to/watch-phoenix.service /etc/systemd/system/
#   sudo systemctl enable --now watch-phoenix
#
# SKIP LIST:
#   Same as intake-dir: __pycache__, .git, .venv, node_modules,
#   clonepool, .pyc, .o, .swp, .tmp, .lock etc.
# =============================================================================

import os
import sys
import time
import signal
import logging
import argparse
import threading
import subprocess
from pathlib import Path
from datetime import datetime, timezone

# ── Optional inotify (Linux). Falls back to polling on non-Linux. ─────────────
try:
    from inotify_simple import INotify, flags as iflags
    HAVE_INOTIFY = True
except ImportError:
    HAVE_INOTIFY = False

WATCHER_VERSION = "1.0.0"

# ── Env ───────────────────────────────────────────────────────────────────────
PHOENIX_HOME = Path(os.environ.get("PHOENIX_HOME", Path.home() / "Phoenix"))
LOG_PATH     = PHOENIX_HOME / "db" / "watcher.log"

# ── Skip lists (mirrors intake_dir) ──────────────────────────────────────────
SKIP_EXTENSIONS = {
    ".pyc", ".pyo", ".pyd", ".so", ".o", ".a",
    ".swp", ".tmp", ".lock", ".sidecar.json",
}
SKIP_DIRS = {
    "__pycache__", ".git", ".venv", "venv", "node_modules",
    "clonepool", ".mypy_cache", ".pytest_cache",
}
SKIP_NAMES = {".DS_Store", "desktop.ini", "thumbs.db"}


# ── Logging ───────────────────────────────────────────────────────────────────
def _setup_logging(verbose: bool = False) -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if verbose else logging.INFO
    fmt   = "%(asctime)s [%(levelname)s] %(message)s"
    logging.basicConfig(
        level=level,
        format=fmt,
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(sys.stdout),
        ]
    )
    return logging.getLogger("watch-phoenix")


# ── Should we intake this path? ───────────────────────────────────────────────
def _should_intake(p: Path) -> tuple[bool, str]:
    if not p.is_file():
        return False, "not_a_file"
    if p.name in SKIP_NAMES:
        return False, f"skip_name:{p.name}"
    if p.suffix.lower() in SKIP_EXTENSIONS:
        return False, f"skip_ext:{p.suffix}"
    for part in p.parts:
        if part in SKIP_DIRS:
            return False, f"skip_dir:{part}"
    return True, "ok"


# ── Git pull helper ───────────────────────────────────────────────────────────
def _git_pull(path: Path, log: logging.Logger):
    try:
        result = subprocess.run(
            ["git", "pull", "--ff-only"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            log.info(f"[git pull] {path} — {result.stdout.strip() or 'already up to date'}")
        else:
            log.warning(f"[git pull] {path} — {result.stderr.strip()}")
    except Exception as e:
        log.warning(f"[git pull] failed: {e}")


# ── Intake dispatcher ─────────────────────────────────────────────────────────
class IntakeDispatcher:
    """
    Debounced intake dispatcher.
    Batches rapid file events (e.g. git pull touching 20 files)
    into a single intake pass after a short quiet window.
    """

    DEBOUNCE_SECS = 1.5   # wait this long after last event before intaking

    def __init__(self, log: logging.Logger):
        self.log      = log
        self._pending: dict[str, float] = {}  # path -> queued_at
        self._lock    = threading.Lock()
        self._event   = threading.Event()
        self._running = True
        self._thread  = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

        # Lazy PH load
        self._ph = None

    def _get_ph(self):
        if self._ph is None:
            try:
                from package_handler import get_ph
                self._ph = get_ph()
                self.log.info("[watcher] Package Handler online.")
            except Exception as e:
                self.log.error(f"[watcher] Cannot load Package Handler: {e}")
        return self._ph

    def queue(self, path: str):
        with self._lock:
            self._pending[path] = time.monotonic()
        self._event.set()

    def _worker(self):
        while self._running:
            self._event.wait(timeout=0.5)
            self._event.clear()

            now     = time.monotonic()
            ready   = []
            with self._lock:
                for path, queued_at in list(self._pending.items()):
                    if (now - queued_at) >= self.DEBOUNCE_SECS:
                        ready.append(path)
                        del self._pending[path]

            for path in ready:
                self._do_intake(path)

    def _do_intake(self, path: str):
        p = Path(path)
        ok, reason = _should_intake(p)
        if not ok:
            self.log.debug(f"[skip] {path} ({reason})")
            return

        ph = self._get_ph()
        if ph is None:
            self.log.error(f"[watcher] PH offline — cannot intake {path}")
            return

        try:
            result = ph.intake(path, notes="auto:watcher")
            if result.get("ok"):
                self.log.info(
                    f"[intake] {p.name}  tav={result['tav']}  "
                    f"size={result['size']}  path={path}"
                )
            else:
                self.log.warning(f"[intake] FAILED {path} — {result.get('error')}")
        except Exception as e:
            self.log.error(f"[intake] ERROR {path} — {e}")

    def stop(self):
        self._running = False
        self._event.set()
        self._thread.join(timeout=5)


# ── Inotify watcher ───────────────────────────────────────────────────────────
class HelixWatcher:
    """
    Watches one or more directories for new/modified/moved-in files
    and auto-intakes them through Package Handler.

    Linux: uses inotify_simple (kernel inotify — zero polling cost).
    Fallback: polls every 2 seconds (works on any OS).
    """

    POLL_INTERVAL = 2.0   # seconds between polls (fallback mode)

    def __init__(self, paths: list[Path], recursive: bool,
                 pull: bool, verbose: bool):
        self.paths     = paths
        self.recursive = recursive
        self.pull      = pull
        self.log       = _setup_logging(verbose)
        self.dispatcher = IntakeDispatcher(self.log)
        self._running  = False

        # Fallback poll state
        self._seen: dict[str, float] = {}  # path -> mtime

    # ── Git pull all watched paths ────────────────────────────────────────────

    def _pull_all(self):
        for p in self.paths:
            # Find git root at or above the watched path
            try:
                result = subprocess.run(
                    ["git", "rev-parse", "--show-toplevel"],
                    cwd=p, capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0:
                    git_root = Path(result.stdout.strip())
                    _git_pull(git_root, self.log)
            except Exception:
                pass

    # ── Startup scan — intake everything already present ─────────────────────

    def _startup_scan(self):
        self.log.info("[watcher] Startup scan...")
        ph = self.dispatcher._get_ph()
        if not ph:
            return
        for base in self.paths:
            it = base.rglob("*") if self.recursive else base.glob("*")
            for item in it:
                if item.is_file():
                    ok, reason = _should_intake(item)
                    if ok:
                        self.dispatcher.queue(str(item))
        self.log.info("[watcher] Startup scan queued.")

    # ── inotify loop (Linux, preferred) ──────────────────────────────────────

    def _run_inotify(self):
        inotify  = INotify()
        watch_flags = (
            iflags.CLOSE_WRITE |   # file written and closed
            iflags.MOVED_TO    |   # file moved into watched dir
            iflags.CREATE              # file created (catches git checkout)
        )

        wd_to_path: dict[int, Path] = {}

        def _add_watch(d: Path):
            try:
                wd = inotify.add_watch(str(d), watch_flags)
                wd_to_path[wd] = d
                self.log.debug(f"[watch] {d}")
            except Exception as e:
                self.log.warning(f"[watch] cannot watch {d}: {e}")

        for base in self.paths:
            _add_watch(base)
            if self.recursive:
                for sub in base.rglob("*"):
                    if sub.is_dir():
                        skip = any(part in SKIP_DIRS for part in sub.parts)
                        if not skip:
                            _add_watch(sub)

        self.log.info(
            f"[watcher] inotify active — watching {len(wd_to_path)} "
            f"director{'y' if len(wd_to_path)==1 else 'ies'}"
        )

        while self._running:
            events = inotify.read(timeout=1000)   # 1s timeout — check _running
            for ev in events:
                if not ev.name:
                    continue
                parent = wd_to_path.get(ev.wd)
                if parent is None:
                    continue
                full = parent / ev.name

                # If a new directory appeared, watch it too (recursive)
                if self.recursive and full.is_dir():
                    skip = any(part in SKIP_DIRS for part in full.parts)
                    if not skip:
                        _add_watch(full)
                    continue

                self.dispatcher.queue(str(full))

        inotify.close()

    # ── Polling loop (fallback) ───────────────────────────────────────────────

    def _run_poll(self):
        self.log.info(
            f"[watcher] poll mode (inotify_simple not installed) — "
            f"interval={self.POLL_INTERVAL}s"
        )
        while self._running:
            for base in self.paths:
                it = base.rglob("*") if self.recursive else base.glob("*")
                for item in it:
                    if not item.is_file():
                        continue
                    try:
                        mtime = item.stat().st_mtime
                    except Exception:
                        continue
                    key = str(item)
                    if self._seen.get(key) != mtime:
                        self._seen[key] = mtime
                        self.dispatcher.queue(key)
            time.sleep(self.POLL_INTERVAL)

    # ── Signal handlers ───────────────────────────────────────────────────────

    def _handle_sigterm(self, *_):
        self.log.info("[watcher] SIGTERM — shutting down.")
        self._running = False

    def _handle_sighup(self, *_):
        """SIGHUP: re-pull from git and re-scan."""
        self.log.info("[watcher] SIGHUP — pulling and rescanning.")
        if self.pull:
            self._pull_all()
        self._startup_scan()

    # ── Main run ──────────────────────────────────────────────────────────────

    def run(self):
        self._running = True

        signal.signal(signal.SIGTERM, self._handle_sigterm)
        signal.signal(signal.SIGHUP,  self._handle_sighup)

        self.log.info(
            f"[watcher] Phoenix Watcher v{WATCHER_VERSION} — "
            f"paths={[str(p) for p in self.paths]} "
            f"recursive={self.recursive} pull={self.pull}"
        )

        if self.pull:
            self._pull_all()

        self._startup_scan()

        try:
            if HAVE_INOTIFY:
                self._run_inotify()
            else:
                self._run_poll()
        except KeyboardInterrupt:
            self.log.info("[watcher] KeyboardInterrupt — shutting down.")
        finally:
            self._running = False
            self.dispatcher.stop()
            self.log.info("[watcher] stopped.")


# ── CLI entry point ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        prog="watch-phoenix",
        description="Auto-intake watcher — every file that lands gets intaked.",
    )
    parser.add_argument(
        "--path", action="append", dest="paths", metavar="DIR",
        help="Directory to watch (repeatable). Default: ~/Phoenix"
    )
    parser.add_argument(
        "--no-recursive", action="store_true",
        help="Flat watch only (no subdirectories)"
    )
    parser.add_argument(
        "--pull", action="store_true",
        help="git pull before starting and on SIGHUP"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Debug logging"
    )
    args = parser.parse_args()

    watch_paths = []
    if args.paths:
        for p in args.paths:
            resolved = Path(p).expanduser().resolve()
            if not resolved.exists():
                print(f"[watch-phoenix] path not found: {resolved}", file=sys.stderr)
                sys.exit(1)
            watch_paths.append(resolved)
    else:
        watch_paths = [PHOENIX_HOME]

    watcher = HelixWatcher(
        paths=watch_paths,
        recursive=not args.no_recursive,
        pull=args.pull,
        verbose=args.verbose,
    )
    watcher.run()


if __name__ == "__main__":
    main()
