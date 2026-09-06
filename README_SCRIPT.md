# DashScope Proxy TUI Launcher

## Quick Start

Run the proxy server in TUI mode with a single command:

```powershell
.\run_tui.ps1
```

This all-in-one script will:
1. Create a Python virtual environment (`venv/`) if it doesn't exist
2. Install all required dependencies from `requirements.txt`
3. Check for `.env` configuration file
4. Verify port availability
5. Launch the proxy server with the TUI dashboard

## Prerequisites

- **Python 3.9+** installed and in PATH
- **DASHSCOPE_API_KEY** configured in `.env` file (copy from `.env.example`)

## Command Line Options

```powershell
# Show help
.\run_tui.ps1 -Help

# Force reinstall all dependencies
.\run_tui.ps1 -ForceReinstall

# Run in headless mode (no TUI)
.\run_tui.ps1 -Headless
```

## TUI Controls

Once the TUI is running:

- **`q`** - Quit the application
- **`r`** - Clear logs
- **`1`** - Switch to Overview tab
- **`2`** - Switch to Logs tab
- **`3`** - Switch to Metrics tab
- **`4`** - Switch to Models tab
- **`5`** - Switch to Config tab
- **`Ctrl+C`** - Force quit

## Process Management (2024-2026 Best Practices)

The script implements current best practices for Windows process management:

### Windows Job Objects

The script creates a **Windows Job Object** with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`:

- **Guaranteed cleanup**: All child processes are terminated when the job handle closes
- **Works on crash**: Even if PowerShell crashes or is force-killed, Windows cleans up
- **Process tree management**: All descendants are included, regardless of parent-child relationships

### Three-Stage Shutdown

1. **Cooperative shutdown**: Send `CTRL_BREAK_EVENT` to request graceful exit
2. **Timeout wait**: Give process up to 8 seconds to shut down cleanly
3. **Force terminate**: If timeout expires, force terminate via Job Object

### Why Job Objects?

| Mechanism | Works on Crash? | Handles Grandchildren? | Works on Force Kill? |
|-----------|-----------------|------------------------|---------------------|
| Process handle | ❌ No | ❌ No | ❌ No |
| Process group | ❌ No | ⚠️ Maybe | ❌ No |
| **Job Object** | ✅ Yes | ✅ Yes | ✅ Yes |

### On Normal Exit
1. Press **`q`** in the TUI to quit gracefully
2. The TUI signals the proxy server to shut down
3. Server drains pending requests, closes connections
4. PID file is removed

### On Ctrl+C
1. Script intercepts Ctrl+C signal
2. Sends `CTRL_BREAK_EVENT` for cooperative shutdown
3. Waits up to 8 seconds for graceful exit
4. Force-terminates via Job Object if needed
5. Cleans up PID file

### On Crash or Force Kill
1. Windows automatically closes the Job Object handle
2. All processes in the job are terminated immediately
3. No orphan processes remain

## Configuration

The proxy reads configuration from `.env` file. Copy `.env.example` to `.env` and configure:

```bash
# Required
DASHSCOPE_API_KEY=your_api_key_here

# Optional - Proxy settings
DASHSCOPE_PROXY_HOST=127.0.0.1
DASHSCOPE_PROXY_PORT=8899

# Optional - Rate limiting overrides
# PROXY_RPM_LIMIT=14
# PROXY_TPM_LIMIT=4000000

# Optional - Secondary providers (MIMO, OpenLux, ARK)
# See .env.example for full configuration
```

## Troubleshooting

### Port Already in Use

If you see an error that port 8899 is already in use:

1. The script will detect and try to stop stale processes automatically
2. If manual cleanup needed:
   ```powershell
   Get-NetTCPConnection -LocalPort 8899 | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }
   Remove-Item proxy.pid -Force
   ```

### Missing Dependencies

If dependencies fail to install:

```powershell
# Force reinstall
.\run_tui.ps1 -ForceReinstall
```

### Missing .env File

The script will error if `.env` is not found. Create it:

```powershell
Copy-Item .env.example .env
# Edit .env and add your DASHSCOPE_API_KEY
```

### Process Won't Stop

If the proxy process won't stop gracefully:

```powershell
# Find and kill the process
Get-Process -Name python | Where-Object { $_.Path -like "*venv*" } | Stop-Process -Force
Remove-Item proxy.pid -Force
```

## Manual Execution

If you prefer to run manually without the script:

```powershell
# Create venv
python -m venv venv

# Activate
.\venv\Scripts\Activate.ps1

# Install dependencies
pip install -r requirements.txt

# Run server
python -m dashscope_proxy_lib.server
```

## What Gets Installed

The script installs these packages (from `requirements.txt`):

- `aiohttp>=3.9.0` - Async HTTP client/server
- `textual>=2.0.0` - TUI framework
- `pytest>=7.0.0` - Testing framework
- `pytest-asyncio>=0.23.0` - Async test support
- `python-dotenv>=1.0.0` - .env file loader
- `psutil>=5.9.0` - System utilities

## Features

The TUI dashboard provides:

- **Overview Tab**: Real-time metrics, rate limits, quotas, circuit breaker status
- **Logs Tab**: Filterable log stream with export functionality
- **Metrics Tab**: Sparkline charts, latency histograms, percentiles
- **Models Tab**: Per-model usage statistics and analytics
- **Config Tab**: Live configuration viewer with filtering

## Multi-Provider Support

The proxy supports multiple upstream providers:

- **Primary**: DashScope (default)
- **Secondary**: MIMO Coding Plan (optional)
- **Tertiary**: OpenLux (optional)
- **Quaternary**: ARK/BytePlus (optional)
- **Quinary**: Meta AI / Muse Spark (optional)
- **Senary**: DeepSeek (optional)

Configure additional providers in `.env` to enable automatic routing based on model name.

## Technical Details

### Windows Console Signals

| Signal | Can Target Process? | Notes |
|--------|---------------------|-------|
| `CTRL_C_EVENT` | ❌ No | Goes to ALL processes sharing console |
| `CTRL_BREAK_EVENT` | ✅ Yes | Can target specific process group |

The script uses `CTRL_BREAK_EVENT` for targeted shutdown requests.

### Python asyncio on Windows

- `loop.add_signal_handler()` raises `NotImplementedError` on Windows
- The server uses `signal.signal()` with `loop.call_soon_threadsafe()` instead
- An exception handler suppresses common shutdown errors (`ConnectionResetError`, invalid handle)