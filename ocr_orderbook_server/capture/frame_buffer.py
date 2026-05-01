"""
frame_buffer.py

Thin wrapper around a multiprocessing.Manager dict that acts as the shared
frame bus between the capture process and the three OCR workers.

Schema of the shared dict:
  frame_path      str   - absolute path to the latest captured frame
  frame_ts        float - unix time the latest frame was written
  ts_result       str   - JSON payload from ts_parser (or empty string)
  delta_result    str   - JSON payload from delta_parser (or empty string)
  cob_result      str   - JSON payload from cob_reader (or empty string)
  stream_alive_ts float - last heartbeat from stream capture process
  ts_alive_ts     float - last heartbeat from T&S OCR process
  delta_alive_ts  float - last heartbeat from delta OCR process
  cob_alive_ts    float - last heartbeat from COB OCR process
"""

import json
import time


def make_shared_state(manager):
    """Create and return the initial shared state dict via a Manager."""
    empty_ts = json.dumps({
        "timestamp": 0,
        "obi": 0.5,
        "print_count": 0,
        "buy_volume": 0,
        "sell_volume": 0,
    })
    empty_delta = json.dumps({
        "timestamp": 0,
        "delta": "UNKNOWN",
        "confidence": 0.0,
        "red_ratio": 0.0,
        "green_ratio": 0.0,
    })
    empty_cob = json.dumps({
        "timestamp": 0,
        "ask_walls": [],
        "bid_walls": [],
    })
    return manager.dict({
        "frame_path":      "",
        "frame_ts":        0.0,
        "ts_result":       empty_ts,
        "delta_result":    empty_delta,
        "cob_result":      empty_cob,
        "stream_alive_ts": 0.0,
        "ts_alive_ts":     0.0,
        "delta_alive_ts":  0.0,
        "cob_alive_ts":    0.0,
    })


def set_frame(shared, path: str):
    shared["frame_path"] = path
    shared["frame_ts"] = time.time()
    shared["stream_alive_ts"] = time.time()


def get_frame(shared) -> tuple[str, float]:
    return shared["frame_path"], shared["frame_ts"]


def set_result(shared, channel: str, payload: dict):
    """channel must be 'ts', 'delta', or 'cob'."""
    shared[f"{channel}_result"] = json.dumps(payload)
    shared[f"{channel}_alive_ts"] = time.time()


def get_result(shared, channel: str) -> dict:
    raw = shared.get(f"{channel}_result", "{}")
    try:
        return json.loads(raw)
    except Exception:
        return {}


def ping(shared, channel: str):
    """Update the heartbeat timestamp for a channel."""
    shared[f"{channel}_alive_ts"] = time.time()


def is_alive(shared, channel: str, threshold: float = 30.0) -> bool:
    ts = shared.get(f"{channel}_alive_ts", 0.0)
    return (time.time() - ts) < threshold
