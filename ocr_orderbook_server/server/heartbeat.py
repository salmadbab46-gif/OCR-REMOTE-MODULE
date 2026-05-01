"""
heartbeat.py

Runs as a background thread inside the HTTP server process.
Periodically updates the server heartbeat timestamp so that consuming bots
can tell whether the OCR server is alive.

Also logs a one-line status summary every heartbeat_interval seconds.
"""

import logging
import threading
import time

logger = logging.getLogger("heartbeat")


class Heartbeat:
    def __init__(self, shared, cfg: dict):
        self.shared    = shared
        self.interval  = cfg["server"]["heartbeat_interval"]
        self.threshold = cfg["server"]["stale_threshold"]
        self._thread   = threading.Thread(target=self._loop, daemon=True,
                                          name="heartbeat")

    def start(self):
        self._thread.start()
        logger.info("Heartbeat thread started (interval=%ds)", self.interval)

    def _loop(self):
        from capture.frame_buffer import is_alive

        while True:
            try:
                now = time.time()
                self.shared["heartbeat_ts"] = now

                stream_ok = is_alive(self.shared, "stream", self.threshold)
                ts_ok     = is_alive(self.shared, "ts",     self.threshold)
                delta_ok  = is_alive(self.shared, "delta",  self.threshold)
                cob_ok    = is_alive(self.shared, "cob",    self.threshold)

                def sym(ok): return "OK" if ok else "DEAD"
                logger.info("♥ stream=%s  ts=%s  delta=%s  cob=%s",
                            sym(stream_ok), sym(ts_ok),
                            sym(delta_ok),  sym(cob_ok))

            except Exception as exc:
                logger.error("Heartbeat error: %s", exc)

            time.sleep(self.interval)
