"""
main.py

OCR Order Book Server — entry point.

Spawns five independent processes:
  1. stream_capture  — yt-dlp + ffmpeg, writes frames to shots/
  2. ts_parser       — T&S OBI calculator
  3. delta_parser    — Footprint color classifier
  4. cob_reader      — COB wall detector
  5. data_server     — Flask HTTP API + web dashboard

All processes communicate through a shared Manager dict (frame_buffer).
Each OCR process restarts automatically if it crashes.

Usage:
  python main.py
  python main.py --config path/to/config.yaml
  python main.py --log-level DEBUG
"""

import argparse
import logging
import logging.handlers
import multiprocessing
import os
import signal
import sys
import time

import yaml

# ── Windows multiprocessing guard ─────────────────────────────────────────────
if __name__ == "__main__":
    multiprocessing.freeze_support()


# ── Logging setup ──────────────────────────────────────────────────────────────

def setup_logging(cfg: dict):
    log_dir   = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             cfg["logging"]["log_dir"])
    os.makedirs(log_dir, exist_ok=True)

    level = getattr(logging, cfg["logging"]["level"], logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-7s] %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Rotating main log
    main_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "ocr_server.log"),
        maxBytes=cfg["logging"]["max_bytes"],
        backupCount=cfg["logging"]["backup_count"],
    )
    main_handler.setFormatter(fmt)

    # Separate error log
    err_handler = logging.handlers.RotatingFileHandler(
        os.path.join(log_dir, "error.log"),
        maxBytes=cfg["logging"]["max_bytes"],
        backupCount=cfg["logging"]["backup_count"],
    )
    err_handler.setLevel(logging.ERROR)
    err_handler.setFormatter(fmt)

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(main_handler)
    root.addHandler(err_handler)
    root.addHandler(console)

    return root


# ── Process wrappers ───────────────────────────────────────────────────────────

def _run_capture(cfg, shared):
    setup_logging(cfg)
    from capture.stream_capture import capture_process_main
    capture_process_main(cfg, shared)


def _run_ts(cfg, shared):
    setup_logging(cfg)
    from ocr.ts_parser import ts_process_main
    ts_process_main(cfg, shared)


def _run_delta(cfg, shared):
    setup_logging(cfg)
    from ocr.delta_parser import delta_process_main
    delta_process_main(cfg, shared)


def _run_cob(cfg, shared):
    setup_logging(cfg)
    from ocr.cob_reader import cob_process_main
    cob_process_main(cfg, shared)


def _run_server(cfg, shared):
    setup_logging(cfg)
    from server.data_server import server_process_main
    server_process_main(cfg, shared)


# ── Process table ──────────────────────────────────────────────────────────────

WORKERS = [
    ("stream_capture", _run_capture),
    ("ts_parser",      _run_ts),
    ("delta_parser",   _run_delta),
    ("cob_reader",     _run_cob),
    ("data_server",    _run_server),
]


# ── Supervisor ─────────────────────────────────────────────────────────────────

class Supervisor:
    """
    Starts all worker processes and auto-restarts them if they crash.
    Shuts everything down cleanly on SIGINT / SIGTERM.
    """

    def __init__(self, cfg: dict, shared):
        self.cfg         = cfg
        self.shared      = shared
        self._processes: dict[str, multiprocessing.Process] = {}
        self._running    = True
        self._log        = logging.getLogger("supervisor")

    def start_all(self):
        for name, target in WORKERS:
            self._spawn(name, target)

    def _spawn(self, name: str, target):
        p = multiprocessing.Process(
            target=target,
            args=(self.cfg, self.shared),
            name=name,
            daemon=False,
        )
        p.start()
        self._processes[name] = p
        self._log.info("Started process %s (pid=%d)", name, p.pid)

    def supervise(self):
        """Main supervision loop — runs until shutdown."""
        worker_map = dict(WORKERS)

        while self._running:
            time.sleep(2)
            for name, target in WORKERS:
                p = self._processes.get(name)
                if p is None:
                    continue
                if not p.is_alive():
                    exit_code = p.exitcode
                    self._log.warning(
                        "Process %s died (exitcode=%s) — restarting",
                        name, exit_code,
                    )
                    p.close()
                    self._spawn(name, worker_map[name])

    def shutdown(self, signum=None, frame=None):
        if not self._running:
            return
        self._running = False
        self._log.info("Shutdown signal received — stopping all processes...")
        for name, p in self._processes.items():
            if p.is_alive():
                self._log.info("Terminating %s (pid=%d)...", name, p.pid)
                p.terminate()
        # Give processes 5 s to exit gracefully
        deadline = time.time() + 5.0
        for name, p in self._processes.items():
            remaining = max(0, deadline - time.time())
            p.join(timeout=remaining)
            if p.is_alive():
                self._log.warning("Force-killing %s", name)
                p.kill()
        self._log.info("All processes stopped. Bye.")
        sys.exit(0)


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OCR Order Book Server")
    parser.add_argument("--config",    default="config.yaml",
                        help="Path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--log-level", default=None,
                        choices=["DEBUG", "INFO", "ERROR"],
                        help="Override log level from config")
    args = parser.parse_args()

    # Resolve config path relative to this file
    config_path = args.config
    if not os.path.isabs(config_path):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   config_path)

    if not os.path.exists(config_path):
        print(f"ERROR: config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)

    with open(config_path) as f:
        cfg = yaml.safe_load(f)

    if args.log_level:
        cfg["logging"]["level"] = args.log_level

    setup_logging(cfg)
    log = logging.getLogger("main")

    log.info("=" * 60)
    log.info("OCR Order Book Server starting")
    log.info("Config: %s", config_path)
    log.info("Stream: %s", cfg["youtube"]["url"])
    log.info("HTTP:   http://%s:%d", cfg["server"]["host"], cfg["server"]["port"])
    log.info("=" * 60)

    # Ensure shots dir exists
    shots_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             cfg["capture"]["shots_dir"])
    os.makedirs(shots_dir, exist_ok=True)

    # Build shared state
    manager = multiprocessing.Manager()
    from capture.frame_buffer import make_shared_state
    shared = make_shared_state(manager)

    # Start supervisor
    supervisor = Supervisor(cfg, shared)
    signal.signal(signal.SIGINT,  supervisor.shutdown)
    signal.signal(signal.SIGTERM, supervisor.shutdown)

    supervisor.start_all()

    log.info("All processes started. Dashboard: http://localhost:%d/",
             cfg["server"]["port"])
    log.info("Press Ctrl+C to stop.\n")

    try:
        supervisor.supervise()
    except KeyboardInterrupt:
        supervisor.shutdown()


if __name__ == "__main__":
    main()
