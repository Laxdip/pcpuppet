# pcpuppet 

## Control Your Windows PC from Your Phone

A lightweight self-hosted remote control panel for your own Windows laptop.
Runs quietly in the background (no console window), exposes a small web
dashboard styled like a terminal, and tunnels it to the internet via ngrok
so you can reach it from your phone.

Lock, sleep, shutdown/restart, adjust volume & brightness, sync the
clipboard, pop up an on-screen message, take a live screenshot of your laptop screen directly on your mobile, or close an
open window....all from your browser btw !

## Screenshots

**Login** | **Dashboard**
--- | ---
![Login](screenshots/login.png) | ![Dashboard](screenshots/interface.png)

**Features** | **Processes & Security**
--- | ---
![Features](screenshots/live%20screenshot.png) | ![Processes](screenshots/processes%20and%20security%20section.png) <br> ![Security](screenshots/security.png)

## Features

- **No console window** — detaches on startup, runs via `waitress`, logs to file
- **System tray icon** — open dashboard, copy token, restart tunnel, exit
- **Per-device sessions** — log in once per device, revoke access per-device
- **Second password gate** for extra-sensitive actions
- **Every action is sandboxed** — one failing feature (e.g. no battery, no
  `pycaw`) never takes the rest of the app down

## Setup

### Step 1: Download and Prepare Files

1. Put `laptop_controller.py`, `requirements.txt`, `setup.bat`, and
   `startup_launcher.vbs` in the same folder (e.g., `C:\pcpuppet`).

2. Double-click **`setup.bat`** once — this installs all required Python
   packages automatically.

   > **Note:** If you see a Windows SmartScreen warning, click "More info"
   > and "Run anyway" — this is just because the script installs Python
   > packages.

### Step 2: Install Python (if not already installed)

The setup script requires Python 3.8 or higher. If you don't have Python:

1. Download Python from [python.org](https://python.org/downloads/)
2. During installation, **make sure to check** "Add Python to PATH"
3. Restart your computer after installation

### Step 3: Set Up ngrok for Remote Access (Required for mobile access)

ngrok creates a public URL that tunnels to your local PC, so you can access
it from anywhere (even on different networks like mobile data or public WiFi).

**Option A: Using ngrok's Free Tier (Recommended)**

1. Go to [ngrok.com](https://ngrok.com/download)
2. Download `ngrok.exe` for Windows
3. Place `ngrok.exe` in one of these locations:
   - **Easiest:** Put it in the same folder as `laptop_controller.py`
   - **Or:** Put it in `C:\Windows\System32\`
   - **Or:** Put it anywhere and set the `NGROK_PATH` environment variable

4. **Create a free ngrok account** (required for persistent URLs):
   - Go to [ngrok.com/signup](https://ngrok.com/signup)
   - Sign up with email (free tier gives you 1 static domain)
   - Copy your authtoken from the dashboard

5. **Configure ngrok with your authtoken** (one-time setup):
   - Open Command Prompt as Administrator
   - Navigate to where `ngrok.exe` is located
   - Run: `ngrok config add-authtoken YOUR_AUTH_TOKEN_HERE`
   - (Replace `YOUR_AUTH_TOKEN_HERE` with your actual token from ngrok dashboard)

6. **Get your static domain** (optional but recommended):
   - In ngrok dashboard, go to "Cloud Edge" → "Domains"
   - Claim a free static domain like `yourname.ngrok-free.app`
   - This URL will stay the same every time you start the app

   **OR keep the free dynamic URL:**
   - Every time you start the app, ngrok generates a random URL
   - You'll need to check the URL each time (via dashboard or logs)

**Option B: Using a Pre-configured ngrok Path**

If `ngrok.exe` is not in your PATH, set the `NGROK_PATH` environment variable:

1. Press `Win + R`, type `sysdm.cpl`, and go to "Advanced" → "Environment Variables"
2. Under "System variables", click "New"
3. Variable name: `NGROK_PATH`
4. Variable value: Full path to `ngrok.exe` (e.g., `C:\Users\YourName\ngrok.exe`)
5. Click OK and restart your computer

### Step 4: Start the Application

1. Double-click **`startup_launcher.vbs`** to start the app.
   - No window appears — it runs silently in the background
   - Look for the `>_` icon in your system tray (bottom-right corner of screen)

2. **Find your access token**:
   - Right-click the tray icon → "Copy Access Token"
   - **OR** open `C:\Users\YourUsername\AppData\Local\LaptopController\controller.log`
   - Look for the line: `Access token: YOUR_TOKEN_HERE`

3. **Open the dashboard**:
   - Right-click tray icon → "Open Dashboard"
   - **OR** visit: `http://127.0.0.1:5000/?token=YOUR_TOKEN_HERE`
   - (Replace `YOUR_TOKEN_HERE` with the token from step 2)

### Step 5: Access from Your Phone (Different Network)

1. **Get the public URL**:
   - In the dashboard, click the "show" button next to the URL
   - **OR** check `controller.log` for the line: `Public URL: https://xxxx.ngrok.io`
   - **OR** right-click tray icon → "Open Dashboard" and check the URL at the top

2. **On your phone**:
   - Open a browser (Chrome, Firefox, Safari)
   - Enter the ngrok URL (e.g., `https://yourname.ngrok-free.app`)
   - Enter your access token on the login screen
   - **You're in!** You can now control your PC from anywhere

### Step 6: Auto-Start at Login (Optional)

1. Press `Win + R`
2. Type `shell:startup` and press Enter
3. Create a shortcut to `startup_launcher.vbs` in that folder
4. The app will now start automatically when you log in

### Step 7: Firewall Configuration (If Needed)

If the app doesn't start or you can't access it locally:

1. Windows may ask for firewall permission — click "Allow"
2. If not, manually allow port 5000:
   - Press `Win + R`, type `wf.msc` (Windows Firewall)
   - Click "Inbound Rules" → "New Rule"
   - Select "Port" → Next
   - Enter `5000` → Next → "Allow the connection"
   - Name it "pcpuppet" → Finish

## Where things live

Everything generated at runtime — your token, session data, and logs —
lives outside the repo, in `%LOCALAPPDATA%\LaptopController\`:

| File | Contents |
|---|---|
| `config.json` | your access token + local security password |
| `sessions.json` | active device sessions |
| `controller.log` | full activity log, including your token on first run |

## Changing the access token or security password

Just edit (or delete) `config.json` in `%LOCALAPPDATA%\LaptopController\`
and restart the app:

- **Delete the file entirely** → a brand new random token and password are
  generated on next launch.
- **Edit `"token"` or `"security_password"` directly** → set your own
  values, save, restart.

## Adding your own features

The whole app is one file, organized in numbered sections
(`# --- 1. Logging`, `# --- 2. Config`, etc.), which makes it easy to find
where to plug something in. To add a new remote action:

1. **Add a case to `execute_command()`** (search for `elif command ==`) —
   this is where `lock`, `sleep`, `shutdown`, etc. all live. Follow the same
   pattern: read `param` from the request if you need input, do the thing,
   return a `jsonify(...)` response.
2. **Add a button to `HTML_TEMPLATE`** — find the relevant `.win` panel (or
   add a new one) and add a button calling `sendCommand('your_command')`
   from the `<script>` section.
3. Wrap anything that touches an optional dependency (like `sbc` or `gw`)
   in the `@safe(...)` decorator, or check `if xyz is None:` first — this
   keeps one broken feature from crashing the whole app on machines missing
   that dependency.

**Need a new standalone endpoint instead?** Add a new `@app.route(...)`
function next to the existing ones (`/api/status`, `/api/apps`, etc.) — auth
is handled globally by `check_auth()`, so any new route is protected
automatically unless you explicitly exempt it there.

**Want a new optional dependency?** Import it in a `try/except` block like
`screen_brightness_control` or `pygetwindow` are, set it to `None` on
failure, and add it to `requirements.txt`.

## Ideas for features you could add

A few things that would slot into the existing structure without much
rework:

- **Change Windows password** — a new branch in execute_command() for "change_password" 
  that runs `net user <username> <new_password>` hidden (no console window, no shell exposure) 
  so you can reset a Windows user's password from your phone. Requires admin privileges.
- **File browser / file transfer** — a new `/api/files` route using
  `os.listdir` + `send_file` to browse and download files from your PC, or
  upload files to it. Add a `.win` panel with a folder list and an
  `<input type="file">`.
- **Webcam snapshot** — same pattern as `take_screenshot_bytes()`, but
  grabbing a frame from `cv2.VideoCapture(0)` instead of `ImageGrab.grab()`.
  Handy as a "who's at my desk" check.
- **Run arbitrary shortcuts/scripts** — a dropdown of pre-approved `.bat` /
  `.exe` paths (don't accept freeform shell input from the client — keep it
  to a fixed whitelist you define server-side) so you can trigger things
  like "open VS Code" or "start backup script" remotely.
- **Battery/CPU alert push** — a background thread that checks
  `psutil.sensors_battery()` / `psutil.cpu_percent()` on a timer and, if a
  threshold is crossed, calls `show_popup_message()` or pings a service like
  Pushover/ntfy so your phone gets notified without you opening the dashboard.
- **Scheduled actions** — add a `schedule` (pip package) job or a simple
  timer thread so you can queue "lock at 11pm" or "shutdown in 2 hours" from
  the dashboard, instead of only the fixed `-t 5` in `execute_command()`.
- **Multi-factor for dangerous commands** — require the `security_password`
  (not just the session token) specifically for `shutdown`/`restart`, by
  checking it inside those branches of `execute_command()` — right now the
  second password only guards `/api/security/unlock`, not individual
  commands.

If you build one of these, it'll fit the same pattern described above: a
branch in `execute_command()` (or a new route) plus a button/panel in
`HTML_TEMPLATE`.

## Security notes

- This exposes real control over your PC to anyone with the token — treat
  the token like a password.
- The ngrok URL is public by default; anyone who has both the URL and the
  token can reach the dashboard. Revoke sessions you don't recognize from
  the dashboard, or regenerate the token if you suspect it's leaked.
- If `pystray` fails to load (missing tray icon support), the app still runs
  headless in the background — you just won't get a tray icon; use Task
  Manager to stop it (`pythonw.exe`), or add a stop mechanism of your choice.
- Samajhne wale ko ishara ☕

## Requirements

See `requirements.txt`. Optional features degrade gracefully if a package
is missing — you'll just lose that one feature (e.g. no `pycaw` → no volume
control, falls back to media-key simulation).

## License
MIT

## Author
Prasad
