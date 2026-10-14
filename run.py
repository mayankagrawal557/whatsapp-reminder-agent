"""
WhatsApp AI Reminder Agent — Crash-Proof Root Runner & Process Supervisor.

Guarantees 24/7 uptime for http://127.0.0.1:8000:
1. Auto-Dependency Guard: Silently detects and installs missing packages from requirements.txt.
2. Pre-Flight Schema Check: Validates SQLite database tables & column migrations prior to startup.
3. Crash-Proof Supervisor Loop: Automatically detects Uvicorn crashes (transient reload syntax errors,
   uncaught exceptions, or process terminations) and relaunches Uvicorn on port 8000 within 2 seconds.
"""
import importlib
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

# Project paths
ROOT_DIR = Path(__file__).resolve().parent
DATA_DIR = ROOT_DIR / "data"
REQUIREMENTS_FILE = ROOT_DIR / "requirements.txt"

# Critical dependencies to verify before launching
REQUIRED_PACKAGES = [
    ("fastapi", "fastapi>=0.110.0"),
    ("uvicorn", "uvicorn[standard]>=0.28.0"),
    ("sqlalchemy", "sqlalchemy[asyncio]>=2.0.28"),
    ("greenlet", "greenlet>=3.0.0"),
    ("aiosqlite", "aiosqlite>=0.20.0"),
    ("pydantic", "pydantic>=2.6.4"),
    ("pydantic_settings", "pydantic-settings>=2.2.1"),
    ("apscheduler", "apscheduler>=3.10.4"),
    ("httpx", "httpx>=0.27.0"),
    ("multipart", "python-multipart>=0.0.9"),
    ("jinja2", "jinja2>=3.1.3"),
    ("openpyxl", "openpyxl>=3.1.2"),
    ("google.genai", "google-genai>=0.1.1"),
    ("bcrypt", "bcrypt>=4.0.0"),
    ("jwt", "PyJWT>=2.8.0"),
    ("email_validator", "email-validator>=2.0.0"),
]


def log_msg(tag: str, message: str) -> None:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] [{tag}] {message}", flush=True)


def ensure_dependencies() -> None:
    """Check for missing packages and silently auto-install from requirements.txt."""
    missing = []
    for mod_name, pkg_spec in REQUIRED_PACKAGES:
        try:
            importlib.import_module(mod_name)
        except (ModuleNotFoundError, ImportError):
            missing.append(pkg_spec)

    if missing:
        log_msg("DEPENDENCY-GUARD", f"Missing packages detected: {missing}. Installing silently...")
        try:
            if REQUIREMENTS_FILE.exists():
                subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS_FILE), "--quiet"],
                    check=True,
                )
            else:
                subprocess.run(
                    [sys.executable, "-m", "pip", "install"] + missing + ["--quiet"],
                    check=True,
                )
            log_msg("DEPENDENCY-GUARD", "All dependencies installed successfully.")
        except Exception as exc:
            log_msg("DEPENDENCY-GUARD", f"Warning: pip install encountered an issue: {exc}")


def preflight_database_check() -> None:
    """
    Ensure directories exist and execute database schema initialization
    to ensure tables and columns (user_id, customer_reply, replied_at) are ready.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (ROOT_DIR / "app" / "static").mkdir(parents=True, exist_ok=True)
    (ROOT_DIR / "app" / "templates").mkdir(parents=True, exist_ok=True)

    try:
        import asyncio
        from app.database import init_db

        asyncio.run(init_db())
        log_msg("SCHEMA-CHECK", "SQLite database schema verified & ready.")
    except Exception as exc:
        log_msg("SCHEMA-CHECK", f"Pre-flight schema warning: {exc}")
        traceback.print_exc()


def is_port_in_use(port: int = 8000, host: str = "127.0.0.1") -> bool:
    """Check if the target host:port is currently bound."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def run_supervisor(host: str = "127.0.0.1", port: int = 8000, reload: bool = True) -> None:
    """
    Persistent process supervisor.
    Supervises Uvicorn in a loop so transient reload crashes or file editing syntax errors
    never permanently terminate the server.
    """
    restart_delay = 2.0  # seconds to wait before auto-recovery
    consecutive_fast_crashes = 0

    print("=" * 68, flush=True)
    print("   WHATSAPP AI REMINDER AGENT — CRASH-PROOF SUPERVISOR", flush=True)
    print(f"   Target URL: http://{host}:{port} | Auto-Recovery: ACTIVE", flush=True)
    print("=" * 68, flush=True)

    while True:
        try:
            # 1. Dependency Guard
            ensure_dependencies()

            # 2. Database Schema Pre-Flight Check
            preflight_database_check()

            # 3. Port conflict prevention
            if is_port_in_use(port, host):
                log_msg("SUPERVISOR", f"Port {port} is occupied. Waiting for release...")
                for _ in range(5):
                    time.sleep(1)
                    if not is_port_in_use(port, host):
                        break

            # 3b. Launch Baileys WhatsApp Web Bridge (port 3001) if node is present
            bridge_process = None
            bridge_dir = ROOT_DIR / "bridge"
            server_js = bridge_dir / "server.js"
            if shutil.which("node") and server_js.exists() and not is_port_in_use(3001, "127.0.0.1"):
                try:
                    log_msg("SUPERVISOR", "Launching Baileys WhatsApp Web Bridge on http://127.0.0.1:3001...")
                    cflags = 0x00000008 if sys.platform == "win32" else 0
                    bridge_process = subprocess.Popen(["node", "server.js"], cwd=str(bridge_dir), creationflags=cflags)
                except Exception as b_err:
                    log_msg("SUPERVISOR", f"Warning: Could not launch Baileys bridge: {b_err}")

            # 4. Launch Uvicorn
            log_msg("SUPERVISOR", f"Launching Uvicorn server on http://{host}:{port} (reload={reload})...")

            cmd = [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                host,
                "--port",
                str(port),
            ]
            if reload:
                cmd.append("--reload")

            start_time = time.time()
            process = subprocess.Popen(cmd, cwd=str(ROOT_DIR))

            # Block until child exits
            exit_code = process.wait()
            uptime_seconds = time.time() - start_time

            if exit_code == 0:
                log_msg("SUPERVISOR", "Server process terminated cleanly (exit code 0). Exiting.")
                break

            # If child crashed in less than 2 seconds, increment fast crash backoff
            if uptime_seconds < 2.0:
                consecutive_fast_crashes += 1
                backoff = min(restart_delay * (1.5 ** consecutive_fast_crashes), 10.0)
            else:
                consecutive_fast_crashes = 0
                backoff = restart_delay

            log_msg(
                "SUPERVISOR",
                f"Uvicorn terminated with code {exit_code} (uptime: {uptime_seconds:.1f}s). "
                f"Auto-restarting server in {backoff:.1f}s...",
            )
            time.sleep(backoff)

        except KeyboardInterrupt:
            log_msg("SUPERVISOR", "KeyboardInterrupt detected. Shutting down cleanly...")
            if "process" in locals() and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    process.kill()
            break
        except Exception as exc:
            log_msg("SUPERVISOR", f"Supervisor caught unexpected error: {exc}")
            traceback.print_exc()
            time.sleep(restart_delay)


if __name__ == "__main__":
    run_supervisor()
