"""
Lax
=============================================
Control your Windows laptop from your phone via a Flask web UI + Ngrok..

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


# ---------------------------------------------------------------------------
# 4. Volume helper
# ---------------------------------------------------------------------------
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
    """Fallback for machines without pycaw: simulate media keys."""
    VK_VOLUME_UP, VK_VOLUME_DOWN = 0xAF, 0xAE
    key = VK_VOLUME_UP if direction > 0 else VK_VOLUME_DOWN
    for _ in range(5):
        ctypes.windll.user32.keybd_event(key, 0, 0, 0)
        time.sleep(0.05)


# ---------------------------------------------------------------------------
# 5. Custom on screen message (popup + optional text to speech)
# ---------------------------------------------------------------------------
MB_ICONINFORMATION = 0x40
MB_SYSTEMMODAL = 0x1000  # keeps the box on top of whatever's currently open


def show_popup_message(title, message):
    """Show a Windows message box on the physical screen. Runs in its own
    thread since MessageBoxW blocks until someone clicks OK, and we don't
    want that to block the Flask request."""
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
    """Read the message aloud using Windows' built-in text-to-speech
    (System.Speech via PowerShell). Passed through an environment variable
    rather than embedded in the command string, so odd characters/quotes in
    the message can't break the PowerShell invocation."""
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


# ---------------------------------------------------------------------------
# 6. Clipboard sync (both directions)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# 7. Screenshot
# ---------------------------------------------------------------------------
def take_screenshot_bytes(max_width=1280):
    from PIL import ImageGrab, Image
    import io
    img = ImageGrab.grab()
    if img.mode != "RGB":
        img = img.convert("RGB")
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, int(img.height * ratio)), resample=Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=65, optimize=False)
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# 8. HTML
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
    --bg:#080b10; --panel:#0e131b; --panel2:#0b0f15; --line:#1c2735;
    --arch:#1793d1; --arch-soft:#5ec8f8; --green:#50fa7b; --green-soft:#8dffb0;
    --magenta:#ff5fa8; --amber:#ffb86c; --red:#ff5f56;
    --text:#cfd9e6; --dim:#54677d; --dim2:#3a4a5c;
  }
  *{box-sizing:border-box; margin:0; padding:0;}
  html,body{background:var(--bg); color:var(--text); font-family:'JetBrains Mono',monospace;}
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
  }
  .wrap{max-width:540px; margin:0 auto; position:relative; z-index:6;}
  .bootline{font-size:11px; color:var(--dim); white-space:pre; margin-bottom:10px; line-height:1.5;}
  ::selection{background:var(--arch); color:#000;}
  .muted{color:var(--dim); font-size:11px;}

  /* ---------- lock screen ---------- */
  .lock-screen{position:fixed; inset:0; background:var(--bg); z-index:100; display:flex; align-items:center; justify-content:center; padding:20px;}
  .lock-box{border:1px solid var(--arch); border-radius:2px; padding:24px; width:100%; max-width:340px; background:var(--panel); box-shadow:0 0 30px rgba(23,147,209,0.15);}
  .lock-box input{
    width:100%; background:#050810; border:1px solid var(--line); color:var(--green); padding:10px;
    font-family:inherit; font-size:14px; border-radius:2px; margin:10px 0;
  }
  .lock-box button{
    width:100%; background:rgba(23,147,209,0.12); border:1px solid var(--arch); color:var(--arch-soft);
    padding:10px; border-radius:2px; font-family:inherit; font-weight:700; cursor:pointer; letter-spacing:0.5px;
  }
  .lock-box button:hover{background:rgba(23,147,209,0.22);}

  /* ---------- neofetch header (signature element) ---------- */
  .neofetch{
    display:flex; gap:18px; align-items:flex-start; border:1px solid var(--line);
    border-radius:2px; padding:16px; background:var(--panel);
    box-shadow:0 0 30px rgba(23,147,209,0.06), inset 0 0 40px rgba(23,147,209,0.02);
  }
  .arch-logo{
    font-size:9px; line-height:1.15; color:var(--arch); white-space:pre; font-weight:700;
    text-shadow:0 0 6px rgba(23,147,209,0.5); flex-shrink:0;
  }
  .nf-info{flex:1; min-width:0;}
  .nf-title{font-size:16px; font-weight:800; letter-spacing:0.5px; margin-bottom:2px;}
  .nf-title .u{color:var(--arch-soft);}
  .nf-title .at{color:var(--dim);}
  .nf-rule{height:1px; background:var(--line); margin:6px 0 8px;}
  .nf-row{display:flex; gap:6px; font-size:11.5px; margin-bottom:3px; align-items:baseline; flex-wrap:wrap;}
  .nf-row .k{color:var(--green-soft); font-weight:700; min-width:64px; flex-shrink:0;}
  .nf-row .v{color:var(--text); word-break:break-word;}
  .nf-swatches{display:flex; gap:3px; margin-top:10px;}
  .nf-swatches span{width:14px; height:8px; border-radius:1px; display:inline-block;}

  /* ---------- window-manager style panels ---------- */
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
    cursor:pointer; text-align:left; transition:.12s; position:relative;
  }
  .btn::before{content:"$ "; color:var(--dim);}
  .btn:active{transform:scale(.97);}
  .btn:hover{border-color:var(--arch); box-shadow:0 0 10px rgba(23,147,209,0.18); color:var(--arch-soft);}
  .btn-danger{border-color:#4a1f1f; color:#ff9a9a;}
  .btn-danger:hover{border-color:var(--red); box-shadow:0 0 10px rgba(255,95,86,0.2); color:#ff9a9a;}
  .btn-warn{border-color:#4a3d1f; color:var(--amber);}
  .btn-warn:hover{border-color:var(--amber); box-shadow:0 0 10px rgba(255,184,108,0.2);}
  .btn-magenta{border-color:#4a1f3d; color:#ff9ecb;}
  .btn-magenta:hover{border-color:var(--magenta); box-shadow:0 0 10px rgba(255,95,168,0.2);}
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

  .urlbox{border:1px dashed var(--arch-soft); border-radius:2px; padding:10px 12px; font-size:11.5px; word-break:break-all; margin-top:10px;}

  .toast{
    position:fixed; left:50%; bottom:22px; transform:translateX(-50%);
    background:#0a1620; border:1px solid var(--arch); color:var(--arch-soft);
    padding:10px 16px; border-radius:2px; font-size:12px; display:none; z-index:50;
    box-shadow:0 0 20px rgba(23,147,209,0.25);
  }
  .toast.show{display:block; animation:up .25s ease;}
  @keyframes up{from{opacity:0; transform:translate(-50%,10px);} to{opacity:1; transform:translate(-50%,0);}}

  .eye-btn{
    background:none; border:1px solid var(--line); color:var(--dim); font-size:10px;
    padding:2px 7px; border-radius:2px; cursor:pointer; font-family:inherit; letter-spacing:0.5px; transition:.15s;
  }
  .eye-btn:hover{color:var(--arch-soft); border-color:var(--arch);}
  .prompt-tail{color:var(--dim); font-size:12px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; margin-top:12px;}
  .cur{display:inline-block; width:8px; height:14px; background:var(--green); vertical-align:middle; animation:blink 1s steps(1) infinite;}
  @keyframes blink{50%{opacity:0;}}
</style>
</head>
<body class="crt">
<div id="lockScreen" class="lock-screen">
  <div class="lock-box">
    <div class="bootline">[lax@lax ~]$ sudo authenticate --token</div>
    <div class="muted" style="margin-bottom:8px;">Enter the access token printed in controller.log on first run.</div>
    <input id="tokenInput" type="password" placeholder="access token" autocomplete="off">
    <button onclick="doLogin()">[ unlock session ]</button>
  </div>
</div>

<div class="wrap" id="mainUI" style="display:none;">
  <div class="bootline" id="bootline"></div>

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
      <div class="nf-swatches">
        <span style="background:#ff5f56"></span><span style="background:#ffb86c"></span>
        <span style="background:#50fa7b"></span><span style="background:#1793d1"></span>
        <span style="background:#5ec8f8"></span><span style="background:#ff5fa8"></span>
        <span style="background:#cfd9e6"></span><span style="background:#54677d"></span>
      </div>
    </div>
  </div>

  <div class="urlbox" id="publicUrl">••••••••••••••••••••••••••••••••••</div>

  <div class="prompt-tail">[lax@lax ~]$ tail -f /var/log/session <span class="cur"></span></div>

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

  <div class="muted" style="margin-top:6px; text-align:center;">[lax@lax ~]$ session auto-refreshes · ctrl+d to disconnect</div>
  </div>
</div>

<div class="toast" id="notification"></div>

<script>
let TOKEN = localStorage.getItem('lc_token') || '';
const urlToken = new URLSearchParams(window.location.search).get('token');
if (urlToken) { TOKEN = urlToken; localStorage.setItem('lc_token', urlToken); }

function api(path, opts={}) {
  opts.headers = Object.assign({'X-Auth-Token': TOKEN, 'Content-Type':'application/json', 'ngrok-skip-browser-warning':'true'}, opts.headers||{});
  return fetch(path, opts).then(r => {
    if (r.status === 401) { showLock(); throw new Error('unauthorized'); }
    return r.json();
  });
}

function showLock(){
  document.getElementById('lockScreen').style.display = 'flex';
  document.getElementById('mainUI').style.display = 'none';
}

function doLogin(){
  const t = document.getElementById('tokenInput').value.trim();
  if (!t) return;
  TOKEN = t;
  localStorage.setItem('lc_token', t);
  document.getElementById('lockScreen').style.display = 'none';
  document.getElementById('mainUI').style.display = 'block';
  bootSequence();
}

function bootSequence(){
  const lines = ['[ok] lax session key accepted','[ok] establishing control channel','[ok] link online','[ok] whoami: lax'];
  const el = document.getElementById('bootline');
  el.textContent = '';
  lines.forEach((l,i)=> setTimeout(()=>{ el.textContent += l + '\n'; }, i*160));
  refreshAll();
  setInterval(getSystemStatus, 10000);
  setInterval(getOpenApps, 20000);
}

let privacyRevealed = false;
let realHostName = '';
let realPublicUrl = '';

function refreshAll(){
  getSystemStatus(); getOpenApps(); getBrightness(); getPublicUrl();
}

function togglePrivacy(){
  privacyRevealed = !privacyRevealed;
  document.getElementById('hostName').textContent = privacyRevealed ? (realHostName || '--') : '••••••••';
  document.getElementById('publicUrl').textContent = privacyRevealed ? (realPublicUrl || '--') : '••••••••••••••••••••••••••••••••••';
  document.getElementById('privacyToggle').textContent = privacyRevealed ? 'hide' : 'show';
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
    .then(d => { if (d.status === 'success') showNotification('brightness set'); }).catch(()=>{});
}

function sendCommand(command, param=null){
  const body = {command}; if (param) body.param = param;
  api('/api/command', {method:'POST', body: JSON.stringify(body)})
    .then(d => showNotification(d.status === 'success' ? `[ok] ${command}` : `[err] ${d.message||'failed'}`))
    .catch(()=>{});
}

function quickMsg(text){
  document.getElementById('msgText').value = text;
}

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
    .then(r => { if (!r.ok) return r.json().then(d => { throw new Error(d.message || 'capture failed'); }); return r.blob(); })
    .then(blob => {
      if (lastCaptureUrl) URL.revokeObjectURL(lastCaptureUrl);
      lastCaptureUrl = URL.createObjectURL(blob);
      document.getElementById('captureImg').src = lastCaptureUrl;
      document.getElementById('captureBox').style.display = 'block';
      showNotification('[ok] screenshot ready');
    })
    .catch((err) => showNotification(`[err] screenshot failed: ${err.message || 'unknown'}`));
}

function deleteScreenshot(){
  if (lastCaptureUrl) URL.revokeObjectURL(lastCaptureUrl);
  lastCaptureUrl = null;
  document.getElementById('captureImg').src = '';
  document.getElementById('captureBox').style.display = 'none';
  showNotification('[ok] screenshot deleted');
}

function showNotification(msg){
  const n = document.getElementById('notification');
  n.textContent = msg; n.classList.add('show');
  clearTimeout(n._t); n._t = setTimeout(()=> n.classList.remove('show'), 2500);
}

if (TOKEN) { document.getElementById('tokenInput').value = TOKEN; doLogin(); }
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 9. Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)


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
        msg = "clipboard sync unavailable (win32clipboard failed to import — reinstall pywin32)"
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
    return jsonify({"status": "error", "message": "could not write to PC clipboard (see controller.log)"}), 500


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
# 10. ngrok + networking helpers
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
# 11. Flask server thread...waitress if available, else Flask dev server
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
# 12. System tray
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
# 13. Entry point
# ---------------------------------------------------------------------------
def is_running_headless():
    """True if launched via pythonw.exe (no console attached) — i.e. via
    launch_hidden.vbs or the Startup folder. False if launched via python.exe
    directly (e.g. when troubleshooting in a visible terminal)."""
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

    # Give the server a moment before opening the dashboard
    time.sleep(1.5)

    # Only auto-open the browser when launched visibly via python.exe
    # (troubleshooting). When started headlessly via pythonw.exe — i.e.
    # from launch_hidden.vbs or Startup....stay silent.
    if not is_running_headless():
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT}/?token={AUTH_TOKEN}")
        except Exception:
            pass

    # Keep the script running forever without tray
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
