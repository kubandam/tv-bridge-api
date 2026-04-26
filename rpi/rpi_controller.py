#!/usr/bin/env python3
"""
RASPBERRY PI CONTROLLER - Main entry point for TV Bridge system

This script:
1. Sends heartbeats to API every 10 seconds
2. Polls for commands from API
3. Manages subprocesses (FFmpeg capture, CLIP detection)
4. Reports system stats (CPU, memory, disk)
5. Uploads images to image log

Usage:
  python3 rpi_controller.py

Configuration is via .env file or environment variables.
"""

# Load .env file if present
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # dotenv not installed, use system env vars

import os
import sys
import time
import json
import signal
import shutil
import subprocess
import threading
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Tuple
import urllib.request
import urllib.error

# Optional: for system stats
try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False
    print("[WARN] psutil not installed - system stats will not be available")
    print("       Install with: pip3 install psutil")

# ============================================================
# CONFIGURATION
# ============================================================

# API Configuration
API_BASE_URL = os.environ.get("API_BASE_URL", "https://tv-bridge-api-ih76.onrender.com")
API_KEY = os.environ.get("API_KEY", "BX5SXQVhiiRQxoSCWWqV2pE6M1nBF6Pg")
DEVICE_ID = os.environ.get("DEVICE_ID", "tv-1")

# Intervals (seconds)
HEARTBEAT_INTERVAL = 10
COMMAND_POLL_INTERVAL = 5

# Capture configuration
CAPTURE_DIR = os.environ.get("CAPTURE_DIR", "nova")
CHANNEL = os.environ.get("CHANNEL", "Jednotka HD(Towercom)")
CAPTURE_FPS = os.environ.get("CAPTURE_FPS", "0.5")
CHANNELS_CONF = os.environ.get("CHANNELS_CONF", "channels.conf")

# Max images to keep locally
MAX_LOCAL_IMAGES = int(os.environ.get("MAX_LOCAL_IMAGES", "100"))

# ============================================================
# GLOBAL STATE
# ============================================================

class ControllerState:
    def __init__(self):
        self.running = True
        self.capture_process: Optional[subprocess.Popen] = None
        self.detect_process: Optional[subprocess.Popen] = None
        self.tzap_process: Optional[subprocess.Popen] = None
        self.frames_captured = 0
        self.frames_processed = 0
        self.ads_detected = 0
        self.last_command_id = 0
        self.lock = threading.Lock()

state = ControllerState()


# ============================================================
# API CLIENT
# ============================================================

def api_request(method: str, path: str, body: Optional[Dict] = None, timeout: float = 10.0) -> Tuple[bool, Any]:
    """Make API request and return (success, response_or_error)"""
    url = f"{API_BASE_URL.rstrip('/')}{path}"

    data = None
    if body:
        data = json.dumps(body).encode("utf-8")

    req = urllib.request.Request(
        url=url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": API_KEY,
            "X-Device-Id": DEVICE_ID,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8", errors="replace")
            return True, json.loads(resp_body) if resp_body else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        return False, f"HTTP {e.code}: {detail}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def send_heartbeat():
    """Send heartbeat to API with current status"""
    cpu_percent = None
    memory_percent = None
    disk_percent = None

    if HAS_PSUTIL:
        try:
            cpu_percent = psutil.cpu_percent(interval=0.1)
            memory_percent = psutil.virtual_memory().percent
            disk_percent = psutil.disk_usage('/').percent
        except:
            pass

    with state.lock:
        body = {
            "capture_running": state.capture_process is not None and state.capture_process.poll() is None,
            "detect_running": state.detect_process is not None and state.detect_process.poll() is None,
            "frames_captured": state.frames_captured,
            "frames_processed": state.frames_processed,
            "ads_detected": state.ads_detected,
            "cpu_percent": cpu_percent,
            "memory_percent": memory_percent,
            "disk_percent": disk_percent,
        }

    success, result = api_request("POST", "/v1/rpi/heartbeat", body)
    if not success:
        print(f"[HEARTBEAT] Error: {result}")
    return success


def poll_commands():
    """Poll for pending commands from API"""
    success, result = api_request("GET", f"/v1/rpi/commands/pull?after_id={state.last_command_id}")

    if not success:
        print(f"[COMMANDS] Poll error: {result}")
        return []

    return result if isinstance(result, list) else []


def ack_command(command_id: int, status: str, result: Dict = None):
    """Acknowledge command completion"""
    body = {"status": status, "result": result or {}}
    success, resp = api_request("POST", f"/v1/rpi/commands/{command_id}/ack", body)
    if not success:
        print(f"[COMMANDS] Ack error: {resp}")
    return success


# ============================================================
# PROCESS MANAGEMENT
# ============================================================

def find_tuner_command():
    """Find available tuner command (tzap or dvbv5-zap)"""
    if shutil.which("tzap"):
        return "tzap"
    elif shutil.which("dvbv5-zap"):
        return "dvbv5-zap"
    return None


def release_dvb_device():
    """Kill any processes using the DVB adapter"""
    print("[DVB] Releasing DVB device...")
    try:
        # Find processes using the DVB device
        result = subprocess.run(
            ["sudo", "fuser", "-k", "/dev/dvb/adapter0/frontend0"],
            capture_output=True,
            timeout=5,
        )
        # fuser returns 1 if no process found, which is fine
        time.sleep(1)
    except Exception as e:
        print(f"[DVB] fuser error (may be ok): {e}")


def stop_tvheadend():
    """Stop Tvheadend service to free the DVB tuner"""
    print("[TVHEADEND] Stopping service...")
    try:
        result = subprocess.run(
            ["sudo", "systemctl", "stop", "tvheadend"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            print("[TVHEADEND] Service stopped")
            time.sleep(3)  # Wait longer for tuner to be released
            # Also try to release any remaining processes
            release_dvb_device()
            return True
        else:
            print(f"[TVHEADEND] Stop failed: {result.stderr.decode()}")
            return False
    except subprocess.TimeoutExpired:
        print("[TVHEADEND] Stop command timed out")
        return False
    except Exception as e:
        print(f"[TVHEADEND] Error: {e}")
        return False


def start_tvheadend():
    """Restart Tvheadend service"""
    print("[TVHEADEND] Starting service...")
    try:
        subprocess.run(
            ["sudo", "systemctl", "start", "tvheadend"],
            capture_output=True,
            timeout=10,
        )
        print("[TVHEADEND] Service started")
    except Exception as e:
        print(f"[TVHEADEND] Start error: {e}")


def start_tuner():
    """Start DVB tuner using available command (tzap or dvbv5-zap)"""
    if state.tzap_process and state.tzap_process.poll() is None:
        print("[TUNER] Already running")
        return True

    # First stop Tvheadend to free the tuner
    stop_tvheadend()

    tuner_cmd = find_tuner_command()

    if not tuner_cmd:
        print("[TUNER] ERROR: No tuner command found!")
        print("[TUNER] Install one of:")
        print("        sudo apt install dvb-apps     # for tzap")
        print("        sudo apt install dvb-tools    # for dvbv5-zap")
        return False

    # Resolve channels.conf path relative to script directory
    script_dir = Path(__file__).parent
    channels_path = Path(CHANNELS_CONF)
    if not channels_path.is_absolute():
        # Try script directory first
        if (script_dir / CHANNELS_CONF).exists():
            channels_path = script_dir / CHANNELS_CONF
        # Otherwise use as-is (current directory)

    print(f"[TUNER] Using {tuner_cmd} for channel: {CHANNEL}")
    print(f"[TUNER] Channels file: {channels_path}")

    if not channels_path.exists():
        print(f"[TUNER] ERROR: Channels file not found: {channels_path}")
        return False

    try:
        if tuner_cmd == "tzap":
            proc = subprocess.Popen(
                ["tzap", "-c", str(channels_path), "-r", CHANNEL],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        else:  # dvbv5-zap
            proc = subprocess.Popen(
                ["dvbv5-zap", "-c", str(channels_path), "-r", CHANNEL],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        # Wait a bit and check if still running
        time.sleep(2)

        if proc.poll() is None:
            # Process is still running - success!
            state.tzap_process = proc
            print(f"[TUNER] Started with PID {proc.pid}")
            time.sleep(3)  # Wait for signal lock
            return True
        else:
            # Process exited - get error output
            stdout, stderr = proc.communicate(timeout=1)
            print(f"[TUNER] Process exited with code {proc.returncode}")
            if stderr:
                print(f"[TUNER] Error: {stderr.decode().strip()}")
            if stdout:
                print(f"[TUNER] Output: {stdout.decode().strip()}")
            return False

    except FileNotFoundError:
        print(f"[TUNER] Command not found: {tuner_cmd}")
        return False
    except Exception as e:
        print(f"[TUNER] Failed to start: {e}")
        return False


def stop_tuner():
    """Stop tuner process"""
    proc = state.tzap_process
    state.tzap_process = None
    if proc is None:
        return
    print("[TUNER] Stopping...")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    except Exception:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    print("[TUNER] Stopped")


def start_capture():
    """Start FFmpeg capture process"""
    if state.capture_process and state.capture_process.poll() is None:
        print("[CAPTURE] Already running")
        return True

    # Ensure capture directory exists
    capture_path = Path(CAPTURE_DIR)
    capture_path.mkdir(parents=True, exist_ok=True)

    # First ensure tuner is running
    if not start_tuner():
        return False

    print(f"[CAPTURE] Starting FFmpeg capture to {CAPTURE_DIR}")

    try:
        state.capture_process = subprocess.Popen(
            [
                "ffmpeg", "-y",
                "-i", "/dev/dvb/adapter0/dvr0",
                "-vf", f"fps={CAPTURE_FPS}",
                "-q:v", "2",
                "-f", "image2",
                "-strftime", "1",
                f"{CAPTURE_DIR}/capture_%Y%m%d_%H%M%S.jpg"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        print(f"[CAPTURE] Started with PID {state.capture_process.pid}")
        return True
    except Exception as e:
        print(f"[CAPTURE] Failed to start: {e}")
        return False


def stop_capture():
    """Stop FFmpeg capture process"""
    proc = state.capture_process
    state.capture_process = None  # Clear first to avoid race with main loop
    if proc is None:
        return
    print("[CAPTURE] Stopping...")
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    except Exception:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    print("[CAPTURE] Stopped")


def start_detect():
    """Start CLIP detection process"""
    if state.detect_process and state.detect_process.poll() is None:
        print("[DETECT] Already running")
        return True

    print(f"[DETECT] Starting CLIP detection on {CAPTURE_DIR}")

    try:
        # Check if rpi_detect.py exists
        script_path = Path(__file__).parent / "rpi_detect.py"
        if not script_path.exists():
            print(f"[DETECT] ERROR: Script not found: {script_path}")
            print(f"[DETECT] Make sure rpi_detect.py is in the same directory as rpi_controller.py")
            return False
        
        # Check if capture directory exists
        capture_path = Path(CAPTURE_DIR)
        if not capture_path.exists():
            print(f"[DETECT] Creating capture directory: {capture_path}")
            capture_path.mkdir(parents=True, exist_ok=True)
        
        # Check if there are any images to process
        image_count = len(list(capture_path.glob("*.jpg")))
        print(f"[DETECT] Capture directory has {image_count} images")
        if image_count == 0:
            print(f"[DETECT] WARNING: No images in {CAPTURE_DIR} - make sure capture is running first!")
        
        # Run rpi_detect.py with same Python as controller (venv if activated)
        python_exe = sys.executable
        print(f"[DETECT] Executing: {python_exe} {script_path} {CAPTURE_DIR}")
        state.detect_process = subprocess.Popen(
            [python_exe, str(script_path), CAPTURE_DIR],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        print(f"[DETECT] Started with PID {state.detect_process.pid}")

        # Start thread to read output
        def read_output():
            for line in state.detect_process.stdout:
                line = line.strip()
                if line:
                    print(f"[DETECT] {line}")
                    # Parse output to update counters
                    if "AD" in line or "OK" in line:
                        with state.lock:
                            state.frames_processed += 1
                            if "AD" in line and "🚨" in line:
                                state.ads_detected += 1
            
            # Check if process exited with error
            exit_code = state.detect_process.wait()
            if exit_code != 0:
                print(f"[DETECT] Process exited with code {exit_code}")
                with state.lock:
                    state.detect_process = None

        threading.Thread(target=read_output, daemon=True).start()
        
        # Give it a moment to start and check if it failed immediately
        time.sleep(0.5)
        if state.detect_process and state.detect_process.poll() is not None:
            exit_code = state.detect_process.poll()
            print(f"[DETECT] Failed to start - process exited immediately with code {exit_code}")
            state.detect_process = None
            return False
        
        return True
    except Exception as e:
        print(f"[DETECT] Failed to start: {e}")
        import traceback
        traceback.print_exc()
        return False


def stop_detect():
    """Stop CLIP detection process"""
    proc = state.detect_process
    state.detect_process = None
    if proc is None:
        return
    print("[DETECT] Stopping...")
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    except Exception:
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    print("[DETECT] Stopped")


def stop_all():
    """Stop all processes"""
    print("[CONTROLLER] Stopping all processes...")
    stop_detect()
    stop_capture()
    stop_tuner()
    cleanup_images()
    # Optionally restart Tvheadend
    start_tvheadend()
    print("[CONTROLLER] All processes stopped")


def restart_all():
    """Restart all processes"""
    print("[CONTROLLER] Restarting all processes...")
    stop_all()
    time.sleep(2)
    start_capture()
    time.sleep(2)
    start_detect()
    print("[CONTROLLER] All processes restarted")


def cleanup_images():
    """Remove all captured images"""
    capture_path = Path(CAPTURE_DIR)
    if capture_path.exists():
        for f in capture_path.glob("*.jpg"):
            try:
                f.unlink()
            except:
                pass
        print(f"[CLEANUP] Removed images from {CAPTURE_DIR}")


# ============================================================
# COMMAND HANDLERS
# ============================================================

def handle_command(cmd: Dict) -> Tuple[str, Dict]:
    """Handle a command and return (status, result)"""
    cmd_type = cmd.get("type", "")
    payload = cmd.get("payload", {})

    print(f"[COMMAND] Executing: {cmd_type} {payload}")

    try:
        if cmd_type == "start_capture":
            success = start_capture()
            return ("done" if success else "failed", {"started": success})

        elif cmd_type == "stop_capture":
            stop_capture()
            return ("done", {"stopped": True})

        elif cmd_type == "start_detect":
            success = start_detect()
            return ("done" if success else "failed", {"started": success})

        elif cmd_type == "stop_detect":
            stop_detect()
            return ("done", {"stopped": True})

        elif cmd_type == "restart_all":
            restart_all()
            return ("done", {"restarted": True})

        elif cmd_type == "stop_all":
            stop_all()
            return ("done", {"stopped": True})

        elif cmd_type == "set_channel":
            global CHANNEL
            new_channel = payload.get("channel")
            if new_channel:
                CHANNEL = new_channel
                # Restart capture with new channel
                stop_capture()
                stop_tuner()
                time.sleep(1)
                start_capture()
                return ("done", {"channel": new_channel})
            return ("failed", {"error": "No channel specified"})

        elif cmd_type == "set_config":
            import rpi_detect as _det
            for key, value in payload.items():
                if key == "capture_fps":
                    global CAPTURE_FPS
                    CAPTURE_FPS = str(value)
                elif key == "capture_dir":
                    global CAPTURE_DIR
                    CAPTURE_DIR = str(value)
                elif key == "threshold":
                    _det.THRESHOLD = float(value)
                elif key == "threshold_zeroshot":
                    _det.THRESHOLD_ZEROSHOT = float(value)
                elif key == "smooth_window":
                    _det.SMOOTH_WINDOW = int(value)
            return ("done", {"updated": list(payload.keys())})

        else:
            return ("failed", {"error": f"Unknown command type: {cmd_type}"})

    except Exception as e:
        return ("failed", {"error": str(e)})


# ============================================================
# MAIN LOOPS
# ============================================================

def heartbeat_loop():
    """Background thread for sending heartbeats"""
    while state.running:
        try:
            send_heartbeat()
        except Exception as e:
            print(f"[HEARTBEAT] Error: {e}")

        # Sleep in small intervals to allow quick shutdown
        for _ in range(HEARTBEAT_INTERVAL * 10):
            if not state.running:
                break
            time.sleep(0.1)


def command_loop():
    """Background thread for polling commands"""
    while state.running:
        try:
            commands = poll_commands()
            for cmd in commands:
                cmd_id = cmd.get("id", 0)
                if cmd_id > state.last_command_id:
                    state.last_command_id = cmd_id
                    status, result = handle_command(cmd)
                    ack_command(cmd_id, status, result)
        except Exception as e:
            print(f"[COMMANDS] Error: {e}")

        # Sleep in small intervals
        for _ in range(COMMAND_POLL_INTERVAL * 10):
            if not state.running:
                break
            time.sleep(0.1)


def frame_counter_loop():
    """Background thread for counting captured frames"""
    capture_path = Path(CAPTURE_DIR)

    while state.running:
        try:
            if capture_path.exists():
                images = list(capture_path.glob("*.jpg"))
                with state.lock:
                    state.frames_captured = len(images)

                # Cleanup old images if too many
                if len(images) > MAX_LOCAL_IMAGES:
                    images.sort(key=lambda p: p.stat().st_mtime)
                    for img in images[:-MAX_LOCAL_IMAGES]:
                        try:
                            img.unlink()
                        except:
                            pass
        except Exception as e:
            pass

        time.sleep(1)


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    print("\n[CONTROLLER] Received shutdown signal...")
    state.running = False
    stop_all()
    sys.exit(0)


def main():
    print("=" * 50)
    print("  TV BRIDGE - Raspberry Pi Controller")
    print("=" * 50)
    print(f"API: {API_BASE_URL}")
    print(f"Device ID: {DEVICE_ID}")
    print(f"Capture Dir: {CAPTURE_DIR}")
    print(f"Channel: {CHANNEL}")
    print(f"Capture FPS: {CAPTURE_FPS}")

    # Check for tuner command
    tuner_cmd = find_tuner_command()
    if tuner_cmd:
        print(f"Tuner: {tuner_cmd}")
    else:
        print("Tuner: NOT FOUND!")
        print("  Install: sudo apt install dvb-apps (tzap)")
        print("       or: sudo apt install dvb-tools (dvbv5-zap)")

    print("=" * 50)
    print()

    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Create capture directory
    Path(CAPTURE_DIR).mkdir(parents=True, exist_ok=True)

    # Start background threads
    threads = [
        threading.Thread(target=heartbeat_loop, daemon=True),
        threading.Thread(target=command_loop, daemon=True),
        threading.Thread(target=frame_counter_loop, daemon=True),
    ]

    for t in threads:
        t.start()

    print("[CONTROLLER] Started. Waiting for commands from API...")
    print("[CONTROLLER] Press Ctrl+C to stop")
    print()

    # Send initial heartbeat
    send_heartbeat()

    # Main loop - just keep alive
    try:
        while state.running:
            time.sleep(1)

            # Check if subprocesses have died unexpectedly
            # Use lock to avoid race conditions with command handlers
            with state.lock:
                if state.capture_process and state.capture_process.poll() is not None:
                    print("[CAPTURE] Process died unexpectedly")
                    state.capture_process = None

                if state.detect_process and state.detect_process.poll() is not None:
                    print("[DETECT] Process died unexpectedly")
                    state.detect_process = None

                if state.tzap_process and state.tzap_process.poll() is not None:
                    print("[TUNER] Process died unexpectedly")
                    state.tzap_process = None

    except KeyboardInterrupt:
        pass

    print("\n[CONTROLLER] Shutting down...")
    state.running = False
    stop_all()
    print("[CONTROLLER] Goodbye!")


if __name__ == "__main__":
    main()
