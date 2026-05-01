"""
data_server.py

Flask HTTP server that exposes OCR data to remote trading bots.

Endpoints
---------
GET /ts          Latest T&S OBI reading
GET /delta       Latest delta/footprint reading
GET /cob         Latest COB walls reading
GET /all         All three streams + heartbeat in one JSON
GET /health      Lightweight health check (used by watchdog / load-balancer)
GET /            Live web dashboard (auto-refreshes every 1 s)

All JSON responses include "server_ts" (unix time) so bots can detect
stale data even if the OCR workers slow down.

Runs in its own Process.  The shared Manager dict is passed in at startup.
"""

import json
import logging
import time

from flask import Flask, jsonify, render_template_string

logger = logging.getLogger("data_server")

# ── Dashboard HTML ─────────────────────────────────────────────────────────────
DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>OCR Order Book Server</title>
  <style>
    body { background:#111; color:#eee; font-family:monospace; padding:20px; }
    h1   { color:#0ff; margin-bottom:4px; }
    .sub { color:#888; font-size:12px; margin-bottom:20px; }
    .grid{ display:grid; grid-template-columns:1fr 1fr 1fr; gap:16px; }
    .card{ background:#1a1a1a; border:1px solid #333; border-radius:6px; padding:14px; }
    .card h2{ margin:0 0 8px; font-size:14px; color:#aaa; letter-spacing:1px; }
    .val { font-size:28px; font-weight:bold; margin:4px 0; }
    .sub2{ font-size:11px; color:#666; }
    .bull{ color:#0f0; }
    .bear{ color:#f44; }
    .neut{ color:#ff0; }
    .ok  { color:#0f0; }
    .dead{ color:#f44; }
    #health{ background:#1a1a1a; border:1px solid #333; border-radius:6px;
             padding:12px; margin-top:16px; font-size:12px; }
    #ts  { font-size:11px; color:#888; margin-top:20px; }
    .wall-list{ font-size:11px; color:#ccc; margin-top:6px; }
    .wall-ask { color:#f77; }
    .wall-bid { color:#7f7; }
  </style>
</head>
<body>
  <h1>OCR Order Book Server</h1>
  <div class="sub">Live data from YouTube stream — updates every 1s</div>

  <div class="grid">
    <div class="card" id="card-ts">
      <h2>T&amp;S — OBI</h2>
      <div class="val" id="obi-val">—</div>
      <div class="sub2" id="obi-sub">waiting for data...</div>
    </div>
    <div class="card" id="card-delta">
      <h2>FOOTPRINT — DELTA</h2>
      <div class="val" id="delta-val">—</div>
      <div class="sub2" id="delta-sub">waiting for data...</div>
    </div>
    <div class="card" id="card-cob">
      <h2>COB — WALLS</h2>
      <div class="val" id="cob-asks">ASK: —</div>
      <div class="val" id="cob-bids">BID: —</div>
      <div class="wall-list" id="cob-detail"></div>
    </div>
  </div>

  <div id="health">
    <span>Workers: </span>
    <span id="h-stream">stream=?</span> &nbsp;
    <span id="h-ts">ts=?</span> &nbsp;
    <span id="h-delta">delta=?</span> &nbsp;
    <span id="h-cob">cob=?</span>
  </div>

  <div id="ts">Last fetch: —</div>

  <script>
    function cls(ok){ return ok ? 'ok' : 'dead'; }
    function bias(obi){
      if(obi>0.5) return 'bull';
      if(obi<0.5) return 'bear';
      return 'neut';
    }

    async function refresh(){
      try{
        const r = await fetch('/all');
        const d = await r.json();

        // T&S
        const ts = d.ts;
        const obi = ts.obi;
        const obiEl = document.getElementById('obi-val');
        obiEl.textContent = obi.toFixed(4);
        obiEl.className = 'val ' + bias(obi);
        document.getElementById('obi-sub').textContent =
          `window=${ts.print_count}  buy=${ts.buy_volume}  sell=${ts.sell_volume}`;

        // Delta
        const dt = d.delta;
        const dEl = document.getElementById('delta-val');
        dEl.textContent = dt.delta;
        dEl.className = 'val ' + (dt.delta==='BULL'?'bull':'bear');
        document.getElementById('delta-sub').textContent =
          `conf=${(dt.confidence*100).toFixed(0)}%  red=${dt.red_ratio.toFixed(3)}  green=${dt.green_ratio.toFixed(3)}`;

        // COB
        const cob = d.cob;
        document.getElementById('cob-asks').textContent =
          `ASK walls: ${cob.ask_walls.length}`;
        document.getElementById('cob-bids').textContent =
          `BID walls: ${cob.bid_walls.length}`;
        let detail = '';
        cob.ask_walls.slice(0,3).forEach(w=>{
          detail += `<span class="wall-ask">ASK ${w.price.toFixed(1)} × ${w.size}</span><br>`;
        });
        cob.bid_walls.slice(0,3).forEach(w=>{
          detail += `<span class="wall-bid">BID ${w.price.toFixed(1)} × ${w.size}</span><br>`;
        });
        document.getElementById('cob-detail').innerHTML = detail;

        // Health
        const h = d.health;
        ['stream','ts','delta','cob'].forEach(ch=>{
          const el = document.getElementById('h-'+ch);
          const alive = h[ch+'_alive'];
          el.textContent = ch + '=' + (alive?'OK':'DEAD');
          el.className = cls(alive);
        });

        document.getElementById('ts').textContent =
          'Last fetch: ' + new Date().toLocaleTimeString();
      }catch(e){
        document.getElementById('ts').textContent = 'Error: ' + e.message;
      }
    }

    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>"""


def make_app(shared, cfg: dict) -> Flask:
    app = Flask(__name__)
    threshold = cfg["server"]["stale_threshold"]

    def get_health():
        from capture.frame_buffer import is_alive
        return {
            "server_ts":    time.time(),
            "stream_alive": is_alive(shared, "stream", threshold),
            "ts_alive":     is_alive(shared, "ts",     threshold),
            "delta_alive":  is_alive(shared, "delta",  threshold),
            "cob_alive":    is_alive(shared, "cob",    threshold),
        }

    @app.route("/ts")
    def route_ts():
        from capture.frame_buffer import get_result
        data = get_result(shared, "ts")
        data["server_ts"] = time.time()
        return jsonify(data)

    @app.route("/delta")
    def route_delta():
        from capture.frame_buffer import get_result
        data = get_result(shared, "delta")
        data["server_ts"] = time.time()
        return jsonify(data)

    @app.route("/cob")
    def route_cob():
        from capture.frame_buffer import get_result
        data = get_result(shared, "cob")
        data["server_ts"] = time.time()
        return jsonify(data)

    @app.route("/all")
    def route_all():
        from capture.frame_buffer import get_result
        return jsonify({
            "ts":        get_result(shared, "ts"),
            "delta":     get_result(shared, "delta"),
            "cob":       get_result(shared, "cob"),
            "health":    get_health(),
            "server_ts": time.time(),
        })

    @app.route("/health")
    def route_health():
        h = get_health()
        all_ok = all([
            h["stream_alive"], h["ts_alive"],
            h["delta_alive"],  h["cob_alive"],
        ])
        return jsonify(h), 200 if all_ok else 503

    @app.route("/")
    def route_dashboard():
        return render_template_string(DASHBOARD_HTML)

    return app


def server_process_main(cfg: dict, shared):
    """Entry point for the HTTP server Process."""
    import signal
    signal.signal(signal.SIGTERM, lambda *_: None)

    logging.getLogger("data_server").setLevel(
        getattr(logging, cfg["logging"]["level"], logging.INFO)
    )

    from server.heartbeat import Heartbeat
    hb = Heartbeat(shared, cfg)
    hb.start()

    host = cfg["server"]["host"]
    port = cfg["server"]["port"]
    logger.info("HTTP server starting on %s:%d", host, port)

    app = make_app(shared, cfg)
    # Disable Flask's default request logging to reduce noise
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    app.run(host=host, port=port, debug=False, use_reloader=False, threaded=True)
