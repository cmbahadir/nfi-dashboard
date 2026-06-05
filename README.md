# nfi-dashboard

A lightweight web dashboard for monitoring a live [Freqtrade](https://github.com/freqtrade/freqtrade) bot running the **NostalgiaForInfinityX6** strategy on Binance SPOT.

Reads directly from the Freqtrade SQLite database and log file — no extra dependencies on the bot itself.

![Dashboard preview](https://img.shields.io/badge/stack-Flask%20%2B%20Waitress%20%2B%20Chart.js-blue)

## Features

- **Summary cards** — total trades, realized profit, unrealized P&L, win rate, average profit, open positions
- **Cumulative profit chart** — interactive time-series (Chart.js), green/red coloured
- **Trades table** — every trade with pair, status, duration, entry/exit rates, stake, P&L, exit reason and entry tag
- **Bot error log** — parsed from the Freqtrade log file; filterable by severity (ERROR / WARNING) and category (WebSocket, Exchange API, Telegram, Strategy); click any row to expand the traceback
- **Auto-refresh** every 30 seconds
- Reachable over LAN and Tailscale out of the box (binds to `0.0.0.0`)

## Setup

### 1. Install dependencies

```bash
pip install flask waitress
```

### 2. Configure paths

Either edit the constants at the top of `dashboard.py`, or export environment variables:

```bash
export FREQTRADE_DB=/home/you/freqtrade/tradesv3.sqlite
export FREQTRADE_LOG=/home/you/freqtrade/user_data/logs/freqtrade_nfi.log
export DASHBOARD_PORT=8888   # optional, default 8888
```

### 3. Run

```bash
python3 dashboard.py
```

Open `http://localhost:8888` in your browser.

## Run as a persistent service (systemd user unit)

```bash
# Copy and edit the service file
cp freqtrade-dashboard.service ~/.config/systemd/user/
# Edit User, WorkingDirectory, ExecStart, and Environment paths
nano ~/.config/systemd/user/freqtrade-dashboard.service

systemctl --user daemon-reload
systemctl --user enable --now freqtrade-dashboard

# Start at boot even without a login session
loginctl enable-linger $USER
```

### Useful commands

```bash
systemctl --user status freqtrade-dashboard
systemctl --user restart freqtrade-dashboard
journalctl --user -u freqtrade-dashboard -f
```

## LAN / Tailscale access via nginx (recommended)

If port 8888 is blocked by a firewall or router, put nginx in front on port 8080:

```bash
sudo cp nginx-dashboard.conf /etc/nginx/conf.d/dashboard.conf
sudo systemctl reload nginx
```

## Access

| Network   | Direct (port 8888)             | Via nginx (port 8080)          |
|-----------|--------------------------------|--------------------------------|
| Local     | `http://localhost:8888`        | `http://localhost:8080`        |
| LAN       | `http://<your-lan-ip>:8888`    | `http://<your-lan-ip>:8080`    |
| Tailscale | `http://<tailscale-ip>:8888`   | `http://<tailscale-ip>:8080`   |
