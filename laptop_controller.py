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

# ---------------------------------------------------
