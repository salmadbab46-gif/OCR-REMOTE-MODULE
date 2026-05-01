"""
stream_capture.py

Runs as a separate process. Continuously fetches frames from a YouTube live
stream using yt-dlp + ffmpeg and writes them to the shots/ directory.

On stream failure it backs off exponentially (up to max_reconnect_delay)
then retries. The latest frame path is always available via frame_buffer.
"""

import logging
import os
import subprocess
import time
from datetime import datetime

logger = logging.getLogger("stream_capture")


class StreamCapture:
    def __init__(self, cfg: dict):
        self.youtube_url      = cfg["youtube"]["url"]
        self.stream_url_ttl   = cfg["youtube"]["stream_url_ttl"]
        self.shots_dir        = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            cfg["capture"]["shots_dir"]
        )
        self.max_frame_age    = cfg["capture"]["max_frame_age"]
        self.ffmpeg_timeout   = cfg["capture"]["ffmpeg_timeout"]
        self.reconnect_delay  = cfg["capture"]["reconnect_delay"]
        self.max_reconnect    = cfg["capture"]["max_reconnect_delay"]

        os.makedirs(self.shots_dir, exist_ok=True)

        self._stream_url: str   = ""
        self._url_fetched_at: float = 0.0

    # ── public ────────────────────────────────────────────────────────────────

    def run(self, shared):
        """Main loop. Runs forever; designed to be the target of a Process."""
        import signal
        signal.signal(signal.SIGTERM, lambda *_: None)  # let parent kill cleanly

        from capture.frame_buffer import set_frame, ping

        delay = self.reconnect_delay
        consecutive_failures = 0

        logger.info("Stream capture process started. Target: %s", self.youtube_url)

        while True:
            try:
                url = self._get_stream_url()
                if not url:
                    logger.warning("Could not resolve stream URL — retrying in %ds", delay)
                    time.sleep(delay)
                    delay = min(delay * 2, self.max_reconnect)
                    continue

                path = self._capture_frame(url)
                if not path:
                    consecutive_failures += 1
                    logger.warning("Frame capture failed (#%d) — invalidating URL cache",
                                   consecutive_failures)
                    self._stream_url = ""  # force URL refresh
                    if consecutive_failures >= 3:
                        logger.warning("3 consecutive failures — backing off %ds", delay)
                        time.sleep(delay)
                        delay = min(delay * 2, self.max_reconnect)
                    continue

                consecutive_failures = 0
                delay = self.reconnect_delay  # reset backoff on success
                set_frame(shared, path)
                ping(shared, "stream")
                self._cleanup_old_frames()

            except Exception as exc:
                logger.error("Capture loop error: %s", exc, exc_info=True)
                time.sleep(5)

    # ── private ───────────────────────────────────────────────────────────────

    def _get_stream_url(self) -> str:
        now = time.time()
        if self._stream_url and (now - self._url_fetched_at) < self.stream_url_ttl:
            return self._stream_url

        logger.info("Resolving YouTube stream URL via yt-dlp...")
        try:
            import yt_dlp
            opts = {
                "format": "bestvideo[ext=mp4]/bestvideo/b",
                "quiet": True,
                "no_warnings": True,
                "socket_timeout": 20,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.youtube_url, download=False)
                url = info.get("url") or (
                    (info.get("formats") or [{}])[-1].get("url", "")
                )
            if url:
                self._stream_url    = url
                self._url_fetched_at = now
                logger.info("Stream URL resolved (TTL %ds)", self.stream_url_ttl)
                return url
            logger.warning("yt-dlp returned no URL")
        except Exception as exc:
            logger.error("yt-dlp error: %s", exc)
        return ""

    def _capture_frame(self, stream_url: str) -> str:
        ts       = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filepath = os.path.join(self.shots_dir, f"shot_{ts}.png")
        cmd = [
            "ffmpeg", "-y",
            "-timeout",            "30000000",   # socket timeout µs
            "-reconnect",          "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max","5",
            "-i",                  stream_url,
            "-frames:v",           "1",
            "-vf",                 "scale=3840:-1",
            filepath,
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.ffmpeg_timeout,
            )
            if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
                logger.debug("Captured frame: %s", os.path.basename(filepath))
                return filepath
            logger.warning("ffmpeg produced no output. stderr: %s",
                           result.stderr[-300:])
        except subprocess.TimeoutExpired:
            logger.warning("ffmpeg timed out after %ds", self.ffmpeg_timeout)
        except FileNotFoundError:
            logger.error("ffmpeg not found in PATH — install ffmpeg and retry")
        except Exception as exc:
            logger.error("ffmpeg error: %s", exc)
        return ""

    def _cleanup_old_frames(self):
        cutoff = time.time() - self.max_frame_age
        try:
            for fn in os.listdir(self.shots_dir):
                if not fn.endswith(".png") or not fn.startswith("shot_"):
                    continue
                fp = os.path.join(self.shots_dir, fn)
                if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                    try:
                        os.remove(fp)
                    except OSError:
                        pass
        except Exception:
            pass


def capture_process_main(cfg: dict, shared):
    """Entry point for the stream capture Process."""
    import logging
    logging.getLogger("stream_capture").setLevel(
        getattr(logging, cfg["logging"]["level"], logging.INFO)
    )
    worker = StreamCapture(cfg)
    worker.run(shared)
