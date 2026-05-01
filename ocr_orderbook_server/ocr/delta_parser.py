"""
delta_parser.py

Classifies the Footprints M15 Delta row by HSV color pixel dominance.
No Tesseract needed — pure pixel counting.

Green pixels (H 35-90, S>80, V>80)  → BUY delta
Red/purple   (H 0-15 or 140-180)     → SELL delta

Output published to shared state:
  {"timestamp": <unix>, "delta": "BULL"|"BEAR", "confidence": <float 0-1>,
   "red_ratio": <float>, "green_ratio": <float>}

Binary only: BULL or BEAR. No MIXED / BLOCK category — if pixel counts are
equal, whichever is strictly greater wins; true 50/50 defaults to BEAR.
"""

import logging
import os
import time

import cv2
import numpy as np

logger = logging.getLogger("delta_parser")


class DeltaParser:
    def __init__(self, cfg: dict):
        d = cfg["ocr"]["delta"]

        self.crop_left    = d["crop_left"]
        self.crop_right   = d["crop_right"]
        self.row_top      = d["delta_row_top"]
        self.row_bot      = d["delta_row_bot"]
        self.min_colored  = d["min_colored_pixels"]
        self.label_skip   = d["label_skip_px"]
        self.debug        = d["debug"]

        self.shots_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            cfg["capture"]["shots_dir"],
        )

        self._debug_saved  = False
        self._frame_count  = 0

    # ── public ────────────────────────────────────────────────────────────────

    def process_frame(self, frame_path: str) -> dict:
        img = cv2.imread(frame_path)
        if img is None:
            logger.warning("Cannot read frame: %s", frame_path)
            return self._fallback()

        full_w = img.shape[1]
        x0     = int(full_w * self.crop_left)
        x1     = int(full_w * self.crop_right)
        panel  = img[:, x0:x1]

        ph     = panel.shape[0]
        ry0    = int(ph * self.row_top)
        ry1    = int(ph * self.row_bot)
        delta_row = panel[ry0:ry1, :]

        if not self._debug_saved:
            self._debug_saved = True
            self._save(panel,     "debug_footprint_crop.png")
            self._save(delta_row, "debug_footprint_delta_row.png")
            logger.info("Delta debug crops saved (panel %dx%d, delta row %dx%d)",
                        panel.shape[1], panel.shape[0],
                        delta_row.shape[1], delta_row.shape[0])

        self._frame_count += 1

        # Top 30 rows of the delta strip; skip label column
        strip = delta_row[:30, self.label_skip:]
        if strip.size == 0:
            return self._fallback()

        hsv = cv2.cvtColor(strip, cv2.COLOR_BGR2HSV)
        h   = hsv[:, :, 0].astype(np.int32)
        s   = hsv[:, :, 1].astype(np.int32)
        v   = hsv[:, :, 2].astype(np.int32)

        vivid  = (s > 80) & (v > 80)
        red    = vivid & ((h <= 15) | (h >= 165))
        green  = vivid & (h >= 35) & (h <= 90)
        purple = vivid & (h >= 140) & (h <= 165)

        rc = int(np.sum(red))
        gc = int(np.sum(green))
        pc = int(np.sum(purple))
        total = rc + gc + pc

        if total < self.min_colored:
            logger.debug("Delta: insufficient colored pixels (%d) — using BEAR default", total)
            return self._fallback()

        sell_count = rc + pc
        denom      = total + 1
        red_ratio  = round(sell_count / denom, 4)
        grn_ratio  = round(gc / denom, 4)

        # Binary classification — strict greater-than; ties → BEAR
        if gc > sell_count:
            signal     = "BULL"
            confidence = round(grn_ratio / (grn_ratio + red_ratio + 1e-6), 4)
        else:
            signal     = "BEAR"
            confidence = round(red_ratio / (grn_ratio + red_ratio + 1e-6), 4)

        if self._frame_count % 5 == 0:
            logger.info("Delta %s  red_ratio=%.3f  green_ratio=%.3f  confidence=%.3f",
                        signal, red_ratio, grn_ratio, confidence)

        return {
            "timestamp":   time.time(),
            "delta":       signal,
            "confidence":  confidence,
            "red_ratio":   red_ratio,
            "green_ratio": grn_ratio,
        }

    # ── private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _fallback() -> dict:
        return {
            "timestamp":   time.time(),
            "delta":       "BEAR",
            "confidence":  0.0,
            "red_ratio":   0.0,
            "green_ratio": 0.0,
        }

    def _save(self, img, filename: str):
        try:
            cv2.imwrite(os.path.join(self.shots_dir, filename), img)
        except Exception:
            pass


def delta_process_main(cfg: dict, shared):
    """Entry point for the delta OCR Process."""
    import signal
    signal.signal(signal.SIGTERM, lambda *_: None)

    logging.getLogger("delta_parser").setLevel(
        getattr(logging, cfg["logging"]["level"], logging.INFO)
    )

    from capture.frame_buffer import get_frame, set_result, ping

    parser = DeltaParser(cfg)
    stale_threshold = cfg["server"]["stale_threshold"]
    last_frame_path = ""

    logger.info("Delta parser process started")

    while True:
        try:
            frame_path, frame_ts = get_frame(shared)

            if not frame_path or not os.path.exists(frame_path):
                time.sleep(0.5)
                continue

            if frame_path == last_frame_path:
                time.sleep(0.2)
                continue

            if time.time() - frame_ts > stale_threshold:
                time.sleep(0.5)
                continue

            last_frame_path = frame_path
            result = parser.process_frame(frame_path)
            set_result(shared, "delta", result)

        except Exception as exc:
            logger.error("Delta loop error: %s", exc, exc_info=True)
            time.sleep(1)
