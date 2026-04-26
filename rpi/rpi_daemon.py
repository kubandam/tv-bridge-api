#!/usr/bin/env python3
"""
RASPBERRY PI DAEMON - Lightweight service for controller lifecycle management

This daemon:
1. Runs continuously in the background as a systemd service
2. Polls API for controller commands (start_controller, stop_controller)
3. Starts/stops rpi_controller.py as a separate subprocess
4. Reports daemon status to API

Usage:
  python3 rpi_daemon.py

Configuration via .env file or environment variables.
"""

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os
import sys
import time
import json
import signal
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
import urllib.request
import urllib.error

# ============================================================
# CONFIGURATION
# ============================================================

API_BASE_URL = os.environ.get("API_BASE_URL", "https://tv-bridge-api-ih76.onrender.com")
API_KEY = os.environ.get("API_KEY", "BX5SXQVhiiRQxoSCWWqV2pE6M1nBF6Pg")
DEVICE_ID = os.environ.get("DEVICE_ID", "tv-1")

# Daemon settings
DAEMON_POLL_INTERVAL = 5  # Check for commands every 5 seconds
CONTROLLER_SCRIPT = Path(__file__).parent / "rpi_controller.py"

# ============================================================
# GLOBAL STATE
# ============================================================

class DaemonState:
    def __init__(self):
        self.running = True
        self.controller_process: Optional[subprocess.Popen] = None
        self.last_command_id = 0
        self.lock = threading.Lock()

state = DaemonState()

# ============================================================
# API CLIENT
# ============================================================

def api_request(method: str, path: str, body: Optional[Dict] = None, timeout: float = 10.0) -> Tuple[bool, Any]:
    """Make API request and return (success, response_or_error)"""
    url = f"{API_BASE_URL.rstrip('/')}{path}"
    
    data = None
    if body:
        data = json.dumps(body).encode("utf-8")
    
    headers = {
        "X-API-Key": API_KEY,
        "X-Device-Id": DEVICE_ID,
        "Content-Type": "application/json",
    }
    
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_data = json.loads(resp.read().decode("utf-8"))
            return True, response_data
    except urllib.error.HTTPError as e:
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            return False, error_body
        except:
            return False, {"error": f"HTTP {e.code}", "detail": str(e)}
    except Exception as e:
        return False, {"error": "request_failed", "detail": str(e)}

# ============================================================
# CONTROLLER LIFECYCLE
# ============================================================

def is_controller_running() -> bool:
    """Check if controller process is running"""
    with state.lock:
        if state.controller_process is None:
            return False
        
        # Check if process is still alive
        poll = state.controller_process.poll()
        if poll is not None:
            # Process has exited
            state.controller_process = None
            return False
        
        return True

def start_controller() -> Tuple[bool, str]:
    """Start rpi_controller.py as subprocess"""
    with state.lock:
        if state.controller_process is not None:
            poll = state.controller_process.poll()
            if poll is None:
                return False, "Controller already running"
            else:
                # Process exited, clean up
                state.controller_process = None
        
        # Start controller
        try:
            print(f"[DAEMON] Starting controller: {CONTROLLER_SCRIPT}")
            
            # Start as subprocess with output redirected to log file
            log_file = Path(__file__).parent / "controller.log"
            log_handle = open(log_file, "a")
            
            process = subprocess.Popen(
                [sys.executable, str(CONTROLLER_SCRIPT)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                cwd=Path(__file__).parent,
            )
            
            state.controller_process = process
            print(f"[DAEMON] Controller started with PID {process.pid}")
            return True, f"Controller started (PID {process.pid})"
            
        except Exception as e:
            print(f"[DAEMON] Failed to start controller: {e}")
            return False, f"Failed to start: {str(e)}"

def stop_controller() -> Tuple[bool, str]:
    """Stop rpi_controller.py gracefully"""
    with state.lock:
        if state.controller_process is None:
            return False, "Controller not running"
        
        poll = state.controller_process.poll()
        if poll is not None:
            state.controller_process = None
            return False, "Controller already stopped"
        
        try:
            print(f"[DAEMON] Stopping controller (PID {state.controller_process.pid})")
            
            # Send SIGTERM for graceful shutdown
            state.controller_process.terminate()
            
            # Wait up to 10 seconds for graceful exit
            try:
                state.controller_process.wait(timeout=10)
                print("[DAEMON] Controller stopped gracefully")
            except subprocess.TimeoutExpired:
                print("[DAEMON] Controller didn't stop, sending SIGKILL")
                state.controller_process.kill()
                state.controller_process.wait()
            
            state.controller_process = None
            return True, "Controller stopped"
            
        except Exception as e:
            print(f"[DAEMON] Failed to stop controller: {e}")
            return False, f"Failed to stop: {str(e)}"

# ============================================================
# COMMAND POLLING
# ============================================================

def poll_commands():
    """Poll API for daemon commands"""
    success, response = api_request(
        "GET",
        f"/v1/rpi/daemon-commands?device_id={DEVICE_ID}&since={state.last_command_id}"
    )
    
    if not success:
        print(f"[DAEMON] Command poll failed: {response}")
        return
    
    commands = response.get("commands", [])
    if not commands:
        return
    
    print(f"[DAEMON] Received {len(commands)} command(s)")
    
    for cmd in commands:
        cmd_id = cmd["id"]
        cmd_type = cmd["type"]
        
        print(f"[DAEMON] Processing command {cmd_id}: {cmd_type}")
        
        # Update last_command_id
        if cmd_id > state.last_command_id:
            state.last_command_id = cmd_id
        
        # Execute command
        if cmd_type == "start_controller":
            success, message = start_controller()
        elif cmd_type == "stop_controller":
            success, message = stop_controller()
        else:
            success = False
            message = f"Unknown command type: {cmd_type}"
        
        # Report result back to API
        result_payload = {
            "status": "done" if success else "failed",
            "result": {"message": message}
        }
        
        api_request("PUT", f"/v1/rpi/daemon-commands/{cmd_id}", body=result_payload)
        print(f"[DAEMON] Command {cmd_id} result: {message}")

# ============================================================
# DAEMON STATUS REPORTING
# ============================================================

def send_daemon_status():
    """Send daemon status to API"""
    controller_running = is_controller_running()
    
    payload = {
        "daemon_running": True,
        "controller_running": controller_running,
        "controller_pid": state.controller_process.pid if state.controller_process else None,
    }
    
    success, response = api_request("POST", f"/v1/rpi/daemon-status", body=payload)
    
    if not success:
        print(f"[DAEMON] Status update failed: {response}")

# ============================================================
# SIGNAL HANDLERS
# ============================================================

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    print(f"\n[DAEMON] Received signal {signum}, shutting down...")
    state.running = False
    
    # Stop controller if running
    if is_controller_running():
        print("[DAEMON] Stopping controller before exit...")
        stop_controller()

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("=" * 60)
    print("  RASPBERRY PI DAEMON - Controller Lifecycle Manager")
    print("=" * 60)
    print(f"Device ID: {DEVICE_ID}")
    print(f"API: {API_BASE_URL}")
    print(f"Controller script: {CONTROLLER_SCRIPT}")
    print(f"Poll interval: {DAEMON_POLL_INTERVAL}s")
    print("=" * 60)
    print()
    
    if not CONTROLLER_SCRIPT.exists():
        print(f"[ERROR] Controller script not found: {CONTROLLER_SCRIPT}")
        sys.exit(1)
    
    print("[DAEMON] Starting daemon loop...")
    print("[DAEMON] Waiting for API commands...")
    print()
    
    last_status_time = 0
    STATUS_INTERVAL = 30  # Send status every 30 seconds
    
    while state.running:
        try:
            # Poll for commands
            poll_commands()
            
            # Send status periodically
            now = time.time()
            if now - last_status_time >= STATUS_INTERVAL:
                send_daemon_status()
                last_status_time = now
            
            # Sleep
            time.sleep(DAEMON_POLL_INTERVAL)
            
        except KeyboardInterrupt:
            print("\n[DAEMON] Keyboard interrupt, shutting down...")
            break
        except Exception as e:
            print(f"[DAEMON] Error in main loop: {e}")
            time.sleep(DAEMON_POLL_INTERVAL)
    
    print("[DAEMON] Daemon stopped")
    sys.exit(0)

if __name__ == "__main__":
    main()
