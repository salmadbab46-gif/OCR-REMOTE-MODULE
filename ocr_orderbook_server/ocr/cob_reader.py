"""
cob_reader.py

Reads the Cumulative Order Book (COB) panel.

Two-stage pipeline:
  1. Green highlight detection to find the current price row.
  2. Tesseract OCR (upscaled, thresholded) to read price+size pairs.
  3. Wall detection: any row whose size exceeds wall_multiplier × median size
     (and at least min_wall_size lots) is flagged as a wall.

Output published to shared state:
  {
    "timestamp": <unix>,
    "ask_walls": [{"price": <float>, "size": <int>}, ...],
    "bid_walls": [{"price": <float>, "size": <int>}, ...]
  }
Ask walls = rows ABOVE the current price row.
Bid walls = rows BELOW the current price row.
"""

import logging
import os
import re
import statistics
import time

import cv2
import numpy as np
import pytesseract

logger = logging.getLogger("cob_reader")


class COBReader:
    def __init__(self, cfg: dict):
        c = cfg["ocr"]["cob"]

        self.crop_left   = c["crop_left"]
        self.crop_right  = c["crop_right"]
        self.wall_mult   = c["wall_multiplier"]
        self.min_wall    = c["min_wall_size"]
        self.scale       = c["ocr_scale"]
        self.price_min   = c["price_min"]
        self.price_max   = c["price_max"]
        self.debug       = c["debug"]

        self.shots_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            cfg["capture"]["shots_dir"],
        )

        pytesseract.pytesseract.tesseract_cmd = cfg["ocr"]["tesseract_path"]

        self._debug_saved = False
        self._frame_count = 0
        self._line_pat    = re.compile(r"(\d{4,5}[.,]\d)(\d+)")

    # ── public ────────────────────────────────────────────────────────────────

    def process_frame(self, frame_path: str) -> dict:
        img = cv2.imread(frame_path)
        if img is None:
            logger.warning("Cannot read frame: %s", frame_path)
            return self._empty()

        h_full, w_full = img.shape[:2]
        x0 = int(w_full * self.crop_left)
        x1 = int(w_full * self.crop_right)
        cob = img[:, x0:x1]

        if not self._debug_saved:
            self._debug_saved = True
            self._save(cob, "debug_cob_crop.png")
            logger.info("COB crop saved (%dx%d, x=%d–%d of %dpx frame)",
                        cob.shape[1], cob.shape[0], x0, x1, w_full)

        self._frame_count += 1
        h_cob, w_cob = cob.shape[:2]

        # ── Find current-price row via green/teal highlight ───────────────────
        hsv_cob  = cv2.cvtColor(cob, cv2.COLOR_BGR2HSV)
        hl_mask  = cv2.inRange(hsv_cob, (60, 80, 80), (100, 255, 255))
        hl_rows  = np.where(hl_mask.sum(axis=1) > 10)[0]
        price_y  = int(hl_rows.mean()) if len(hl_rows) > 0 else (h_cob // 2)

        # ── OCR ───────────────────────────────────────────────────────────────
        scaled = cv2.resize(cob, (w_cob * self.scale, h_cob * self.scale),
                            interpolation=cv2.INTER_LINEAR)
        gray   = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
        _, th  = cv2.threshold(gray, 60, 255, cv2.THRESH_BINARY)

        raw_text = pytesseract.image_to_string(
            th, config="--oem 3 --psm 6 outputbase digits"
        )

        # ── Parse rows ────────────────────────────────────────────────────────
        lines = raw_text.splitlines()
        total_lines = max(len(lines), 1)
        rows_parsed = []

        for line_num, line in enumerate(lines):
            line = line.strip().replace(",", ".")
            m = self._line_pat.search(line)
            if not m:
                continue
            try:
                price = float(m.group(1))
                size  = int(m.group(2))
            except ValueError:
                continue
            if not (self.price_min < price < self.price_max) or size <= 0:
                continue
            y_est = int((line_num / total_lines) * h_cob)
            rows_parsed.append((price, size, y_est))

        # ── Wall detection ────────────────────────────────────────────────────
        ask_walls: list[dict] = []
        bid_walls: list[dict] = []

        if len(rows_parsed) >= 3:
            median_sz    = statistics.median(r[1] for r in rows_parsed)
            threshold    = max(self.wall_mult * median_sz, self.min_wall)

            for price, size, y_est in rows_parsed:
                if size >= threshold:
                    entry = {"price": price, "size": size}
                    if y_est < price_y:
                        ask_walls.append(entry)
                    else:
                        bid_walls.append(entry)

            if self._frame_count % 5 == 0:
                logger.info(
                    "COB rows=%d median=%.0f threshold=%.0f "
                    "ask_walls=%d bid_walls=%d price_y=%d",
                    len(rows_parsed), median_sz, threshold,
                    len(ask_walls), len(bid_walls), price_y,
                )
        else:
            logger.debug("COB: too few rows (%d) — raw sample: %r",
                         len(rows_parsed), raw_text[:120])

        return {
            "timestamp": time.time(),
            "ask_walls": ask_walls,
            "bid_walls": bid_walls,
        }

    # ── private ───────────────────────────────────────────────────────────────

    @staticmethod
    def _empty() -> dict:
        return {"timestamp": time.time(), "ask_walls": [], "bid_walls": []}

    def _save(self, img, filename: str):
        try:
            cv2.imwrite(os.path.join(self.shots_dir, filename), img)
        except Exception:
            pass


def cob_process_main(cfg: dict, shared):
    """Entry point for the COB reader Process."""
    import signal
    signal.signal(signal.SIGTERM, lambda *_: None)

    logging.getLogger("cob_reader").setLevel(
        getattr(logging, cfg["logging"]["level"], logging.INFO)
    )

    from capture.frame_buffer import get_frame, set_result, ping

    reader = COBReader(cfg)
    stale_threshold = cfg["server"]["stale_threshold"]
    last_frame_path = ""

    logger.info("COB reader process started")

    while True:
        try:
            frame_path, frame_ts = get_frame(shared)

            if not frame_path or not os.path.exists(frame_path):
                time.sleep(0.5)
                continue

            if frame_path == last_frame_path:
                time.sleep(0.5)
                continue

            if time.time() - frame_ts > stale_threshold:
                time.sleep(0.5)
                continue

            last_frame_path = frame_path
            result = reader.process_frame(frame_path)
            set_result(shared, "cob", result)

        except Exception as exc:
            logger.error("COB loop error: %s", exc, exc_info=True)
            time.sleep(2)
