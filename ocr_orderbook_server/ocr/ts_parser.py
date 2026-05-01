"""
ts_parser.py

Reads the Time & Sales panel from a captured frame, classifies each row as
BUY or SELL by background color, OCRs the size column, then computes OBI
from a rolling 50-print window.

Color classification (proven in mt5_bot_v7):
  Red  (R>140 AND R>G*1.8 AND R>B*1.8) → SELL aggressor print
  Gray/bright (mean grayscale > threshold, non-red)  → BUY  aggressor print
  Black / empty → skip

Output published to shared state:
  {"timestamp": <unix>, "obi": <float -1 to 1>, "print_count": <int>,
   "buy_volume": <int>, "sell_volume": <int>}
"""

import logging
import os
import re
import time
from collections import deque

import cv2
import numpy as np
import pytesseract

logger = logging.getLogger("ts_parser")


class TSParser:
    def __init__(self, cfg: dict):
        ocr_cfg = cfg["ocr"]
        ts      = ocr_cfg["ts"]

        self.crop_left    = ts["crop_left"]
        self.crop_right   = ts["crop_right"]
        self.window_size  = ts["window_size"]
        self.min_rows     = ts["min_valid_rows"]
        self.header_skip  = ts["header_skip"]
        self.row_h        = ts["row_height"]
        self.red_pct      = ts["red_pct"]
        self.buy_bright   = ts["buy_bright"]
        self.ocr_right_px = ts["ocr_right_px"]
        self.price_min    = ts["price_min"]
        self.price_max    = ts["price_max"]
        self.debug        = ts["debug"]

        self.shots_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            cfg["capture"]["shots_dir"],
        )

        pytesseract.pytesseract.tesseract_cmd = ocr_cfg["tesseract_path"]

        self._print_window: deque = deque(maxlen=self.window_size)
        self._frame_count  = 0
        self._debug_saved  = False

    # ── public ────────────────────────────────────────────────────────────────

    def process_frame(self, frame_path: str) -> dict | None:
        """
        Parse one frame. Returns result dict or None if frame is invalid.
        None means: keep the previous OBI value unchanged.
        """
        img = cv2.imread(frame_path)
        if img is None:
            logger.warning("Cannot read frame: %s", frame_path)
            return None

        full_w = img.shape[1]
        x0     = int(full_w * self.crop_left)
        x1     = int(full_w * self.crop_right)
        panel  = img[:, x0:x1]

        if not self._debug_saved:
            self._debug_saved = True
            self._save_debug(panel, "debug_ts_crop.png")
            logger.info("T&S crop saved (panel %dx%d, x=%d–%d of %dpx frame)",
                        panel.shape[1], panel.shape[0], x0, x1, full_w)

        self._frame_count += 1
        h = panel.shape[0]
        segments = self._detect_segments(panel, h)

        prints      = []
        debug_img   = panel.copy() if (self.debug and self._frame_count % 10 == 0) else None

        for y1, y2, side in segments:
            row_bgr     = panel[y1:y2, :]
            size, price = self._ocr_row(row_bgr)
            if size is None or size <= 0:
                continue
            prints.append({"side": side, "size": size, "price": price})

            if debug_img is not None:
                color = (0, 180, 0) if side == "buy" else (0, 0, 200)
                cv2.rectangle(debug_img, (0, y1), (panel.shape[1] - 1, y2), color, 1)
                lbl = f"{side[0].upper()} {size}"
                cv2.putText(debug_img, lbl, (2, y1 + self.row_h - 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.28, color, 1)

        if debug_img is not None:
            self._save_debug(debug_img, "debug_ts_classified.png")

        if len(prints) < self.min_rows:
            logger.debug("T&S: insufficient rows (%d/%d) — skipping frame",
                         len(prints), self.min_rows)
            return None

        # Extend rolling window
        self._print_window.extend(prints)

        buy_vol  = sum(p["size"] for p in self._print_window if p["side"] == "buy")
        sell_vol = sum(p["size"] for p in self._print_window if p["side"] == "sell")
        total    = buy_vol + sell_vol

        # Sanity: abnormal cumulative volume → wipe window
        if buy_vol > 100_000 or sell_vol > 100_000:
            logger.warning("T&S: abnormal volume (buy=%d sell=%d) — clearing window",
                           buy_vol, sell_vol)
            self._print_window.clear()
            return None

        obi = round(buy_vol / total, 4) if total > 0 else 0.5

        if self._frame_count % 10 == 0:
            bias = "BULL" if obi > 0.5 else ("BEAR" if obi < 0.5 else "NEUT")
            logger.info("T&S obi=%.3f (%s) window=%d/%d buy_vol=%d sell_vol=%d",
                        obi, bias, len(self._print_window), self.window_size,
                        buy_vol, sell_vol)

        return {
            "timestamp":   time.time(),
            "obi":         obi,
            "print_count": len(self._print_window),
            "buy_volume":  buy_vol,
            "sell_volume": sell_vol,
        }

    # ── private ───────────────────────────────────────────────────────────────

    def _detect_segments(self, img_bgr, h):
        """
        Fixed-grid scan: rows are ROW_H px tall, starting after HEADER_SKIP.
        Red mask:  R>140 AND R>G*1.8 AND R>B*1.8
        BUY proxy: mean grayscale > BUY_BRIGHT (and not red)
        """
        segments = []
        r_ch = img_bgr[:, :, 2].astype(np.float32)
        g_ch = img_bgr[:, :, 1].astype(np.float32)
        b_ch = img_bgr[:, :, 0].astype(np.float32)
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)

        total_px = img_bgr.shape[1] * self.row_h

        y = self.header_skip
        while y + self.row_h <= h:
            y1, y2 = y, y + self.row_h
            y = y2

            sr = r_ch[y1:y2, :]
            sg = g_ch[y1:y2, :]
            sb = b_ch[y1:y2, :]
            sg_gray = gray[y1:y2, :]

            red_mask  = (sr > 140) & (sr > sg * 1.8) & (sr > sb * 1.8)
            red_ratio = float(np.sum(red_mask)) / (total_px + 1e-6)
            mean_gray = float(np.mean(sg_gray))

            if red_ratio > self.red_pct:
                segments.append((y1, y2, "sell"))
            elif mean_gray > self.buy_bright:
                segments.append((y1, y2, "buy"))

        return segments

    def _ocr_row(self, row_bgr):
        if row_bgr.size == 0:
            return None, None
        w    = row_bgr.shape[1]
        crop = row_bgr[:, max(0, w - self.ocr_right_px):]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        big  = cv2.resize(gray, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC)
        _, th = cv2.threshold(big, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        if np.mean(th) < 128:
            th = cv2.bitwise_not(th)
        cfg  = "--psm 8 --oem 3 -c tessedit_char_whitelist=0123456789"
        text = pytesseract.image_to_string(th, config=cfg).strip()
        return self._extract_size(text)

    def _extract_size(self, text):
        price = None
        size  = None
        for tok in text.split():
            tok = tok.strip(".")
            if not tok:
                continue
            if re.fullmatch(r"\d+", tok):
                val = int(tok)
                if self.price_min <= val <= self.price_max:
                    price = float(val)
                elif 1 <= val <= 999:
                    size = val
            elif re.fullmatch(r"\d+\.\d+", tok):
                try:
                    val = float(tok)
                    if self.price_min <= val <= self.price_max:
                        price = val
                except ValueError:
                    pass
        return size, price

    def _save_debug(self, img, filename: str):
        try:
            cv2.imwrite(os.path.join(self.shots_dir, filename), img)
        except Exception:
            pass


def ts_process_main(cfg: dict, shared):
    """Entry point for the T&S OCR Process."""
    import signal
    signal.signal(signal.SIGTERM, lambda *_: None)

    logging.getLogger("ts_parser").setLevel(
        getattr(logging, cfg["logging"]["level"], logging.INFO)
    )

    from capture.frame_buffer import get_frame, set_result, ping

    parser = TSParser(cfg)
    stale_threshold = cfg["server"]["stale_threshold"]
    last_frame_path = ""

    logger.info("T&S parser process started")

    while True:
        try:
            frame_path, frame_ts = get_frame(shared)

            if not frame_path or not os.path.exists(frame_path):
                time.sleep(0.5)
                continue

            # Skip if we already processed this frame
            if frame_path == last_frame_path:
                time.sleep(0.1)
                continue

            # Skip stale frames
            if time.time() - frame_ts > stale_threshold:
                time.sleep(0.5)
                continue

            last_frame_path = frame_path
            result = parser.process_frame(frame_path)

            if result is not None:
                set_result(shared, "ts", result)
            else:
                ping(shared, "ts")

        except Exception as exc:
            logger.error("T&S loop error: %s", exc, exc_info=True)
            time.sleep(1)
