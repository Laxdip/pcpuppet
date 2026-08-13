"""
Lax
=============================================
Control your Windows laptop from your phone via a Flask web UI + ngrok...
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
# 1. Logging
# ---------------------------------------------------------------------------
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "LaptopController")
os.makedirs(APP_DIR, exist_ok=True)
LOG_FILE = os.path.join(APP_DIR, "controller.log")
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
SESSION_FILE = os.path.join(APP_DIR, "sessions.json")

logger = logging.getLogger("laptop_controller")
logger.setLevel(logging.INFO)
_handler = RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(_handler)
logger.info("---- process starting (pid=%s) ----", os.getpid())

try:
    import ctypes
except Exception:
    logger.exception("failed to import ctypes")


def safe(default=None, log_name="operation"):
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
# 2. Config / auth token
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
    cfg = {"token": secrets.token_hex(16), "port": 5000, "security_password": secrets.token_hex(8)}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def save_config():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(CONFIG, f, indent=2)
        return True
    except Exception:
        logger.exception("failed to save config")
        return False


CONFIG = load_config()
AUTH_TOKEN = CONFIG["token"]
PORT = CONFIG.get("port", 5000)
SECURITY_PASSWORD = CONFIG.get("security_password", secrets.token_hex(8))


# ---------------------------------------------------------------------------
# 2b. Device sessions 
# ---------------------------------------------------------------------------
ACTIVE_SESSIONS = {}

def load_sessions_from_disk():
    global ACTIVE_SESSIONS
    if os.path.exists(SESSION_FILE):
        try:
            with open(SESSION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    ACTIVE_SESSIONS = data
                    logger.info(f"Loaded {len(ACTIVE_SESSIONS)} saved sessions")
                    return
        except Exception:
            pass
    ACTIVE_SESSIONS = {}

def save_sessions_to_disk():
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(ACTIVE_SESSIONS, f, indent=2)
    except Exception:
        pass

load_sessions_from_disk()

def create_session(label):
    secret = secrets.token_hex(24)
    handle = secrets.token_hex(4)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    ACTIVE_SESSIONS[secret] = {
        "handle": handle,
        "label": (label or "unknown device")[:80],
        "created": now,
        "last_seen": now,
    }
    save_sessions_to_disk()
    return secret, handle

def touch_session(secret):
    s = ACTIVE_SESSIONS.get(secret)
    if s:
        s["last_seen"] = time.strftime("%Y-%m-%d %H:%M:%S")

def revoke_by_handle(handle):
    for secret, s in list(ACTIVE_SESSIONS.items()):
        if s["handle"] == handle:
            del ACTIVE_SESSIONS[secret]
            save_sessions_to_disk()
            return True
    return False

def list_sessions_view(caller_secret):
    out = []
    for secret, s in ACTIVE_SESSIONS.items():
        out.append({
            "handle": s["handle"],
            "label": s["label"],
            "created": s["created"],
            "last_seen": s["last_seen"],
            "is_you": secret == caller_secret,
        })
    out.sort(key=lambda x: x["last_seen"], reverse=True)
    return out


# ---------------------------------------------------------------------------
# 3. Third-party imports
# ---------------------------------------------------------------------------
try:
    from flask import Flask, render_template_string, request, jsonify, g
    from flask_cors import CORS
    import psutil
    import requests
except ImportError as e:
    msg = f"REQUIRED PACKAGE MISSING: {e}\n\nRun setup.bat again."
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
    logger.warning("screen_brightness_control unavailable")

try:
    import pygetwindow as gw
except Exception:
    gw = None
    logger.warning("pygetwindow unavailable")

try:
    import win32gui
    import win32con
except Exception:
    pass

try:
    import win32clipboard
except Exception:
    win32clipboard = None
    logger.warning("win32clipboard unavailable")

app = Flask(__name__)
CORS(app)
log_werkzeug = logging.getLogger("werkzeug")
log_werkzeug.setLevel(logging.WARNING)


@app.before_request
def check_auth():
    if request.path in ("/", "/favicon.ico"):
        return None
    if request.path == "/api/login":
        return None
    if request.path == "/api/security/unlock":
        return None
    supplied = request.headers.get("X-Auth-Token") or request.args.get("token")
    if supplied not in ACTIVE_SESSIONS:
        return jsonify({"status": "error", "message": "unauthorized"}), 401
    touch_session(supplied)
    g.session_secret = supplied


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    label = data.get("label", "")
    if not secrets.compare_digest(password, AUTH_TOKEN):
        return jsonify({"status": "error", "message": "invalid token"}), 401
    secret, handle = create_session(label)
    logger.info(f"new session: handle={handle} label={label!r}")
    return jsonify({"status": "success", "session": secret, "handle": handle})


@app.route("/api/sessions", methods=["GET"])
def get_sessions():
    return jsonify({"status": "success", "sessions": list_sessions_view(g.get("session_secret"))})


@app.route("/api/sessions/revoke", methods=["POST"])
def revoke_session():
    data = request.get_json(silent=True) or {}
    handle = (data.get("handle") or "").strip()
    if not handle:
        return jsonify({"status": "error", "message": "handle required"}), 400
    ok = revoke_by_handle(handle)
    return jsonify({"status": "success" if ok else "error", "message": None if ok else "session not found"})


# ===== SECURITY PASSWORD ENDPOINT =====
@app.route("/api/security/unlock", methods=["POST"])
def security_unlock():
    data = request.get_json(silent=True) or {}
    password = data.get("password", "")
    if secrets.compare_digest(password, SECURITY_PASSWORD):
        return jsonify({"status": "success", "message": "unlocked"})
    return jsonify({"status": "error", "message": "wrong password"}), 401


# ===== VOLUME HELPERS =====
@safe(default=False, log_name="set_volume")
def set_volume(level):
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, level / 100.0)), None)
    return True


@safe(default=50, log_name="get_volume")
def get_volume():
    from ctypes import cast, POINTER
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    devices = AudioUtilities.GetSpeakers()
    interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    volume = cast(interface, POINTER(IAudioEndpointVolume))
    return int(volume.GetMasterVolumeLevelScalar() * 100)


def nudge_volume(direction):
    VK_VOLUME_UP, VK_VOLUME_DOWN = 0xAF, 0xAE
    key = VK_VOLUME_UP if direction > 0 else VK_VOLUME_DOWN
    for _ in range(5):
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        time.sleep(0.05)


# ===== MESSAGE =====
MB_ICONINFORMATION = 0x40
MB_SYSTEMMODAL = 0x1000

def show_popup_message(title, message):
    def _show():
        try:
            ctypes.windll.user32.MessageBoxW(
                0, message[:1000], (title or "System Notice")[:200],
                MB_ICONINFORMATION | MB_SYSTEMMODAL
            )
        except Exception:
            logger.exception("popup message failed")
    threading.Thread(target=_show, daemon=True).start()


def speak_message(message):
    def _speak():
        try:
            env = os.environ.copy()
            env["LC_SPEAK_TEXT"] = message[:1000]
            ps_script = (
                "Add-Type -AssemblyName System.Speech; "
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                "$s.Speak($env:LC_SPEAK_TEXT)"
            )
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps_script],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            logger.exception("speak message failed")
    threading.Thread(target=_speak, daemon=True).start()


# ===== CLIPBOARD =====
@safe(default="", log_name="get_clipboard")
def get_clipboard_text():
    if win32clipboard is None:
        return ""
    win32clipboard.OpenClipboard()
    try:
        try:
            return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        except Exception:
            return ""
    finally:
        win32clipboard.CloseClipboard()


@safe(default=False, log_name="set_clipboard")
def set_clipboard_text(text):
    if win32clipboard is None:
        return False
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_UNICODETEXT, text)
        return True
    finally:
        win32clipboard.CloseClipboard()


# ===== SCREENSHOT - FAST =====
def take_screenshot_bytes(max_width=1280):
    from PIL import ImageGrab, Image
    import io
    img = ImageGrab.grab()
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), Image.NEAREST)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=55, optimize=True, progressive=False)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# HTML TEMPLATE
# ---------------------------------------------------------------------------
HTML_TEMPLATE = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=5.0, user-scalable=yes">
<title>root@lax:~#</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700;800&display=swap');
  
  :root{
    --bg:#080b10;
    --panel:#0e131b;
    --panel2:#0b0f15;
    --line:#1c2735;
    --arch:#1793d1;
    --arch-soft:#5ec8f8;
    --green:#50fa7b;
    --green-soft:#8dffb0;
    --magenta:#ff5fa8;
    --amber:#ffb86c;
    --red:#ff5f56;
    --text:#cfd9e6;
    --dim:#54677d;
    --dim2:#3a4a5c;
    --brand: #66d9ef;
  }
  
  .theme-cyan{ --arch:#1793d1; --arch-soft:#5ec8f8; --green:#50fa7b; --green-soft:#8dffb0; --magenta:#ff5fa8; --amber:#ffb86c; --red:#ff5f56; --text:#cfd9e6; --dim:#54677d; --dim2:#3a4a5c; --line:#1c2735; --bg:#080b10; --panel:#0e131b; --panel2:#0b0f15; --brand: #66d9ef; }
  .theme-green{ --arch:#50fa7b; --arch-soft:#8dffb0; --green:#50fa7b; --green-soft:#8dffb0; --magenta:#ff5fa8; --amber:#ffb86c; --red:#ff5f56; --text:#cfd9e6; --dim:#3a5a3a; --dim2:#2a4a2a; --line:#2a4a2a; --bg:#0a1008; --panel:#0e180e; --panel2:#0a120a; --brand: #50fa7b; }
  .theme-gold{ --arch:#ffb86c; --arch-soft:#ffd93d; --green:#ffb86c; --green-soft:#ffd93d; --magenta:#ff5fa8; --amber:#ffb86c; --red:#ff5f56; --text:#f5e6d3; --dim:#7a5a3a; --dim2:#5a3a2a; --line:#4a3a2a; --bg:#100a08; --panel:#1a120e; --panel2:#120e0a; --brand: #ffd93d; }
  .theme-red{ --arch:#ff5f56; --arch-soft:#ff8a82; --green:#50fa7b; --green-soft:#8dffb0; --magenta:#ff5fa8; --amber:#ffb86c; --red:#ff5f56; --text:#f5d3d3; --dim:#7a3a3a; --dim2:#5a2a2a; --line:#4a2a2a; --bg:#100808; --panel:#1a0e0e; --panel2:#120a0a; --brand: #ff8a82; }
  .theme-magenta{ --arch:#ff5fa8; --arch-soft:#ff8ec8; --green:#50fa7b; --green-soft:#8dffb0; --magenta:#ff5fa8; --amber:#ffb86c; --red:#ff5f56; --text:#f5d3e6; --dim:#7a3a5a; --dim2:#5a2a3a; --line:#4a2a3a; --bg:#10080c; --panel:#1a0e14; --panel2:#120a0e; --brand: #ff8ec8; }
  .theme-purple{ --arch:#ae81ff; --arch-soft:#d4b8ff; --green:#50fa7b; --green-soft:#8dffb0; --magenta:#ff5fa8; --amber:#ffb86c; --red:#ff5f56; --text:#e6d3f5; --dim:#5a3a7a; --dim2:#3a2a5a; --line:#3a2a4a; --bg:#0a0810; --panel:#120e1a; --panel2:#0e0a14; --brand: #d4b8ff; }
  .theme-blue{ --arch:#6cb4ff; --arch-soft:#a8d4ff; --green:#50fa7b; --green-soft:#8dffb0; --magenta:#ff5fa8; --amber:#ffb86c; --red:#ff5f56; --text:#d3e5f5; --dim:#3a5a7a; --dim2:#2a3a5a; --line:#2a3a4a; --bg:#080a10; --panel:#0e121a; --panel2:#0a0e14; --brand: #a8d4ff; }
  
  *, *::before, *::after {
    user-select: none !important;
    -webkit-user-select: none !important;
    -moz-user-select: none !important;
    -ms-user-select: none !important;
    -webkit-touch-callout: none !important;
  }
  
  input, textarea, input::selection, textarea::selection {
    user-select: text !important;
    -webkit-user-select: text !important;
    -moz-user-select: text !important;
    -ms-user-select: text !important;
  }
  
  *{box-sizing:border-box; margin:0; padding:0;}
  html,body{background:var(--bg); color:var(--text); font-family:'JetBrains Mono',monospace; -webkit-tap-highlight-color:transparent;}
  body{
    min-height:100vh; padding:16px 14px 40px;
    background-image:
      linear-gradient(rgba(23,147,209,0.035) 1px, transparent 1px),
      linear-gradient(90deg, rgba(23,147,209,0.035) 1px, transparent 1px);
    background-size:24px 24px;
  }
  .crt::before{
    content:""; position:fixed; inset:0; pointer-events:none; z-index:5;
    background:repeating-linear-gradient(0deg, rgba(0,0,0,0.16) 0px, rgba(0,0,0,0.16) 1px, transparent 1px, transparent 3px);
    mix-blend-mode:overlay; opacity:.3;
    pointer-events:none;
  }
  .wrap{max-width:540px; margin:0 auto; position:relative; z-index:6;}
  .bootline{font-size:11px; color:var(--dim); white-space:pre; margin-bottom:0; line-height:1.5;}
  ::selection{background:var(--arch); color:#000;}
  .muted{color:var(--dim); font-size:11px;}

  .lock-screen{
    position:fixed; inset:0; background:var(--bg); z-index:1000; 
    display:flex; align-items:center; justify-content:center; padding:20px;
  }
  .lock-screen.active{ display:flex !important; visibility:visible !important; opacity:1 !important; }
  .lock-screen.hidden{ display:none !important; }
  
  .lock-box{border:1px solid var(--arch); border-radius:2px; padding:24px; width:100%; max-width:340px; background:var(--panel); box-shadow:0 0 30px rgba(94,200,248,0.13);}
  .lock-box input{
    width:100%; background:#050810; border:1px solid var(--line); color:var(--green); padding:10px;
    font-family:inherit; font-size:14px; border-radius:2px; margin:10px 0;
  }
  .lock-box input:focus{outline:none; border-color:var(--arch);}
  .lock-box button{
    width:100%; background:rgba(23,147,209,0.12); border:1px solid var(--arch); color:var(--arch-soft);
    padding:10px; border-radius:2px; font-family:inherit; font-weight:700; cursor:pointer; letter-spacing:0.5px;
  }
  .lock-box button:hover{background:rgba(23,147,209,0.22);}
  .lock-box button:disabled{opacity:0.5; cursor:not-allowed;}

  .neofetch{
    display:flex; gap:18px; align-items:flex-start; border:1px solid var(--line);
    border-radius:2px; padding:16px; background:var(--panel);
    box-shadow:0 0 30px rgba(23,147,209,0.06), inset 0 0 40px rgba(23,147,209,0.02);
  }
  .arch-logo{
    font-size:9px; line-height:1.15; color:var(--arch); white-space:pre; font-weight:700;
    text-shadow:0 0 6px rgba(23,147,209,0.53); flex-shrink:0;
  }
  .nf-info{flex:1; min-width:0;}
  .nf-title{font-size:16px; font-weight:800; letter-spacing:0.5px; margin-bottom:2px;}
  .nf-title .u{color:var(--arch-soft);}
  .nf-title .at{color:var(--dim);}
  .nf-rule{height:1px; background:var(--line); margin:6px 0 8px;}
  .nf-row{display:flex; gap:6px; font-size:11.5px; margin-bottom:3px; align-items:baseline; flex-wrap:wrap;}
  .nf-row .k{color:var(--green-soft); font-weight:700; min-width:64px; flex-shrink:0;}
  .nf-row .v{color:var(--text); word-break:break-word;}
  
  .nf-swatches{ display:flex; gap:3px; margin-top:10px; cursor:pointer; -webkit-tap-highlight-color: transparent !important; }
  .nf-swatches span{
    width:16px; height:10px; border-radius:1px; display:inline-block;
    border: 1px solid transparent; transition: all 0.1s; cursor:pointer;
    -webkit-tap-highlight-color: transparent !important; outline: none !important;
  }
  .nf-swatches span:hover{ transform: scale(1.2); border-color: var(--text); box-shadow: 0 0 12px rgba(23,147,209,0.27); }
  .nf-swatches span.active{ border-color: var(--text); box-shadow: 0 0 12px rgba(23,147,209,0.4); transform: scale(1.1); }
  .nf-swatches span:focus { outline: none !important; }

  .brand {
    padding: 4px 0 4px 0; margin: 2px 0 10px 0; border-bottom: 1px solid var(--line);
    position: relative; min-height: 32px;
  }
  .brand .name {
    font-size: 22px; font-weight: 800; letter-spacing: 4px; color: var(--brand);
    font-family: 'JetBrains Mono', monospace; display: inline-block; min-width: 180px;
    text-shadow: 0 0 10px rgba(102,217,239,0.27);
  }

  .winbar{
    display:flex; align-items:center; justify-content:space-between; padding:7px 10px;
    border:1px solid var(--line); border-bottom:none; border-radius:2px 2px 0 0;
    font-size:11px; color:var(--dim); letter-spacing:0.5px;
  }
  .winbar .path{color:var(--arch-soft);}
  .winbar .path::before{content:"~/"; color:var(--dim);}
  .winbar .wdots span{display:inline-block; width:7px; height:7px; border-radius:50%; margin-left:5px;}
  .win{
    border:1px solid var(--line); border-top:2px solid var(--wc, var(--arch)); border-radius:0 0 2px 2px;
    background:var(--panel2); padding:14px; margin-bottom:16px;
  }
  .win.c-arch{--wc:var(--arch);}
  .win.c-green{--wc:var(--green);}
  .win.c-magenta{--wc:var(--magenta);}
  .win.c-amber{--wc:var(--amber);}

  .status-grid{display:grid; grid-template-columns:1fr 1fr; gap:8px;}
  .stat{border:1px solid var(--line); border-radius:2px; padding:8px 10px; background:rgba(23,147,209,0.03);}
  .stat .k{font-size:9.5px; color:var(--dim); text-transform:uppercase; letter-spacing:1px;}
  .stat .v{font-size:13.5px; color:var(--text); font-weight:700; margin-top:2px;}

  .grid2{display:grid; grid-template-columns:1fr 1fr; gap:8px;}
  .btn{
    background:transparent; border:1px solid var(--line); color:var(--text);
    padding:12px 10px; border-radius:2px; font-family:inherit; font-size:12.5px; font-weight:600;
    cursor:pointer; text-align:left; transition: all 0.1s; position:relative;
    -webkit-tap-highlight-color: transparent !important; outline: none !important;
  }
  .btn::before{content:"$ "; color:var(--dim);}
  .btn:active{transform:scale(.97);}
  .btn:hover{border-color:var(--arch); box-shadow:0 0 10px rgba(23,147,209,0.2); color:var(--arch-soft);}
  .btn-danger{border-color:#4a1f1f; color:#ff9a9a;}
  .btn-danger:hover{border-color:var(--red); box-shadow:0 0 10px rgba(255,95,86,0.27); color:#ff9a9a;}
  .btn-warn{border-color:#4a3d1f; color:var(--amber);}
  .btn-warn:hover{border-color:var(--amber); box-shadow:0 0 10px rgba(255,184,108,0.27);}
  .btn-magenta{border-color:#4a1f3d; color:#ff9ecb;}
  .btn-magenta:hover{border-color:var(--magenta); box-shadow:0 0 10px rgba(255,95,168,0.27);}
  .btn-full{grid-column:1/-1;}

  .slider-box{border:1px solid var(--line); border-radius:2px; padding:10px 12px; margin-top:8px; background:rgba(23,147,209,0.02);}
  .slider-top{display:flex; justify-content:space-between; font-size:12px; margin-bottom:8px; color:var(--dim);}
  input[type=range]{width:100%; accent-color:var(--arch); height:4px;}
  input[type=text], input[type=password], textarea{
    width:100%; background:#050810; border:1px solid var(--line); color:var(--text);
    padding:8px; font-family:inherit; font-size:12.5px; border-radius:2px; margin-bottom:8px;
  }
  input:focus, textarea:focus{outline:none; border-color:var(--arch);}
  label.chk{display:flex; align-items:center; gap:6px; font-size:11px; color:var(--dim); margin:8px 0;}
  label.chk input{width:auto; margin:0;}

  .applist{max-height:200px; overflow-y:auto; margin-top:8px;}
  .appitem{
    display:flex; justify-content:space-between; align-items:center; gap:8px;
    border:1px solid var(--line); border-radius:2px; padding:7px 10px; margin-bottom:6px; font-size:12px;
  }
  .appitem button{
    background:rgba(255,95,86,0.1); border:1px solid #4a1f1f; color:#ff9a9a;
    border-radius:2px; padding:4px 8px; cursor:pointer; font-family:inherit; font-size:11px;
  }

  .sesslist{max-height:220px; overflow-y:auto; margin-top:8px;}
  .sessitem{
    border:1px solid var(--line); border-radius:2px; padding:8px 10px; margin-bottom:6px; font-size:11.5px;
  }
  .sessitem.you{border-color:var(--green); background:rgba(80,250,123,0.04);}
  .sessitem .row1{display:flex; justify-content:space-between; align-items:center; gap:8px;}
  .sessitem .lbl{color:var(--text); font-weight:600;}
  .sessitem .lbl .tag{color:var(--green-soft); font-size:9.5px; margin-left:6px; border:1px solid var(--green); border-radius:2px; padding:1px 5px;}
  .sessitem .meta{color:var(--dim); font-size:10px; margin-top:4px;}
  .sessitem button{
    background:rgba(255,95,86,0.1); border:1px solid #4a1f1f; color:#ff9a9a;
    border-radius:2px; padding:4px 8px; cursor:pointer; font-family:inherit; font-size:10.5px; flex-shrink:0;
  }

  .urlbox{border:1px dashed var(--arch-soft); border-radius:2px; padding:10px 12px; font-size:11.5px; word-break:break-all; margin-top:10px;}

  .toast{
    position:fixed; left:50%; bottom:22px; transform:translateX(-50%);
    background:#0a1620; border:1px solid var(--arch); color:var(--arch-soft);
    padding:10px 16px; border-radius:2px; font-size:12px; display:none; z-index:9999;
    box-shadow:0 0 20px rgba(23,147,209,0.27);
  }
  .toast.show{display:block; animation:up .25s ease;}
  @keyframes up{from{opacity:0; transform:translate(-50%,10px);} to{opacity:1; transform:translate(-50%,0);}}

  .eye-btn{
    background:none; border:1px solid var(--line); color:var(--dim); font-size:10px;
    padding:2px 7px; border-radius:2px; cursor:pointer; font-family:inherit; letter-spacing:0.5px;
    -webkit-tap-highlight-color: transparent !important; outline: none !important;
  }
  .eye-btn:hover{color:var(--arch-soft); border-color:var(--arch);}
  .prompt-tail{color:var(--dim); font-size:12px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:12px;}
  
  #mainUI { display: none; }
  #mainUI.visible { display: block; }
</style>
</head>
<body class="crt">

<div id="lockScreen" class="lock-screen active">
  <div class="lock-box">
    <div class="bootline">[lax@lax ~]$ sudo authenticate --token</div>
    <div class="muted" style="margin-bottom:8px;">Enter the access token printed in controller.log on first run.</div>
    <input id="tokenInput" type="password" placeholder="access token" autocomplete="off" onkeydown="if(event.key==='Enter')doLogin()">
    <button id="loginBtn" onclick="doLogin()">[ unlock session ]</button>
    <div id="loginError" style="color:var(--red); font-size:11px; margin-top:8px; min-height:18px;"></div>
  </div>
</div>

<div class="wrap" id="mainUI">
  <div class="bootline" id="bootline"></div>

  <div class="brand">
    <span class="name" id="glitchName">pcpuppet</span>
  </div>

  <div class="neofetch">
    <pre class="arch-logo">      /\
     /  \
    /\  /\
   /  __  \
  /  (  )  \
 /_-''  ''-_\
/_-'      '-_\</pre>
    <div class="nf-info">
      <div class="nf-title"><span class="u">lax</span><span class="at">@</span><span class="u">lax</span></div>
      <div class="nf-rule"></div>
      <div class="nf-row"><span class="k">os</span><span class="v" id="systemInfo">--</span></div>
      <div class="nf-row"><span class="k">host</span><span id="hostName">••••••••</span>
        <button class="eye-btn" id="privacyToggle" onclick="togglePrivacy()">show</button></div>
      <div class="nf-row"><span class="k">cpu</span><span class="v" id="cpuUsage">--</span></div>
      <div class="nf-row"><span class="k">memory</span><span class="v" id="memoryUsage">--</span></div>
      <div class="nf-row"><span class="k">battery</span><span class="v" id="batteryStatus">--</span></div>
      <div class="nf-swatches" id="themeSwatches">
        <span style="background:#66d9ef" data-theme="cyan" onclick="setTheme('cyan')" class="active"></span>
        <span style="background:#50fa7b" data-theme="green" onclick="setTheme('green')"></span>
        <span style="background:#ffd93d" data-theme="gold" onclick="setTheme('gold')"></span>
        <span style="background:#ff8a82" data-theme="red" onclick="setTheme('red')"></span>
        <span style="background:#ff8ec8" data-theme="magenta" onclick="setTheme('magenta')"></span>
        <span style="background:#d4b8ff" data-theme="purple" onclick="setTheme('purple')"></span>
        <span style="background:#a8d4ff" data-theme="blue" onclick="setTheme('blue')"></span>
      </div>
    </div>
  </div>

  <div class="urlbox" id="publicUrl">••••••••••••••••••••••••••••••••••</div>

  <div class="prompt-tail">[lax@lax ~]$ tail -f /var/log/session</div>

  <div style="margin-top:16px;">

  <div class="winbar c-arch"><span class="path">power</span><div class="wdots"><span style="background:var(--red)"></span><span style="background:var(--amber)"></span><span style="background:var(--green)"></span></div></div>
  <div class="win c-arch">
    <div class="status-grid" style="margin-bottom:12px;">
      <div class="stat"><div class="k">brightness</div><div class="v" id="brightnessValue">--%</div></div>
      <div class="stat"><div class="k">state</div><div class="v">online</div></div>
    </div>
    <div class="grid2">
      <button class="btn btn-danger" onclick="sendCommand('lock')">lock</button>
      <button class="btn btn-warn" onclick="sendCommand('sleep')">sleep</button>
      <button class="btn btn-danger" onclick="sendCommand('shutdown')">shutdown -t5</button>
      <button class="btn btn-warn" onclick="sendCommand('restart')">restart -t5</button>
      <button class="btn btn-full" onclick="sendCommand('cancel_shutdown')">cancel pending shutdown/restart</button>
    </div>
    <div class="slider-box">
      <div class="slider-top"><span>brightness</span><span></span></div>
      <input type="range" id="brightnessSlider" min="0" max="100" value="50" oninput="updateBrightness(this.value)">
    </div>
    <div class="grid2" style="margin-top:8px;">
      <button class="btn" onclick="sendCommand('volume_up')">vol +</button>
      <button class="btn" onclick="sendCommand('volume_down')">vol -</button>
      <button class="btn btn-full" onclick="sendCommand('volume_mute')">mute</button>
    </div>
  </div>

  <div class="winbar c-magenta"><span class="path">message</span><div class="wdots"><span style="background:var(--red)"></span><span style="background:var(--amber)"></span><span style="background:var(--green)"></span></div></div>
  <div class="win c-magenta">
    <input id="msgTitle" type="text" placeholder="title (optional)">
    <textarea id="msgText" rows="2" placeholder="message to pop up on screen..."></textarea>
    <label class="chk"><input type="checkbox" id="msgSpeak"> also read it out loud</label>
    <div class="grid2">
      <button class="btn" onclick="quickMsg('System error 0x0000dead. Please restart immediately.')">preset: fake error</button>
      <button class="btn" onclick="quickMsg('This PC is now under my control.')">preset: spooky</button>
      <button class="btn btn-full btn-magenta" onclick="sendMessage()">send message</button>
    </div>
  </div>

  <div class="winbar c-green"><span class="path">clipboard</span><div class="wdots"><span style="background:var(--red)"></span><span style="background:var(--amber)"></span><span style="background:var(--green)"></span></div></div>
  <div class="win c-green">
    <textarea id="clipText" rows="3" placeholder="text to sync..."></textarea>
    <div class="grid2">
      <button class="btn" onclick="pullClipboard()">pull from PC</button>
      <button class="btn" onclick="pushClipboard()">push to PC</button>
    </div>
  </div>

  <div class="winbar c-amber"><span class="path">screenshot</span><div class="wdots"><span style="background:var(--red)"></span><span style="background:var(--amber)"></span><span style="background:var(--green)"></span></div></div>
  <div class="win c-amber">
    <button class="btn btn-full" onclick="takeScreenshot()">take screenshot</button>
    <div id="captureBox" style="margin-top:8px; display:none;">
      <img id="captureImg" style="width:100%; border:1px solid var(--line); border-radius:2px; margin-bottom:8px;">
      <button class="btn btn-danger btn-full" onclick="deleteScreenshot()">delete this screenshot</button>
    </div>
  </div>

  <div class="winbar c-arch"><span class="path">processes</span><div class="wdots"><span style="background:var(--red)"></span><span style="background:var(--amber)"></span><span style="background:var(--green)"></span></div></div>
  <div class="win c-arch">
    <button class="btn btn-full" onclick="getOpenApps()" style="margin-bottom:8px;">refresh window list</button>
    <div class="applist" id="appList"><div class="muted">run refresh to list open windows</div></div>
  </div>

  <!-- ===== SECURITY SECTION WITH PASSWORD ===== -->
    <div class="winbar c-magenta"><span class="path">security</span><div class="wdots"><span style="background:var(--red)"></span><span style="background:var(--amber)"></span><span style="background:var(--green)"></span></div></div>
  <div class="win c-magenta">
    <div id="securityLockText" class="muted" style="margin-bottom:8px;">🖕 security section locked....enter password to access</div>
    
    <div id="securityLockBox" style="display:flex; gap:6px; margin-bottom:10px;">
      <input id="securityPassword" type="password" placeholder="security password" style="flex:1; margin:0;" onkeydown="if(event.key==='Enter')unlockSecurity()">
      <button class="btn" onclick="unlockSecurity()" style="padding:8px 14px; flex-shrink:0;">unlock</button>
    </div>
    <div id="securityError" style="color:var(--red); font-size:10px; min-height:16px; margin-bottom:6px;"></div>
    
    <div id="securityContent" style="display:none; border-top:1px solid var(--line); padding-top:10px;">
      <div class="muted" style="margin-bottom:6px;">change authentication token or manage logged-in devices</div>
      <input id="newTokenInput" type="text" placeholder="new token (min 4 characters)">
      <div class="grid2">
        <button class="btn" onclick="changeToken()">rotate token</button>
        <button class="btn btn-danger" onclick="logoutSession()">end this session</button>
      </div>
      <div class="muted" style="margin:10px 0 4px;">devices currently logged in:</div>
      <button class="btn btn-full" onclick="loadSessions()" style="margin-bottom:4px;">refresh device list</button>
      <div class="sesslist" id="sessList"><div class="muted">tap refresh to load</div></div>
    </div>
  </div>

  <div class="muted" style="margin-top:6px; text-align:center;">[lax@lax ~]$ session auto-refreshes · ctrl+d to disconnect</div>
  </div>
</div>

<div class="toast" id="notification"></div>

<script>
const glitchChars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789!@#$%^&*()_+-=[]{}|;:,.<>?/~`';
const targetText = 'pcpuppet';
const glitchEl = document.getElementById('glitchName');
let glitchInterval = null;
let glitchTimeout = null;

function casinoGlitch() {
  if (glitchTimeout) clearTimeout(glitchTimeout);
  const length = targetText.length;
  let progress = 0;
  const steps = 12;
  function doGlitchStep() {
    if (progress >= steps) { glitchEl.textContent = targetText; return; }
    let result = '';
    const charsToChange = Math.max(1, Math.floor((1 - progress/steps) * length));
    const startPos = Math.floor(Math.random() * (length - charsToChange + 1));
    for (let i = 0; i < length; i++) {
      if (i >= startPos && i < startPos + charsToChange) {
        result += glitchChars[Math.floor(Math.random() * glitchChars.length)];
      } else { result += targetText[i]; }
    }
    glitchEl.textContent = result;
    progress++;
    const delay = 50 + (progress / steps) * 30;
    glitchTimeout = setTimeout(doGlitchStep, delay);
  }
  doGlitchStep();
}

function startGlitchLoop() {
  if (glitchInterval) clearInterval(glitchInterval);
  glitchInterval = setInterval(casinoGlitch, 3500);
  setTimeout(casinoGlitch, 300);
}

function setTheme(theme) {
  const body = document.body;
  const classList = body.className.split(' ');
  const newClasses = classList.filter(c => !c.startsWith('theme-'));
  newClasses.push('theme-' + theme);
  body.className = newClasses.join(' ');
  const swatches = document.querySelectorAll('.nf-swatches span');
  for (let i = 0; i < swatches.length; i++) {
    const el = swatches[i];
    if (el.dataset.theme === theme) { el.classList.add('active'); } else { el.classList.remove('active'); }
  }
  localStorage.setItem('lax_theme', theme);
  void body.offsetHeight;
}
const savedTheme = localStorage.getItem('lax_theme') || 'cyan';
setTheme(savedTheme);

// ============================================================
// SECURITY SECTION PASSWORD PROTECTION
// ============================================================
function unlockSecurity() {
    const input = document.getElementById('securityPassword');
    const error = document.getElementById('securityError');
    const content = document.getElementById('securityContent');
    const lockBox = document.getElementById('securityLockBox');
    const lockText = document.getElementById('securityLockText');
    const password = input.value.trim();
    
    if (!password) {
        error.textContent = '[err] enter password';
        return;
    }
    
    api('/api/security/unlock', {
        method: 'POST',
        body: JSON.stringify({password: password})
    })
    .then(data => {
        if (data.status === 'success') {
            content.style.display = 'block';
            lockBox.style.display = 'none';
            lockText.style.display = 'none';
            error.textContent = '';
            input.value = '';
            showNotification('[ok] security section unlocked');
            loadSessions();
        } else {
            error.textContent = '[err] wrong password';
            input.value = '';
            input.focus();
            showNotification('[err] wrong security password');
        }
    })
    .catch(() => {
        error.textContent = '[err] connection failed';
        showNotification('[err] could not verify password');
    });
}

// Allow Enter key on password field
document.addEventListener('DOMContentLoaded', function() {
    const input = document.getElementById('securityPassword');
    if (input) {
        input.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') unlockSecurity();
        });
    }
});

function showLock() {
  const lockScreen = document.getElementById('lockScreen');
  const mainUI = document.getElementById('mainUI');
  const loginBtn = document.getElementById('loginBtn');
  const errorEl = document.getElementById('loginError');
  if (errorEl) errorEl.textContent = '';
  if (loginBtn) loginBtn.disabled = false;
  document.getElementById('tokenInput').value = '';
  lockScreen.classList.remove('hidden');
  lockScreen.classList.add('active');
  mainUI.classList.remove('visible');
  mainUI.style.display = 'none';
  if (glitchInterval) clearInterval(glitchInterval);
  if (glitchTimeout) clearTimeout(glitchTimeout);
}

function hideLock() {
  const lockScreen = document.getElementById('lockScreen');
  const mainUI = document.getElementById('mainUI');
  lockScreen.classList.add('hidden');
  lockScreen.classList.remove('active');
  mainUI.style.display = 'block';
  mainUI.classList.add('visible');
  startGlitchLoop();
}

// ---------- SESSION-BASED AUTH ----------
let TOKEN = localStorage.getItem('lc_session') || '';
let HANDLE = localStorage.getItem('lc_handle') || '';
const urlToken = new URLSearchParams(window.location.search).get('token');

function deviceLabel(){
  const p = navigator.platform || 'device';
  const ua = navigator.userAgent || '';
  let browser = 'browser';
  if (ua.includes('Chrome')) browser = 'Chrome';
  else if (ua.includes('Firefox')) browser = 'Firefox';
  else if (ua.includes('Safari')) browser = 'Safari';
  return `${p} · ${browser}`;
}

function doLogin() {
  const t = document.getElementById('tokenInput').value.trim();
  const errorEl = document.getElementById('loginError');
  const loginBtn = document.getElementById('loginBtn');
  if (!t) { if (errorEl) errorEl.textContent = '[err] token cannot be empty'; return; }
  if (loginBtn) loginBtn.disabled = true;
  if (errorEl) errorEl.textContent = '';

  fetch('/api/login', {
    method: 'POST',
    headers: {'Content-Type':'application/json', 'ngrok-skip-browser-warning':'true'},
    body: JSON.stringify({password: t, label: deviceLabel()})
  })
    .then(r => r.json().then(d => ({status: r.status, body: d})))
    .then(({status, body}) => {
      if (status !== 200 || body.status !== 'success') {
        if (errorEl) errorEl.textContent = '[err] invalid token';
        if (loginBtn) loginBtn.disabled = false;
        return;
      }
      TOKEN = body.session;
      HANDLE = body.handle;
      localStorage.setItem('lc_session', TOKEN);
      localStorage.setItem('lc_handle', HANDLE);
      hideLock();
      bootSequence();
    })
    .catch(() => {
      if (errorEl) errorEl.textContent = '[err] connection refused';
      if (loginBtn) loginBtn.disabled = false;
    });
}

function api(path, opts={}) {
  opts.headers = Object.assign({'X-Auth-Token': TOKEN, 'Content-Type':'application/json', 'ngrok-skip-browser-warning':'true'}, opts.headers||{});
  return fetch(path, opts).then(r => {
    if (r.status === 401) { showLock(); throw new Error('unauthorized'); }
    return r.json();
  });
}

function attemptResume(){
  fetch('/api/status', {headers: {'X-Auth-Token': TOKEN, 'ngrok-skip-browser-warning':'true'}})
    .then(r => {
      if (r.status === 401) { TOKEN=''; HANDLE=''; localStorage.removeItem('lc_session'); localStorage.removeItem('lc_handle'); showLock(); return; }
      hideLock();
      bootSequence();
    })
    .catch(() => showLock());
}

function bootSequence(){
  const lines = ['[ok] lax session key accepted','[ok] establishing control channel','[ok] link online','[ok] whoami: lax'];
  const el = document.getElementById('bootline');
  el.textContent = '';
  lines.forEach((l,i)=> setTimeout(()=>{ el.textContent += l + '\n'; }, i*160));
  refreshAll();
  setInterval(getSystemStatus, 15000);
  setInterval(getOpenApps, 30000);
}

let privacyRevealed = false;
let realHostName = '';
let realPublicUrl = '';

function refreshAll(){
  getSystemStatus(); getOpenApps(); getBrightness(); getPublicUrl(); loadSessions();
}

function togglePrivacy(){
  privacyRevealed = !privacyRevealed;
  document.getElementById('hostName').textContent = privacyRevealed ? (realHostName || '--') : '••••••••';
  document.getElementById('publicUrl').textContent = privacyRevealed ? (realPublicUrl || '--') : '••••••••••••••••••••••••••••••••••';
  document.getElementById('privacyToggle').textContent = privacyRevealed ? 'hide' : 'show';
}

function changeToken(){
  const newToken = document.getElementById('newTokenInput').value.trim();
  if (!newToken) { showNotification('[err] token cannot be empty'); return; }
  if (newToken.length < 4) { showNotification('[err] token must be at least 4 characters'); return; }
  api('/api/set_token', {method:'POST', body: JSON.stringify({new_token: newToken})})
    .then(d => {
      if (d.status === 'success') {
        document.getElementById('newTokenInput').value = '';
        showNotification('[ok] master token rotated — existing device sessions stay logged in');
      } else {
        showNotification('[err] ' + (d.message || 'failed'));
      }
    })
    .catch(() => showNotification('[err] could not reach server'));
}

function logoutSession(){
  api('/api/sessions/revoke', {method:'POST', body: JSON.stringify({handle: HANDLE})})
    .then(() => {
      localStorage.removeItem('lc_session');
      localStorage.removeItem('lc_handle');
      TOKEN = ''; HANDLE = '';
      showLock();
      showNotification('[ok] session ended');
    })
    .catch(() => {
      localStorage.removeItem('lc_session');
      localStorage.removeItem('lc_handle');
      TOKEN = ''; HANDLE = '';
      showLock();
    });
}

function loadSessions(){
  api('/api/sessions').then(d => {
    const list = document.getElementById('sessList');
    if (!d.sessions || !d.sessions.length) { list.innerHTML = '<div class="muted">none</div>'; return; }
    list.innerHTML = d.sessions.map(s => `
      <div class="sessitem ${s.is_you ? 'you' : ''}">
        <div class="row1">
          <span class="lbl">${escapeHtml(s.label)}${s.is_you ? '<span class="tag">this device</span>' : ''}</span>
          <button onclick="revokeDevice('${s.handle}')">revoke</button>
        </div>
        <div class="meta">logged in ${escapeHtml(s.created)} · last seen ${escapeHtml(s.last_seen)}</div>
      </div>
    `).join('');
  }).catch(()=>{});
}

function revokeDevice(handle){
  api('/api/sessions/revoke', {method:'POST', body: JSON.stringify({handle})})
    .then(d => {
      if (handle === HANDLE) {
        localStorage.removeItem('lc_session'); localStorage.removeItem('lc_handle');
        TOKEN=''; HANDLE='';
        showLock();
        showNotification('[ok] this device logged out');
        return;
      }
      showNotification(d.status === 'success' ? '[ok] device revoked' : `[err] ${d.message||'failed'}`);
      loadSessions();
    })
    .catch(() => showNotification('[err] could not reach server'));
}

function getPublicUrl(){
  api('/api/public_url').then(d => {
    realPublicUrl = d.url || '';
    if (privacyRevealed) document.getElementById('publicUrl').textContent = realPublicUrl;
  }).catch(()=>{});
}

function getSystemStatus(){
  api('/api/status').then(d => {
    document.getElementById('systemInfo').textContent = d.system || 'unknown';
    document.getElementById('batteryStatus').textContent = d.battery || 'n/a';
    document.getElementById('cpuUsage').textContent = (d.cpu ?? '--') + '%';
    document.getElementById('memoryUsage').textContent = (d.memory ?? '--') + '%';
    realHostName = d.hostname || 'host';
    if (privacyRevealed) document.getElementById('hostName').textContent = realHostName;
  }).catch(()=>{});
}

function getOpenApps(){
  api('/api/apps').then(d => {
    const list = document.getElementById('appList');
    if (d.apps && d.apps.length){
      list.innerHTML = d.apps.map(a => `
        <div class="appitem"><span>${escapeHtml(a)}</span>
        <button onclick="sendCommand('close_app', '${escapeHtml(a).replace(/'/g,"\\'")}')">kill</button></div>
      `).join('');
    } else {
      list.innerHTML = '<div class="muted">no windows detected</div>';
    }
  }).catch(()=>{});
}

function escapeHtml(t){ const d = document.createElement('div'); d.textContent = t; return d.innerHTML; }

function getBrightness(){
  api('/api/brightness').then(d => {
    document.getElementById('brightnessSlider').value = d.brightness;
    document.getElementById('brightnessValue').textContent = d.brightness + '%';
  }).catch(()=>{});
}

function updateBrightness(v){
  document.getElementById('brightnessValue').textContent = v + '%';
  api('/api/brightness', {method:'POST', body: JSON.stringify({brightness: parseInt(v)})})
    .then(d => { if (d.status === 'success') showNotification('[ok] brightness set'); }).catch(()=>{});
}

function sendCommand(command, param=null){
  const body = {command}; if (param) body.param = param;
  api('/api/command', {method:'POST', body: JSON.stringify(body)})
    .then(d => showNotification(d.status === 'success' ? `[ok] ${command}` : `[err] ${d.message||'failed'}`))
    .catch(()=>{});
}

function quickMsg(text){ document.getElementById('msgText').value = text; }

function sendMessage(){
  const text = document.getElementById('msgText').value.trim();
  const title = document.getElementById('msgTitle').value.trim();
  const speak = document.getElementById('msgSpeak').checked;
  if (!text) { showNotification('[err] type a message first'); return; }
  api('/api/command', {method:'POST', body: JSON.stringify({command:'message', message:text, title, speak})})
    .then(d => showNotification(d.status === 'success' ? '[ok] message sent to screen' : `[err] ${d.message||'failed'}`))
    .catch(()=>{});
}

function pullClipboard(){
  showNotification('[ok] pulling...');
  api('/api/clipboard').then(d => {
    if (d.status === 'error') { showNotification(`[err] ${d.message || 'clipboard unavailable'}`); return; }
    document.getElementById('clipText').value = d.text || '';
    showNotification('[ok] pulled PC clipboard');
  }).catch(() => showNotification('[err] could not reach server'));
}

function pushClipboard(){
  const text = document.getElementById('clipText').value;
  api('/api/clipboard', {method:'POST', body: JSON.stringify({text})})
    .then(d => showNotification(d.status === 'success' ? '[ok] pushed to PC clipboard' : `[err] ${d.message || 'failed'}`))
    .catch(() => showNotification('[err] could not reach server'));
}

let lastCaptureUrl = null;

function takeScreenshot(){
    showNotification('[ok] capturing...');
    fetch('/api/screenshot?token=' + encodeURIComponent(TOKEN), {headers: {'ngrok-skip-browser-warning':'true'}})
    .then(r => { if (!r.ok) throw new Error('failed'); return r.blob(); })
    .then(blob => {
        if (lastCaptureUrl) URL.revokeObjectURL(lastCaptureUrl);
        lastCaptureUrl = URL.createObjectURL(blob);
        const img = document.getElementById('captureImg');
        img.src = lastCaptureUrl;
        document.getElementById('captureBox').style.display = 'block';
        showNotification('[ok] screenshot ready');
    })
    .catch(() => { showNotification('[err] capture failed'); });
}

function deleteScreenshot(){
  if (lastCaptureUrl) { URL.revokeObjectURL(lastCaptureUrl); lastCaptureUrl = null; }
  document.getElementById('captureImg').src = '';
  document.getElementById('captureBox').style.display = 'none';
  showNotification('[ok] screenshot deleted');
}

let toastTimer = null;
function showNotification(msg){
  const n = document.getElementById('notification');
  n.textContent = msg;
  n.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(()=> n.classList.remove('show'), 2000);
}

if (TOKEN) {
  attemptResume();
} else if (urlToken) {
  document.getElementById('tokenInput').value = urlToken;
  doLogin();
}
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 6. Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


@app.route("/api/set_token", methods=["POST"])
def set_token():
    global AUTH_TOKEN
    data = request.get_json(silent=True) or {}
    new_token = (data.get("new_token") or "").strip()
    if not new_token or len(new_token) < 4:
        return jsonify({"status": "error", "message": "token must be at least 4 characters"}), 400
    CONFIG["token"] = new_token
    if not save_config():
        return jsonify({"status": "error", "message": "could not save new token to disk"}), 500
    AUTH_TOKEN = new_token
    logger.info("access token changed by user")
    return jsonify({"status": "success", "message": "token updated"})


@app.route("/api/public_url")
def get_public_url():
    try:
        response = requests.get("http://localhost:4040/api/tunnels", timeout=2)
        data = response.json()
        if data.get("tunnels"):
            return jsonify({"url": data["tunnels"][0]["public_url"]})
    except Exception:
        pass
    return jsonify({"url": f"http://{get_local_ip()}:{PORT} (local network only)"})


@app.route("/api/status")
def get_status():
    try:
        system = f"{platform.system()} {platform.release()}"
        battery = psutil.sensors_battery()
        battery_status = (
            f"{battery.percent}% {'plugged' if battery.power_plugged else 'battery'}"
            if battery else "no battery"
        )
        cpu = psutil.cpu_percent(interval=0.3)
        memory_percent = psutil.virtual_memory().percent
        return jsonify({
            "system": system,
            "hostname": platform.node(),
            "battery": battery_status,
            "cpu": cpu,
            "memory": memory_percent,
        })
    except Exception as e:
        logger.exception("status failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/apps")
def get_apps():
    if gw is None:
        return jsonify({"apps": []})
    try:
        titles = gw.getAllTitles()
        seen, apps = set(), []
        blocked = ("start", "taskbar", "system", "settings", "program manager")
        for t in titles:
            if not t or len(t) <= 1:
                continue
            low = t.lower()
            if any(b in low for b in blocked):
                continue
            if t in seen:
                continue
            seen.add(t)
            apps.append(t)
        return jsonify({"apps": apps[:20]})
    except Exception as e:
        logger.exception("get_apps failed")
        return jsonify({"apps": [], "error": str(e)})


@app.route("/api/brightness", methods=["GET", "POST"])
def handle_brightness():
    if sbc is None:
        return jsonify({"brightness": 50, "error": "brightness control unavailable"}), 200
    try:
        if request.method == "GET":
            current = sbc.get_brightness()
            brightness = int(current[0]) if current else 50
            return jsonify({"brightness": brightness})
        data = request.get_json(silent=True) or {}
        brightness = max(0, min(100, int(data.get("brightness", 50))))
        sbc.set_brightness(brightness)
        return jsonify({"status": "success", "brightness": brightness})
    except Exception as e:
        logger.exception("brightness failed")
        return jsonify({"error": str(e)}), 500


@app.route("/api/clipboard", methods=["GET", "POST"])
def handle_clipboard():
    if win32clipboard is None:
        msg = "clipboard sync unavailable"
        if request.method == "GET":
            return jsonify({"text": "", "status": "error", "message": msg})
        return jsonify({"status": "error", "message": msg}), 503
    if request.method == "GET":
        return jsonify({"text": get_clipboard_text() or "", "status": "success"})
    data = request.get_json(silent=True) or {}
    text = data.get("text", "")
    ok = set_clipboard_text(text)
    if ok:
        return jsonify({"status": "success"})
    return jsonify({"status": "error", "message": "could not write to PC clipboard"}), 500


@app.route("/api/screenshot")
def handle_screenshot():
    try:
        from flask import send_file
        buf = take_screenshot_bytes()
        return send_file(buf, mimetype="image/jpeg")
    except Exception as e:
        logger.exception("screenshot failed")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/command", methods=["POST"])
def execute_command():
    try:
        data = request.get_json(silent=True) or {}
        command = data.get("command")
        param = data.get("param")

        if command == "lock":
            ctypes.windll.user32.LockWorkStation()

        elif command == "sleep":
            subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)

        elif command == "shutdown":
            subprocess.Popen("shutdown /s /t 5", shell=True)
            return jsonify({"status": "success", "message": "shutting down in 5s"})

        elif command == "restart":
            subprocess.Popen("shutdown /r /t 5", shell=True)
            return jsonify({"status": "success", "message": "restarting in 5s"})

        elif command == "cancel_shutdown":
            subprocess.Popen("shutdown /a", shell=True)
            return jsonify({"status": "success", "message": "pending shutdown/restart cancelled"})

        elif command == "volume_up":
            if not set_volume(min(100, get_volume() + 10)):
                nudge_volume(1)

        elif command == "volume_down":
            if not set_volume(max(0, get_volume() - 10)):
                nudge_volume(-1)

        elif command == "volume_mute":
            ctypes.windll.user32.keybd_event(0xAD, 0, 0, 0)

        elif command == "message":
            text = (param or data.get("message") or "").strip()
            if not text:
                return jsonify({"status": "error", "message": "no message text"}), 400
            title = (data.get("title") or "System Notice").strip()
            show_popup_message(title, text)
            if data.get("speak"):
                speak_message(text)
            return jsonify({"status": "success", "message": "message sent"})

        elif command == "close_app":
            if not param:
                return jsonify({"status": "error", "message": "no window specified"}), 400
            if gw is None:
                return jsonify({"status": "error", "message": "window control unavailable"}), 500
            windows = gw.getWindowsWithTitle(param)
            if not windows:
                return jsonify({"status": "error", "message": "window not found"}), 404
            try:
                windows[0].close()
                return jsonify({"status": "success", "message": f"closed {param}"})
            except Exception as e:
                logger.warning(f"close_app fallback for {param}: {e}")
                return jsonify({"status": "error", "message": "could not close window"}), 500
        else:
            return jsonify({"status": "error", "message": "unknown command"}), 400

        return jsonify({"status": "success"})
    except Exception as e:
        logger.exception("command failed")
        return jsonify({"status": "error", "message": str(e)}), 500


# ---------------------------------------------------------------------------
# 7. ngrok + networking helpers
# ---------------------------------------------------------------------------
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def find_ngrok():
    import shutil
    env_path = os.environ.get("NGROK_PATH")
    if env_path and os.path.exists(env_path):
        return env_path
    which = shutil.which("ngrok")
    if which:
        return which
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ngrok", "ngrok.exe"),
        os.path.join(os.environ.get("USERPROFILE", ""), "ngrok.exe"),
        r"C:\ngrok\ngrok.exe",
    ]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


@safe(default=None, log_name="start_ngrok")
def start_ngrok():
    ngrok_path = find_ngrok()
    if not ngrok_path:
        logger.warning("ngrok not found (set NGROK_PATH env var, or add it to PATH)")
        return None

    result = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq ngrok.exe"], capture_output=True, text=True
    )
    if "ngrok.exe" not in result.stdout:
        subprocess.Popen(
            [ngrok_path, "http", str(PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        time.sleep(3)

    response = requests.get("http://localhost:4040/api/tunnels", timeout=3)
    data = response.json()
    if data.get("tunnels"):
        return data["tunnels"][0]["public_url"]
    return None


# ---------------------------------------------------------------------------
# 8. Flask server thread
# ---------------------------------------------------------------------------
def run_server():
    try:
        from waitress import serve
        logger.info(f"starting waitress on 0.0.0.0:{PORT}")
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        logger.warning("waitress not installed, falling back to Flask dev server")
        app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True, use_reloader=False)
    except Exception:
        logger.exception("server crashed")


# ---------------------------------------------------------------------------
# 9. System tray
# ---------------------------------------------------------------------------
def make_icon_image():
    from PIL import Image, ImageDraw, ImageFont
    size = 64
    img = Image.new("RGBA", (size, size), (5, 8, 6, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([2, 2, size - 3, size - 3], outline=(57, 255, 136, 255), width=3)
    try:
        font = ImageFont.truetype("consola.ttf", 28)
    except Exception:
        font = ImageFont.load_default()
    d.text((10, 16), ">_", font=font, fill=(57, 255, 136, 255))
    return img


def run_tray():
    try:
        import pystray
    except Exception:
        logger.warning("pystray not installed; running headless with no tray icon")
        while True:
            time.sleep(3600)
        return

    def open_dashboard(icon, item):
        webbrowser.open(f"http://127.0.0.1:{PORT}/?token={AUTH_TOKEN}")

    def copy_token(icon, item):
        try:
            import subprocess as sp
            sp.run("clip", input=AUTH_TOKEN.encode(), shell=True)
            icon.notify("Access token copied to clipboard")
        except Exception:
            pass

    def restart_ngrok(icon, item):
        threading.Thread(target=start_ngrok, daemon=True).start()
        icon.notify("Restarting ngrok tunnel...")

    def do_exit(icon, item):
        logger.info("exit requested from tray")
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", open_dashboard, default=True),
        pystray.MenuItem("Copy Access Token", copy_token),
        pystray.MenuItem("Restart ngrok Tunnel", restart_ngrok),
        pystray.MenuItem("Exit", do_exit),
    )
    icon = pystray.Icon("lax", make_icon_image(), "Lax", menu)
    icon.run()


# ---------------------------------------------------------------------------
# 10. Entry point
# ---------------------------------------------------------------------------
def is_running_headless():
    try:
        return os.path.basename(sys.executable).lower() == "pythonw.exe"
    except Exception:
        return False


def main():
    logger.info("=" * 60)
    logger.info("Lax starting")
    logger.info(f"Access token: {AUTH_TOKEN}  (also saved in {CONFIG_FILE})")
    logger.info(f"Local URL: http://{get_local_ip()}:{PORT}")
    time.sleep(8)

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    def ngrok_startup():
        time.sleep(1)
        url = start_ngrok()
        if url:
            logger.info(f"Public URL: {url}")
        else:
            logger.info("No ngrok tunnel active; local network only")

    threading.Thread(target=ngrok_startup, daemon=True).start()

    time.sleep(1.5)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception("FATAL: app crashed during startup/run")
        try:
            with open(os.path.join(APP_DIR, "CRASHED.txt"), "w", encoding="utf-8") as f:
                f.write("The app crashed. See controller.log in this same folder for details.\n")
        except Exception:
            pass
