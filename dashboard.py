#!/usr/bin/env python3
"""Freqtrade performance dashboard — NFI Binance."""

import re
import os
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from flask import Flask, jsonify, render_template_string
from waitress import serve

# ── Configuration (override with environment variables) ───────────────────────
DB_PATH   = os.getenv("FREQTRADE_DB",  "/home/cmb/freqtrade/tradesv3.sqlite")
LOG_PATH  = os.getenv("FREQTRADE_LOG", "/home/cmb/freqtrade/user_data/logs/freqtrade_nfi.log")
PORT      = int(os.getenv("DASHBOARD_PORT", "8888"))
HOST      = os.getenv("DASHBOARD_HOST", "0.0.0.0")

LOG_TAIL_LINES = 8000
MAX_ERRORS     = 200

app = Flask(__name__)

# ── Log parser ────────────────────────────────────────────────────────────────
_LOG_RE = re.compile(
    r'^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+)'
    r' - ([\w\.]+)'
    r' - (ERROR|WARNING|CRITICAL|INFO|DEBUG)'
    r' - (.+)$'
)

_CATEGORY_MAP = {
    "exchange_ws":  ("WebSocket",    "ws"),
    "exchange":     ("Exchange API", "api"),
    "telegram":     ("Telegram",     "tg"),
    "NostalgiaFor": ("Strategy",     "strat"),
    "worker":       ("Worker",       "worker"),
    "rpc":          ("RPC",          "rpc"),
}


def _categorize(module: str, message: str) -> tuple[str, str]:
    for key, (label, slug) in _CATEGORY_MAP.items():
        if key in module or key in message:
            return label, slug
    low = message.lower()
    if any(k in low for k in ("networkerror", "connection", "network")):
        return "Network", "net"
    if any(k in low for k in ("binance", "api", "exchange")):
        return "Exchange API", "api"
    return "Other", "other"


def _promote_level(message: str) -> str | None:
    low = message.lower()
    if any(k in low for k in ("networkerror", "exchangenotavailable", "500", "connection closed")):
        return "ERROR"
    return None


def _tail_lines(path: str, n: int) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        buf = bytearray()
        pos = f.tell()
        lines_found = 0
        while pos > 0 and lines_found <= n:
            read_size = min(8192, pos)
            pos -= read_size
            f.seek(pos)
            buf = f.read(read_size) + buf
            lines_found = buf.count(b'\n')
    return buf.decode("utf-8", errors="replace").splitlines()[-n:]


def _fetch_trade_periods() -> list[tuple]:
    """Returns (open_dt, close_dt_or_None, pair, trade_id) for every trade."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT id, pair, open_date, close_date FROM trades ORDER BY open_date")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    periods = []
    for r in rows:
        def _dt(s):
            return datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S") if s else None
        periods.append((_dt(r["open_date"]), _dt(r["close_date"]), r["pair"], r["id"]))
    return periods


def _assess_impact(event: dict, trade_periods: list) -> dict:
    """Classify whether an error actually affected a trading decision."""
    msg      = event["message"]
    msg_low  = msg.lower()
    cat      = event["cat_slug"]

    try:
        ts = datetime.strptime(event["ts"], "%Y-%m-%d %H:%M:%S,%f")
    except ValueError:
        return {"level": "none", "label": "No Impact", "reason": ""}

    def _active_trade(ts):
        now = datetime.utcnow()
        for open_dt, close_dt, pair, tid in trade_periods:
            if open_dt <= ts <= (close_dt or now):
                return pair, tid
        return None, None

    # ── Definitive blocks: bot explicitly logged it could not act ─────────────
    if "unable to exit trade" in msg_low:
        pair_m = re.search(r"trade (\S+?):", msg)
        pair = pair_m.group(1) if pair_m else "unknown"
        return {
            "level": "blocked",
            "label": "Exit Blocked",
            "reason": (
                f"Bot tried to exit {pair} but Binance API was unreachable after all retries. "
                f"The exit was skipped for that candle cycle — position held longer than intended."
            ),
        }

    if "unable to adjust" in msg_low:
        pair_m = re.search(r"trade for (\S+?):", msg)
        pair = pair_m.group(1) if pair_m else "unknown"
        return {
            "level": "blocked",
            "label": "DCA Blocked",
            "reason": (
                f"Bot could not adjust (DCA / grind) the position for {pair}. "
                f"The cost-averaging buy was skipped — average entry price was not improved."
            ),
        }

    if "cancelling entry" in msg_low and "slippage" in msg_low:
        pair_m = re.search(r"for (\S+) due", msg)
        pct_m  = re.search(r"slippage ([\d.]+)%", msg_low)
        pair = pair_m.group(1) if pair_m else "unknown"
        pct  = pct_m.group(1) if pct_m else "?"
        return {
            "level": "blocked",
            "label": "Entry Blocked",
            "reason": (
                f"Entry for {pair} was cancelled because price moved {pct}% beyond the allowed slippage "
                f"threshold before the order could fill. Bot retried on the next cycle."
            ),
        }

    if "giving up" in msg_low:
        pair, tid = _active_trade(ts)
        if pair:
            return {
                "level": "blocked",
                "label": "API Call Abandoned",
                "reason": (
                    f"Bot exhausted all retries during open trade #{tid} ({pair}). "
                    f"That evaluation cycle was skipped entirely — signals for this candle were not processed."
                ),
            }
        return {"level": "none", "label": "No Impact", "reason": "No trades were active when retries were exhausted."}

    # ── Telegram — notification only, zero trading relevance ─────────────────
    if cat == "tg":
        return {
            "level": "none",
            "label": "No Impact",
            "reason": "Telegram handles notifications only. Trading logic ran independently and was unaffected.",
        }

    # ── WebSocket — freqtrade auto-reconnects via REST fallback ───────────────
    if cat == "ws":
        return {
            "level": "recovered",
            "label": "Auto-recovered",
            "reason": (
                "WebSocket disconnected from Binance but freqtrade automatically switches to "
                "REST API polling. No candle cycle was missed; latency increased slightly until reconnect."
            ),
        }

    # ── REST fallback — WebSocket dropped but bot switched to polling automatically
    if "falling back to rest" in msg_low:
        return {
            "level": "recovered",
            "label": "Auto-recovered",
            "reason": (
                "WebSocket stream was stale; freqtrade automatically fell back to REST API polling "
                "for that candle. No evaluation cycle was skipped."
            ),
        }

    # ── Exchange / API errors — impact depends on whether a trade was open ────
    pair, tid = _active_trade(ts)
    if pair:
        return {
            "level": "delayed",
            "label": "Signal Delayed",
            "reason": (
                f"Candle or ticker data was temporarily unavailable during open trade #{tid} ({pair}). "
                f"Strategy evaluation for that 5-minute candle may have been delayed by one cycle."
            ),
        }

    return {
        "level": "none",
        "label": "No Impact",
        "reason": "No trades were active at this time — the error occurred during an idle period.",
    }


def parse_errors(limit: int = MAX_ERRORS) -> dict:
    lines        = _tail_lines(LOG_PATH, LOG_TAIL_LINES)
    trade_periods = _fetch_trade_periods()
    events = []
    current = None

    for line in lines:
        m = _LOG_RE.match(line)
        if m:
            if current and current["level"] in ("ERROR", "WARNING", "CRITICAL"):
                events.append(current)
            ts, module, level, msg = m.groups()
            cat_label, cat_slug = _categorize(module, msg)
            current = {
                "ts": ts, "module": module,
                "level": _promote_level(msg) or level,
                "message": msg, "detail": [],
                "category": cat_label, "cat_slug": cat_slug,
            }
        elif current:
            stripped = line.strip()
            if stripped:
                current["detail"].append(stripped)

    if current and current["level"] in ("ERROR", "WARNING", "CRITICAL"):
        events.append(current)

    events = [e for e in events if e["level"] in ("ERROR", "WARNING", "CRITICAL")]
    events = events[-limit:]
    events.reverse()

    counts     = defaultdict(int)
    by_cat     = defaultdict(int)
    by_impact  = defaultdict(int)
    sig_counts: dict[str, int] = defaultdict(int)

    for e in events:
        counts[e["level"]] += 1
        by_cat[e["category"]] += 1
        sig = re.sub(r"[A-Z]+/USDT", "PAIR", e["message"])
        sig = re.sub(r"\b\d+[mhd]\b", "TF", sig)[:120]
        e["sig"] = sig
        sig_counts[sig] += 1
        e["impact"] = _assess_impact(e, trade_periods)
        by_impact[e["impact"]["level"]] += 1

    for e in events:
        e["count"] = sig_counts[e["sig"]]

    return {
        "total":      len(events),
        "errors":     counts.get("ERROR", 0),
        "warnings":   counts.get("WARNING", 0),
        "criticals":  counts.get("CRITICAL", 0),
        "by_category": dict(by_cat),
        "by_impact":   dict(by_impact),
        "events": [
            {
                "ts": e["ts"], "level": e["level"],
                "module": e["module"], "category": e["category"],
                "cat_slug": e["cat_slug"], "message": e["message"],
                "detail": "\n".join(e["detail"][-6:]),
                "count": e["count"],
                "impact": e["impact"],
            }
            for e in events
        ],
    }


# ── Database ──────────────────────────────────────────────────────────────────

def fetch_trades() -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT id, pair, base_currency, open_date, close_date,
               open_rate, close_rate, stake_amount, amount,
               close_profit, close_profit_abs, realized_profit,
               exit_reason, enter_tag, is_open, strategy,
               max_stake_amount, max_rate, min_rate, fee_open_cost, fee_close_cost
        FROM trades ORDER BY open_date ASC
    """)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ── API routes ────────────────────────────────────────────────────────────────

@app.route("/api/data")
def api_data():
    trades = fetch_trades()
    closed      = [t for t in trades if not t["is_open"]]
    open_trades = [t for t in trades if t["is_open"]]

    total_profit_abs = sum(t["close_profit_abs"] or 0 for t in closed)
    win_trades       = [t for t in closed if (t["close_profit_abs"] or 0) > 0]
    win_rate         = (len(win_trades) / len(closed) * 100) if closed else 0
    avg_profit_pct   = (
        sum(t["close_profit"] or 0 for t in closed) / len(closed) * 100
    ) if closed else 0

    cumulative, running = [], 0.0
    for t in sorted(closed, key=lambda x: x["close_date"]):
        running += t["close_profit_abs"] or 0
        cumulative.append({"date": t["close_date"], "profit": round(running, 4), "pair": t["pair"]})

    return jsonify({
        "summary": {
            "total_trades":    len(trades),
            "closed_trades":   len(closed),
            "open_trades":     len(open_trades),
            "total_profit_abs": round(total_profit_abs, 4),
            "unrealized_pnl":  round(sum(t["realized_profit"] or 0 for t in open_trades), 4),
            "win_rate":        round(win_rate, 1),
            "win_trades":      len(win_trades),
            "loss_trades":     len(closed) - len(win_trades),
            "avg_profit_pct":  round(avg_profit_pct, 3),
        },
        "trades":     trades,
        "cumulative": cumulative,
    })


@app.route("/api/errors")
def api_errors():
    return jsonify(parse_errors())


# ── HTML ──────────────────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>FreqTrade Dashboard — NFI Binance</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:#0d1117; --card:#161b22; --border:#30363d;
    --text:#e6edf3; --muted:#8b949e; --green:#3fb950;
    --red:#f85149; --yellow:#d29922; --blue:#388bfd;
    --orange:#e3943e; --purple:#a371f7; --accent:#1f6feb;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',monospace;font-size:14px}

  header{background:var(--card);border-bottom:1px solid var(--border);padding:16px 24px;display:flex;align-items:center;gap:12px}
  header h1{font-size:18px;font-weight:600}
  header .strategy{font-size:12px;color:var(--muted);background:var(--border);padding:3px 10px;border-radius:12px}
  .live-dot{width:8px;height:8px;border-radius:50%;background:var(--green);animation:pulse 2s infinite;margin-left:auto}
  .live-label{font-size:12px;color:var(--green)}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

  .container{max-width:1400px;margin:0 auto;padding:24px}

  .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:16px;margin-bottom:28px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:18px 20px}
  .card .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
  .card .value{font-size:26px;font-weight:700;line-height:1}
  .card .sub{font-size:11px;color:var(--muted);margin-top:6px}
  .green{color:var(--green)}.red{color:var(--red)}.blue{color:var(--blue)}
  .yellow{color:var(--yellow)}.orange{color:var(--orange)}.purple{color:var(--purple)}

  .chart-card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:20px;margin-bottom:28px}
  .chart-card h2{font-size:14px;font-weight:600;color:var(--muted);margin-bottom:16px}
  .chart-wrap{height:260px}

  .section-card{background:var(--card);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:28px}
  .section-hdr{padding:14px 20px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:8px}
  .section-hdr h2{font-size:14px;font-weight:600;color:var(--muted)}

  table{width:100%;border-collapse:collapse}
  thead{background:rgba(255,255,255,.03)}
  th{padding:10px 16px;text-align:left;font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.5px;border-bottom:1px solid var(--border)}
  td{padding:11px 16px;border-bottom:1px solid rgba(48,54,61,.5);font-size:13px}
  tr:last-child td{border-bottom:none}
  tr:hover td{background:rgba(255,255,255,.02)}

  .pair-cell{font-weight:600}
  .status-open{color:var(--yellow);font-size:11px;background:rgba(210,153,34,.15);padding:2px 8px;border-radius:10px}
  .status-closed{color:var(--green);font-size:11px;background:rgba(63,185,80,.1);padding:2px 8px;border-radius:10px}
  .profit-pos{color:var(--green);font-weight:600}
  .profit-neg{color:var(--red);font-weight:600}
  .tag{font-size:10px;background:rgba(56,139,253,.15);color:var(--blue);padding:2px 7px;border-radius:8px}
  .reason{font-size:11px;color:var(--muted);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

  .err-filters{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
  .filter-btn{font-size:12px;padding:4px 12px;border-radius:16px;border:1px solid var(--border);background:transparent;color:var(--muted);cursor:pointer;transition:all .15s}
  .filter-btn.f-all.active{color:var(--text);border-color:var(--border);background:var(--border)}
  .filter-btn.f-error.active{color:var(--red);border-color:var(--red);background:rgba(248,81,73,.1)}
  .filter-btn.f-warning.active{color:var(--yellow);border-color:var(--yellow);background:rgba(210,153,34,.1)}
  .filter-btn.f-ws.active,.filter-btn.f-api.active,.filter-btn.f-tg.active,.filter-btn.f-strat.active{color:var(--blue);border-color:var(--blue);background:rgba(56,139,253,.1)}
  .filter-btn.f-affected.active{color:var(--orange);border-color:var(--orange);background:rgba(227,148,62,.12);font-weight:600}

  .err-list{padding:0}
  .err-row{border-bottom:1px solid rgba(48,54,61,.4);padding:12px 20px;cursor:pointer;transition:background .1s}
  .err-row:last-child{border-bottom:none}
  .err-row:hover{background:rgba(255,255,255,.025)}
  .err-row.impact-blocked{border-left:3px solid var(--red)}
  .err-row.impact-delayed{border-left:3px solid var(--orange)}
  .err-row-top{display:flex;align-items:flex-start;gap:10px}
  .err-badge{font-size:10px;font-weight:700;padding:2px 7px;border-radius:6px;white-space:nowrap;flex-shrink:0;margin-top:1px}
  .badge-ERROR{background:rgba(248,81,73,.18);color:var(--red)}
  .badge-WARNING{background:rgba(210,153,34,.18);color:var(--yellow)}
  .badge-CRITICAL{background:rgba(163,113,247,.18);color:var(--purple)}
  .cat-pill{font-size:10px;padding:2px 7px;border-radius:6px;background:rgba(56,139,253,.12);color:var(--blue);flex-shrink:0;margin-top:1px}
  .impact-pill{font-size:10px;font-weight:700;padding:2px 8px;border-radius:6px;white-space:nowrap;flex-shrink:0;margin-top:1px}
  .impact-blocked{background:rgba(248,81,73,.18);color:var(--red)}
  .impact-delayed{background:rgba(227,148,62,.18);color:var(--orange)}
  .impact-recovered{background:rgba(56,139,253,.12);color:var(--blue)}
  .impact-none{background:rgba(139,148,158,.1);color:var(--muted)}
  .impact-reason{font-size:11px;color:var(--muted);margin-top:5px;line-height:1.5;padding:6px 10px;background:rgba(0,0,0,.2);border-radius:5px;border-left:2px solid var(--border)}
  .impact-reason.r-blocked{border-left-color:var(--red)}
  .impact-reason.r-delayed{border-left-color:var(--orange)}
  .impact-reason.r-recovered{border-left-color:var(--blue)}
  .err-msg{font-size:12.5px;color:var(--text);flex:1;word-break:break-word;line-height:1.5}
  .err-meta{display:flex;gap:12px;margin-top:4px;font-size:11px;color:var(--muted)}
  .err-count{font-size:10px;background:rgba(139,148,158,.15);color:var(--muted);padding:1px 7px;border-radius:10px;white-space:nowrap;flex-shrink:0;align-self:flex-start;margin-top:2px}
  .err-detail{display:none;margin-top:8px;background:rgba(0,0,0,.35);border-radius:6px;padding:10px 14px;font-size:11px;color:var(--muted);white-space:pre-wrap;line-height:1.6;font-family:monospace;border-left:2px solid var(--border)}
  .err-row.expanded .err-detail{display:block}
  .err-chevron{font-size:10px;color:var(--muted);flex-shrink:0;margin-top:3px;transition:transform .2s}
  .err-row.expanded .err-chevron{transform:rotate(90deg)}

  .err-summary-pills{display:flex;gap:8px;flex-wrap:wrap}
  .err-sum-pill{font-size:12px;padding:3px 12px;border-radius:12px;font-weight:600}
  .pill-error{background:rgba(248,81,73,.15);color:var(--red)}
  .pill-warn{background:rgba(210,153,34,.15);color:var(--yellow)}
  .pill-crit{background:rgba(163,113,247,.15);color:var(--purple)}
  .pill-blocked{background:rgba(248,81,73,.2);color:var(--red);border:1px solid rgba(248,81,73,.4)}
  .pill-delayed{background:rgba(227,148,62,.15);color:var(--orange)}

  .refresh-btn{background:var(--accent);color:#fff;border:none;border-radius:8px;padding:7px 16px;cursor:pointer;font-size:13px}
  .refresh-btn:hover{background:#1a73e8}
  .last-update{font-size:11px;color:var(--muted)}
  .no-data{text-align:center;padding:40px;color:var(--muted)}
</style>
</head>
<body>

<header>
  <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
    <polyline points="2,18 8,12 13,16 22,6" stroke="#3fb950" stroke-width="2" fill="none" stroke-linecap="round"/>
  </svg>
  <h1>FreqTrade Dashboard</h1>
  <span class="strategy">NostalgiaForInfinityX6 · Binance SPOT</span>
  <div class="live-dot"></div>
  <span class="live-label">LIVE</span>
</header>

<div class="container">

  <div class="cards">
    <div class="card"><div class="label">Total Trades</div><div class="value blue" id="s-total">—</div><div class="sub" id="s-open-label">loading…</div></div>
    <div class="card"><div class="label">Realized Profit</div><div class="value" id="s-profit">—</div><div class="sub">USDT closed trades</div></div>
    <div class="card"><div class="label">Unrealized P&amp;L</div><div class="value" id="s-unrealized">—</div><div class="sub">open positions</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value" id="s-winrate">—</div><div class="sub" id="s-wins-label">wins / losses</div></div>
    <div class="card"><div class="label">Avg Profit</div><div class="value" id="s-avg">—</div><div class="sub">per closed trade</div></div>
    <div class="card"><div class="label">Open Positions</div><div class="value yellow" id="s-openpos">—</div><div class="sub">active trades</div></div>
  </div>

  <div class="chart-card">
    <h2>Cumulative Realized Profit (USDT)</h2>
    <div class="chart-wrap"><canvas id="profitChart"></canvas></div>
  </div>

  <div class="section-card">
    <div class="section-hdr">
      <h2>All Trades</h2>
      <div style="display:flex;align-items:center;gap:12px">
        <span class="last-update" id="last-update"></span>
        <button class="refresh-btn" onclick="loadAll()">&#8635; Refresh</button>
      </div>
    </div>
    <div id="table-wrap"><p class="no-data">Loading trades…</p></div>
  </div>

  <div class="section-card">
    <div class="section-hdr">
      <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
        <h2>Bot Error Log</h2>
        <div class="err-summary-pills" id="err-pills"></div>
      </div>
      <div class="err-filters">
        <button class="filter-btn f-all active" onclick="setFilter('all',this)">All</button>
        <button class="filter-btn f-affected" onclick="setFilter('affected',this)">⚠ Affected Trading</button>
        <button class="filter-btn f-error" onclick="setFilter('ERROR',this)">Errors</button>
        <button class="filter-btn f-warning" onclick="setFilter('WARNING',this)">Warnings</button>
        <button class="filter-btn f-ws" onclick="setFilter('ws',this)">WebSocket</button>
        <button class="filter-btn f-api" onclick="setFilter('api',this)">Exchange API</button>
        <button class="filter-btn f-tg" onclick="setFilter('tg',this)">Telegram</button>
        <button class="filter-btn f-strat" onclick="setFilter('strat',this)">Strategy</button>
      </div>
    </div>
    <div class="err-list" id="err-list"><p class="no-data">Loading errors…</p></div>
  </div>

</div>

<script>
let chartInstance = null, allErrors = [], activeFilter = 'all';

function fmt(n,d=2){return(n===null||n===undefined)?'—':parseFloat(n).toFixed(d)}
function dur(open,close){
  const a=new Date(open+'Z'),b=close?new Date(close+'Z'):new Date();
  const s=Math.floor((b-a)/1000),h=Math.floor(s/3600),m=Math.floor((s%3600)/60);
  return h>0?`${h}h ${m}m`:`${m}m`;
}
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

function renderTrades(data) {
  const s=data.summary, trades=data.trades, cum=data.cumulative;

  document.getElementById('s-total').textContent=s.total_trades;
  document.getElementById('s-open-label').textContent=`${s.closed_trades} closed · ${s.open_trades} open`;

  const pe=document.getElementById('s-profit');
  pe.textContent=(s.total_profit_abs>=0?'+':'')+fmt(s.total_profit_abs)+' USDT';
  pe.className='value '+(s.total_profit_abs>=0?'green':'red');

  const ue=document.getElementById('s-unrealized');
  ue.textContent=(s.unrealized_pnl>=0?'+':'')+fmt(s.unrealized_pnl)+' USDT';
  ue.className='value '+(s.unrealized_pnl>=0?'green':'red');

  const we=document.getElementById('s-winrate');
  we.textContent=s.win_rate+'%';
  we.className='value '+(s.win_rate>=50?'green':'red');
  document.getElementById('s-wins-label').textContent=`${s.win_trades}W / ${s.loss_trades}L`;

  const ae=document.getElementById('s-avg');
  ae.textContent=(s.avg_profit_pct>=0?'+':'')+fmt(s.avg_profit_pct,2)+'%';
  ae.className='value '+(s.avg_profit_pct>=0?'green':'red');

  document.getElementById('s-openpos').textContent=s.open_trades;

  if(chartInstance) chartInstance.destroy();
  const allL=['Start',...cum.map(c=>{
    const d=new Date(c.date+'Z');
    return d.toLocaleDateString('en-GB',{month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'});
  })];
  const allP=[0,...cum.map(c=>c.profit)];
  const lc=allP[allP.length-1]>=0?'#3fb950':'#f85149';
  chartInstance=new Chart(document.getElementById('profitChart'),{
    type:'line',
    data:{labels:allL,datasets:[{label:'Cumulative Profit (USDT)',data:allP,
      borderColor:lc,backgroundColor:lc+'18',borderWidth:2.5,tension:0.3,fill:true,
      pointBackgroundColor:allP.map(p=>p>=0?'#3fb950':'#f85149'),pointRadius:5,pointHoverRadius:7}]},
    options:{
      responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},
      plugins:{legend:{display:false},tooltip:{backgroundColor:'#161b22',borderColor:'#30363d',
        borderWidth:1,titleColor:'#8b949e',bodyColor:'#e6edf3',
        callbacks:{label:ctx=>' '+(ctx.parsed.y>=0?'+':'')+ctx.parsed.y.toFixed(4)+' USDT'}}},
      scales:{
        x:{grid:{color:'#21262d'},ticks:{color:'#8b949e',maxRotation:30,font:{size:11}}},
        y:{grid:{color:'#21262d'},ticks:{color:'#8b949e',callback:v=>(v>=0?'+':'')+v.toFixed(2)+' USDT'}}
      }
    }
  });

  const sorted=[...trades].sort((a,b)=>new Date(b.open_date)-new Date(a.open_date));
  let html=`<table><thead><tr>
    <th>#</th><th>Pair</th><th>Status</th><th>Open Date</th>
    <th>Duration</th><th>Entry</th><th>Exit</th>
    <th>Stake (USDT)</th><th>Profit %</th><th>Profit USDT</th>
    <th>Exit Reason</th><th>Tag</th>
  </tr></thead><tbody>`;
  for(const t of sorted){
    const isOpen=t.is_open, pp=(t.close_profit||0)*100;
    const pa=isOpen?(t.realized_profit||0):(t.close_profit_abs||0);
    const pc=pa>=0?'profit-pos':'profit-neg', ps=pa>=0?'+':'';
    html+=`<tr>
      <td style="color:var(--muted)">${t.id}</td>
      <td class="pair-cell">${esc(t.pair)}</td>
      <td><span class="${isOpen?'status-open':'status-closed'}">${isOpen?'OPEN':'CLOSED'}</span></td>
      <td style="color:var(--muted);font-size:12px">${t.open_date.split('.')[0]}</td>
      <td style="color:var(--muted)">${dur(t.open_date,t.close_date)}</td>
      <td>${fmt(t.open_rate,4)}</td>
      <td>${t.close_rate?fmt(t.close_rate,4):'<span style="color:var(--muted)">—</span>'}</td>
      <td>${fmt(t.stake_amount,2)}</td>
      <td class="${pc}">${ps}${fmt(pp,2)}%${isOpen?' <span style="font-size:10px;color:var(--muted)">(unrlzd)</span>':''}</td>
      <td class="${pc}">${ps}${fmt(pa,4)}</td>
      <td><span class="reason" title="${esc(t.exit_reason||'')}">${esc(t.exit_reason||'—')}</span></td>
      <td>${t.enter_tag?'<span class="tag">'+esc(t.enter_tag.trim())+'</span>':'—'}</td>
    </tr>`;
  }
  html+='</tbody></table>';
  document.getElementById('table-wrap').innerHTML=sorted.length?html:'<p class="no-data">No trades yet.</p>';
  document.getElementById('last-update').textContent='Updated '+new Date().toLocaleTimeString();
}

const IMPACT_META = {
  blocked:   { label: 'Blocked',       cls: 'impact-blocked',   icon: '⛔' },
  delayed:   { label: 'Signal Delayed',cls: 'impact-delayed',   icon: '⚠' },
  recovered: { label: 'Auto-recovered',cls: 'impact-recovered', icon: '↺' },
  none:      { label: 'No Impact',     cls: 'impact-none',      icon: '—' },
};

function setFilter(f,btn){
  activeFilter=f;
  document.querySelectorAll('.filter-btn').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  renderErrorList();
}

function renderErrorList(){
  const list=document.getElementById('err-list');
  const filtered=allErrors.filter(e=>{
    if(activeFilter==='all') return true;
    if(activeFilter==='affected') return e.impact.level==='blocked'||e.impact.level==='delayed';
    if(activeFilter==='ERROR'||activeFilter==='WARNING') return e.level===activeFilter;
    return e.cat_slug===activeFilter;
  });
  if(!filtered.length){list.innerHTML='<p class="no-data">No events match this filter.</p>';return;}
  list.innerHTML=filtered.map((e,i)=>{
    const imp=e.impact, meta=IMPACT_META[imp.level]||IMPACT_META.none;
    const hasDetail=e.detail&&e.detail.trim().length>0;
    const clickable=hasDetail||imp.reason;
    const chevron=clickable?'<span class="err-chevron">▶</span>':'';
    const detail=hasDetail?`<div class="err-detail">${esc(e.detail)}</div>`:'';
    const reason=imp.reason?`<div class="impact-reason r-${imp.level}">${esc(imp.reason)}</div>`:'';
    const countBadge=e.count>1?`<span class="err-count">×${e.count}</span>`:'';
    const rowCls=`err-row${imp.level==='blocked'||imp.level==='delayed'?' impact-'+imp.level:''}`;
    return `<div class="${rowCls}" id="er${i}" ${clickable?`onclick="toggleRow('er${i}')"`:''}><div class="err-row-top">
      ${chevron}
      <span class="err-badge badge-${e.level}">${e.level}</span>
      <span class="cat-pill">${esc(e.category)}</span>
      <span class="impact-pill ${meta.cls}">${meta.icon} ${meta.label}</span>
      <div style="flex:1">
        <div class="err-msg">${esc(e.message)}</div>
        <div class="err-meta"><span>${e.ts}</span><span style="font-family:monospace">${esc(e.module)}</span></div>
        ${reason}${detail}
      </div>
      ${countBadge}
    </div></div>`;
  }).join('');
}

function toggleRow(id){document.getElementById(id).classList.toggle('expanded')}

function renderErrors(data){
  allErrors=data.events;
  const bi=data.by_impact||{};
  let pills='';
  if(data.criticals>0)    pills+=`<span class="err-sum-pill pill-crit">⚠ ${data.criticals} critical</span>`;
  if(bi.blocked>0)        pills+=`<span class="err-sum-pill pill-blocked">⛔ ${bi.blocked} blocked trading</span>`;
  if(bi.delayed>0)        pills+=`<span class="err-sum-pill pill-delayed">⚠ ${bi.delayed} signal delayed</span>`;
  if(data.errors>0)       pills+=`<span class="err-sum-pill pill-error">✕ ${data.errors} errors</span>`;
  if(data.warnings>0)     pills+=`<span class="err-sum-pill pill-warn">△ ${data.warnings} warnings</span>`;
  document.getElementById('err-pills').innerHTML=pills||'<span style="color:var(--muted);font-size:12px">No issues found</span>';
  renderErrorList();
}

function loadAll(){
  fetch('/api/data').then(r=>r.json()).then(renderTrades).catch(console.error);
  fetch('/api/errors').then(r=>r.json()).then(renderErrors).catch(console.error);
}

loadAll();
setInterval(loadAll,30000);
</script>
</body>
</html>"""


@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


if __name__ == "__main__":
    print(f"FreqTrade Dashboard")
    print(f"  Local:     http://localhost:{PORT}")
    print(f"  LAN:       http://192.168.1.201:{PORT}")
    print(f"  Tailscale: http://100.101.20.35:{PORT}")
    serve(app, host=HOST, port=PORT, threads=8)
