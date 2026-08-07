"""
Lax
=============================================
Control your Windows laptop from your phone via a Flask web UI + ngrok.

"""

import os
import sys
import json
import time
import socket
import secrets
import logging
import platform
import threading
import subprocess
import webbrowser
from logging.handlers import RotatingFileHandler
from functools import wraps

# ---------------------------------------------------------------------------
# 0. Logging...set this up first, before touching the console at all...so that absolutely anything that goes wrong gets written to a file we can read afterwards. Nothing should ever be
#    able to fail silently.
# ---------------------------------------------------------------------------
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "LaptopController")
os.makedirs(APP_DIR, exist_ok=True)
LOG_FILE = os.path.join(APP_DIR, "controller.log")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")

logger = logging.getLogger("laptop_controller")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
logger.info("---- process starting (pid=%s) ----", os.getpid())

# ---------------------------------------------------------------------------
# 1. When launched with python.exe for troubleshooting, you will now see real errors.
# ---------------------------------------------------------------------------
try:
    import ctypes
except Exception:
    logger.exception("failed to import ctypes")


def safe(default=None, log_name="operation"):
    """Decorator: never let a route/helper raise past this point."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                logger.exception(f"{log_name} failed: {e}")
                return default
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# 2. Config / auth token....a random token is generated on first run and stored locally.
# ---------------------------------------------------------------------------
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if cfg.get("token"):
                    return cfg
        except Exception:
            pass
    cfg = {"token": secrets.token_hex(16), "port": 5000}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


CONFIG = load_config()
AUTH_TOKEN = CONFIG["token"]
PORT = CONFIG.get("port", 5000)

# ---------------------------------------------------------------------------
# 3. Third-party imports
# ---------------------------------------------------------------------------
try:
    from flask import Flask, render_template_string, request, jsonify, g
    from flask_cors import CORS
    import psutil
    import requests
except ImportError as e:
    msg = (
        f"REQUIRED PACKAGE MISSING: {e}\n\n"
        "Run setup.bat again (double-click it) to install dependencies, "
        "then try starting the app again.\n"
    )
    print(msg)
    logger.exception("required package missing")
    try:
        with open(os.path.join(APP_DIR, "CRASHED.txt"), "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        pass
    sys.exit(1)

try:
    import screen_brightness_control as sbc
except Exception:
    sbc = None
    logger.warning("screen_brightness_control unavailable; brightness disabled")

try:
    import pygetwindow as gw
except Exception:
    gw = None
    logger.warning("pygetwindow unavailable; app list/close disabled")

try:
    import win32gui  # noqa: F401
    import win32con  # noqa: F401
except Exception:
    pass

try:
    import win32clipboard
except Exception:
    win32clipboard = None
    logger.warning("win32clipboard unavailable; clipboard push disabled")

app = Flask(__name__)
CORS(app)
log_werkzeug = logging.getLogger("werkzeug")
log_werkzeug.setLevel(logging.WARNING)


@app.before_request
def check_auth():
    if request.path in ("/", "/favicon.ico"):
        return None
    if request.path.startswith("/api/login"):
        return None
    supplied = request.headers.get("X-Auth-Token") or request.args.get("token")
    if supplied != AUTH_TOKEN:
        return jsonify({"status": "error", "message": "unauthorized"}), 401
