You are setting up a complete OCR Order Book Server on this Windows laptop. This folder contains all the source code. Do every step below automatically. Do not ask questions. Make all decisions yourself. If a step fails, fix it and continue. Do not stop until every step is done.

---

## STEP 1 — Install ffmpeg

Check if already installed:
```
ffmpeg -version
```
If NOT installed run:
```
winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
```
If winget fails, run:
```
powershell -Command "Invoke-WebRequest -Uri 'https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip' -OutFile 'C:\ffmpeg.zip'; Expand-Archive 'C:\ffmpeg.zip' -DestinationPath 'C:\ffmpeg' -Force"
```
Then add `C:\ffmpeg\ffmpeg-master-latest-win64-gpl\bin` to the system PATH.

---

## STEP 2 — Install Tesseract OCR

Check if already installed:
```
"C:\Program Files\Tesseract-OCR\tesseract.exe" --version
```
If NOT installed, download and install silently:
```
powershell -Command "Invoke-WebRequest -Uri 'https://github.com/UB-Mannheim/tesseract/releases/download/v5.3.3.20231005/tesseract-ocr-w64-setup-5.3.3.20231005.exe' -OutFile 'C:\tesseract_setup.exe'"
```
```
C:\tesseract_setup.exe /S
```
Wait 30 seconds then verify:
```
"C:\Program Files\Tesseract-OCR\tesseract.exe" --version
```

---

## STEP 3 — Install ngrok

Check if already installed:
```
ngrok version
```
If NOT installed:
```
winget install ngrok.ngrok --accept-source-agreements --accept-package-agreements
```

---

## STEP 4 — Install Python packages

Run from this folder:
```
pip install -r requirements.txt
```
If any package fails install them one by one:
```
pip install opencv-python
pip install numpy
pip install pytesseract
pip install Pillow
pip install flask
pip install pyyaml
pip install yt-dlp
pip install ffmpeg-python
pip install requests
```

---

## STEP 5 — Fix config.yaml

Read the file `config.yaml` in this folder. Make sure these exact values are set:
- `tesseract_path` must be `"C:\\Program Files\\Tesseract-OCR\\tesseract.exe"`
- `shots_dir` must be `"shots"`
- `port` must be `5000`
- `host` must be `"0.0.0.0"`

Fix anything that is wrong. Save the file.

---

## STEP 6 — Create required folders

```
mkdir shots
mkdir logs
```
If they already exist that is fine, ignore the error.

---

## STEP 7 — Create ocr_client.py

Create the file `ocr_client.py` in this folder with this exact content:

```python
"""
ocr_client.py

Drop this file next to any trading bot to receive live OCR data from the server.

Usage:
    from ocr_client import OCRClient
    ocr = OCRClient("https://YOUR_NGROK_URL_HERE")

    obi   = ocr.obi()              # float 0.0-1.0  (>0.5 buy pressure, <0.5 sell pressure)
    delta = ocr.delta()            # "BULL" or "BEAR"
    asks  = ocr.ask_walls()        # [{"price": 3350.5, "size": 450}, ...]
    bids  = ocr.bid_walls()        # [{"price": 3340.0, "size": 380}, ...]
    alive = ocr.is_alive()         # True or False
"""

import time
import requests

OCR_SERVER  = "http://localhost:5000"
TIMEOUT_SEC = 3
STALE_SEC   = 30


class OCRClient:
    def __init__(self, server_url: str = OCR_SERVER):
        self.url       = server_url.rstrip("/")
        self._cache    = {}
        self._cache_ts = 0.0

    def get_all(self) -> dict:
        """Fetch all three streams in one HTTP call. Returns cached data on error."""
        try:
            r = requests.get(f"{self.url}/all", timeout=TIMEOUT_SEC)
            r.raise_for_status()
            self._cache    = r.json()
            self._cache_ts = time.time()
        except Exception as e:
            print(f"[OCR CLIENT] fetch error: {e} — using cached data")
        return self._cache

    # ── T&S ───────────────────────────────────────────────────────────────────

    def obi(self) -> float | None:
        """Order Book Imbalance from Time and Sales rolling window.
        Returns float 0.0-1.0. Returns None if data is stale (older than 30s)."""
        d   = self.get_all().get("ts", {})
        age = time.time() - d.get("timestamp", 0)
        if age > STALE_SEC:
            print(f"[OCR CLIENT] T&S data stale ({age:.0f}s old)")
            return None
        return d.get("obi")

    def ts_volumes(self) -> dict:
        """Raw buy and sell volumes from the rolling T&S window."""
        d = self.get_all().get("ts", {})
        return {
            "buy_volume":  d.get("buy_volume",  0),
            "sell_volume": d.get("sell_volume", 0),
            "print_count": d.get("print_count", 0),
        }

    # ── Delta ─────────────────────────────────────────────────────────────────

    def delta(self) -> str | None:
        """Footprint delta signal. Returns 'BULL' or 'BEAR'. None if stale."""
        d   = self.get_all().get("delta", {})
        age = time.time() - d.get("timestamp", 0)
        if age > STALE_SEC:
            return None
        return d.get("delta")

    def delta_confidence(self) -> float:
        """Confidence of the delta signal from 0.0 to 1.0."""
        return self.get_all().get("delta", {}).get("confidence", 0.0)

    def delta_ratios(self) -> dict:
        """Raw red and green pixel ratios from the footprint panel."""
        d = self.get_all().get("delta", {})
        return {
            "red_ratio":   d.get("red_ratio",   0.0),
            "green_ratio": d.get("green_ratio", 0.0),
        }

    # ── COB ───────────────────────────────────────────────────────────────────

    def ask_walls(self) -> list:
        """All detected ask walls above current price.
        Each entry is a dict: {'price': float, 'size': int}"""
        return self.get_all().get("cob", {}).get("ask_walls", [])

    def bid_walls(self) -> list:
        """All detected bid walls below current price.
        Each entry is a dict: {'price': float, 'size': int}"""
        return self.get_all().get("cob", {}).get("bid_walls", [])

    def nearest_ask_wall(self) -> dict | None:
        """The single closest ask wall above current price. None if no walls."""
        walls = self.ask_walls()
        return min(walls, key=lambda w: w["price"]) if walls else None

    def nearest_bid_wall(self) -> dict | None:
        """The single closest bid wall below current price. None if no walls."""
        walls = self.bid_walls()
        return max(walls, key=lambda w: w["price"]) if walls else None

    # ── Health ────────────────────────────────────────────────────────────────

    def is_alive(self) -> bool:
        """True only when all four OCR workers are running and sending fresh data."""
        h = self.get_all().get("health", {})
        return all([
            h.get("stream_alive"),
            h.get("ts_alive"),
            h.get("delta_alive"),
            h.get("cob_alive"),
        ])

    def status(self) -> dict:
        """One-line status snapshot. Useful for printing in bot logs."""
        d = self.get_all()
        return {
            "obi":          d.get("ts",    {}).get("obi"),
            "delta":        d.get("delta", {}).get("delta"),
            "ask_walls":    len(d.get("cob", {}).get("ask_walls", [])),
            "bid_walls":    len(d.get("cob", {}).get("bid_walls", [])),
            "server_alive": self.is_alive(),
        }
```

---

## STEP 8 — Create START_OCR_SERVER.bat

Create the file `START_OCR_SERVER.bat` in this folder with this exact content:

```bat
@echo off
title OCR Order Book Server
color 0A

echo ============================================================
echo   OCR ORDER BOOK SERVER
echo ============================================================
echo.

cd /d "%~dp0"

python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install Python 3.10+ first.
    pause
    exit /b 1
)

ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo ERROR: ffmpeg not found. Run: winget install ffmpeg
    pause
    exit /b 1
)

"C:\Program Files\Tesseract-OCR\tesseract.exe" --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Tesseract not found. Install from:
    echo https://github.com/UB-Mannheim/tesseract/wiki
    pause
    exit /b 1
)

if not exist "shots" mkdir shots
if not exist "logs"  mkdir logs

echo [OK] Python found
echo [OK] ffmpeg found
echo [OK] Tesseract found
echo.
echo Server starting at:  http://localhost:5000
echo Dashboard:           http://localhost:5000/
echo.
echo ============================================================
echo  IMPORTANT: Open a second terminal and run:
echo      ngrok http 5000
echo  Then copy the https://xxxx.ngrok-free.app URL into your bots
echo ============================================================
echo.
echo Press Ctrl+C to stop.
echo.

python main.py

echo.
echo Server stopped.
pause
```

---

## STEP 9 — Run syntax check

Run this Python command and fix any file that shows ERR before continuing:

```
python -c "import ast; files=['main.py','capture/frame_buffer.py','capture/stream_capture.py','ocr/ts_parser.py','ocr/delta_parser.py','ocr/cob_reader.py','server/data_server.py','server/heartbeat.py','ocr_client.py']; ok=True; [print('OK  '+f) or None for f in files if not (lambda f: ast.parse(open(f).read()))(f)] ; [setattr(__builtins__,'_e',e) or print(f'ERR {f}: {e}') for f in files for e in [None] if False]; print('DONE')"
```

Simpler version if above fails:
```
python -c "
import ast
files = [
    'main.py',
    'capture/frame_buffer.py',
    'capture/stream_capture.py',
    'ocr/ts_parser.py',
    'ocr/delta_parser.py',
    'ocr/cob_reader.py',
    'server/data_server.py',
    'server/heartbeat.py',
    'ocr_client.py'
]
for f in files:
    try:
        ast.parse(open(f).read())
        print('OK  ' + f)
    except SyntaxError as e:
        print('ERR ' + f + ' : ' + str(e))
"
```

All files must print OK. Fix any that show ERR.

---

## STEP 10 — Print final instructions for the user

When every step above is complete, print this message exactly:

```
============================================================
  SETUP COMPLETE — HERE IS WHAT TO DO NEXT
============================================================

1. Double-click START_OCR_SERVER.bat to start the OCR server

2. Open a second terminal window and run:
       ngrok http 5000
   Ngrok will show a URL like:
       https://abc123.ngrok-free.app

3. Open your YouTube stream in the browser and leave it open

4. In every trading bot, add these two lines at the top:
       from ocr_client import OCRClient
       ocr = OCRClient("https://abc123.ngrok-free.app")

5. Then use these in your bot logic:
       ocr.obi()              → T&S pressure  (float 0-1)
       ocr.delta()            → "BULL" or "BEAR"
       ocr.ask_walls()        → ask walls list with price and size
       ocr.bid_walls()        → bid walls list with price and size
       ocr.nearest_ask_wall() → closest single ask wall
       ocr.nearest_bid_wall() → closest single bid wall
       ocr.is_alive()         → True or False (is server running)

6. Check the dashboard anytime at:
       http://localhost:5000/

7. Logs are in the logs/ folder:
       logs/ocr_server.log  — everything
       logs/error.log       — errors only

8. After first run check these debug images in shots/:
       debug_ts_crop.png              (T&S panel crop)
       debug_ts_classified.png        (rows classified as buy/sell)
       debug_footprint_delta_row.png  (delta strip)
       debug_cob_crop.png             (COB panel crop)
   If any crop looks wrong, adjust the crop fractions in config.yaml

============================================================
```

---

## Rules

- Use the Bash tool to run every command
- Never ask the user anything — decide everything yourself
- If an install fails try an alternative method automatically
- Do not stop until all 10 steps are complete and verified
- The only manual step left for the user is running ngrok and pasting the URL
