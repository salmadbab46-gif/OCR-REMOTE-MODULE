# OCR Order Book Server

Reads a Rithmic platform YouTube live stream and distributes three real-time
data streams over HTTP to any number of trading bot clients.

## Architecture

```
YouTube Stream
     │
     ▼
stream_capture (Process 1)
  yt-dlp → ffmpeg → shots/shot_YYYYMMDD_HHMMSS.png
  Updates shared["frame_path"]
     │
     ├──▶ ts_parser    (Process 2)  →  /ts   endpoint
     ├──▶ delta_parser (Process 3)  →  /delta endpoint
     ├──▶ cob_reader   (Process 4)  →  /cob   endpoint
     │
     └──▶ data_server  (Process 5)  →  HTTP :5000
              ├── GET /ts
              ├── GET /delta
              ├── GET /cob
              ├── GET /all       ← use this from bots
              ├── GET /health
              └── GET /          ← web dashboard
```

## Prerequisites

1. **Python 3.10+**

2. **ffmpeg** in PATH:
   ```
   winget install ffmpeg
   ```

3. **Tesseract OCR**:
   Download from https://github.com/UB-Mannheim/tesseract/wiki
   Install to default path: `C:\Program Files\Tesseract-OCR\`

4. **Python packages**:
   ```
   pip install -r requirements.txt
   ```

## Quick Start

```bash
cd ocr_orderbook_server
python main.py
```

Open http://localhost:5000/ to see the live dashboard.

### Options

```
python main.py --config path/to/config.yaml
python main.py --log-level DEBUG
```

## API Reference

All responses are JSON. `server_ts` is always included for staleness checks.

### GET /ts
```json
{
  "timestamp": 1714567890.123,
  "obi": 0.6241,
  "print_count": 50,
  "buy_volume": 312,
  "sell_volume": 188,
  "server_ts": 1714567890.456
}
```
OBI range: 0.0–1.0  
- > 0.5 → buy-dominant flow  
- < 0.5 → sell-dominant flow

### GET /delta
```json
{
  "timestamp": 1714567890.123,
  "delta": "BULL",
  "confidence": 0.78,
  "red_ratio": 0.21,
  "green_ratio": 0.78,
  "server_ts": 1714567890.456
}
```
`delta` is always `"BULL"` or `"BEAR"` — no mixed category.

### GET /cob
```json
{
  "timestamp": 1714567890.123,
  "ask_walls": [{"price": 3350.5, "size": 450}],
  "bid_walls":  [{"price": 3340.0, "size": 380}],
  "server_ts":  1714567890.456
}
```

### GET /all
Combined payload — recommended endpoint for bots.

### GET /health
Returns 200 when all workers alive, 503 when any worker is dead.
```json
{
  "stream_alive": true,
  "ts_alive":     true,
  "delta_alive":  true,
  "cob_alive":    true,
  "server_ts":    1714567890.456
}
```

## Consuming from a Bot

```python
import requests

resp = requests.get("http://YOUR_SERVER_IP:5000/all", timeout=3)
data = resp.json()

obi   = data["ts"]["obi"]          # float 0-1
delta = data["delta"]["delta"]     # "BULL" or "BEAR"
asks  = data["cob"]["ask_walls"]   # list of {price, size}
bids  = data["cob"]["bid_walls"]

# Staleness check
import time
age = time.time() - data["ts"]["timestamp"]
if age > 30:
    print("WARNING: T&S data is stale")
```

## Tuning OCR Regions

After first run, inspect the debug images in `shots/`:

| File | What to check |
|------|---------------|
| `debug_ts_crop.png` | Should show only T&S rows, no adjacent panels |
| `debug_ts_classified.png` | Green/red boxes drawn on classified rows |
| `debug_footprint_crop.png` | Should show the Footprints panel |
| `debug_footprint_delta_row.png` | The M15 delta row strip |
| `debug_cob_crop.png` | Should show the COB panel |

Adjust `crop_left` / `crop_right` fractions in `config.yaml` until the crops
are accurate. All fractions are relative to the full frame width (3840px).

## Logs

- `logs/ocr_server.log` — all events, rotated at 10 MB
- `logs/error.log`      — errors only

## Troubleshooting

**"ffmpeg not found"**: Run `winget install ffmpeg` and restart your terminal.

**"tesseract not found"**: Verify `tesseract_path` in config.yaml points to
your Tesseract installation.

**T&S OBI always 0.5**: Check `debug_ts_crop.png` — the crop may be off.
Adjust `ts.crop_left` / `ts.crop_right` in config.yaml.

**Delta always BEAR**: Check `debug_footprint_delta_row.png`. The strip must
show the colored delta cells, not the label column. Increase `label_skip_px`.

**COB returns no walls**: The OCR needs clean digits. Check `debug_cob_crop.png`
and adjust `cob.crop_left`. Increase `ocr_scale` (try 3) for better accuracy.
