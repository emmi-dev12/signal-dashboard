#!/usr/bin/env python3
"""
Signal Dashboard — First-time setup
Run once: installs deps, creates config, installs LaunchAgent, starts server.
"""

import json
import os
import random
import shutil
import string
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
INSTALL_DIR  = Path.home() / 'signal-dashboard'
CONFIG_DIR   = Path.home() / '.signal-dashboard'
CONFIG_PATH  = CONFIG_DIR / 'config.json'
LOG_PATH     = Path.home() / 'Library' / 'Logs' / 'signal-dashboard.log'
PLIST_ID     = 'com.user.signal-dashboard'
PLIST_PATH   = Path.home() / 'Library' / 'LaunchAgents' / f'{PLIST_ID}.plist'

REQUIRED_PACKAGES = ['flask', 'apscheduler', 'feedparser', 'requests', 'beautifulsoup4']


def banner(msg: str):
    print(f'\n\033[1;35m▶ {msg}\033[0m')

def ok(msg: str):
    print(f'  \033[32m✓\033[0m {msg}')

def info(msg: str):
    print(f'  \033[90m·\033[0m {msg}')

def warn(msg: str):
    print(f'  \033[33m!\033[0m {msg}')


def install_deps():
    banner('Installing Python dependencies')
    result = subprocess.run(
        [sys.executable, '-m', 'pip', 'install', '--quiet', '--upgrade'] + REQUIRED_PACKAGES,
        capture_output=True, text=True
    )
    if result.returncode != 0:
        warn(f'pip output:\n{result.stderr[:500]}')
    else:
        ok(f'Installed: {", ".join(REQUIRED_PACKAGES)}')


def create_config():
    banner('Creating config')
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    existing = {}
    if CONFIG_PATH.exists():
        try:
            existing = json.loads(CONFIG_PATH.read_text())
            info('Found existing config — preserving settings')
        except Exception:
            pass

    # Generate ntfy topic if not set
    if not existing.get('ntfy_topic'):
        chars = string.ascii_lowercase + string.digits
        topic = 'signal-' + ''.join(random.choices(chars, k=12))
        existing['ntfy_topic'] = topic

    config = {
        'ntfy_topic':        existing.get('ntfy_topic', ''),
        'anthropic_api_key': existing.get('anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY', ''),
        'newsapi_key':       existing.get('newsapi_key', ''),
        'port':              existing.get('port', 8765),
        'breaking_threshold': existing.get('breaking_threshold', 8),
    }

    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    ok(f'Config saved → {CONFIG_PATH}')

    topic = config['ntfy_topic']
    print()
    print(f'  \033[1;36mntfy subscription URL:\033[0m')
    print(f'  \033[1m  https://ntfy.sh/{topic}\033[0m')
    print(f'  Open this URL in the ntfy app on your phone (iOS/Android) or in a browser.')
    print(f'  You\'ll receive a push notification for every signal scoring 8+/10.')
    print()

    return config


def copy_scripts():
    banner('Installing server to ~/signal-dashboard/')
    INSTALL_DIR.mkdir(exist_ok=True)
    for script in ['server.py', 'setup.py']:
        src = SCRIPT_DIR / script
        dst = INSTALL_DIR / script
        if src.exists():
            shutil.copy2(src, dst)
            ok(f'Copied {script} → {dst}')
        else:
            warn(f'Script not found: {src}')


def install_launch_agent(config: dict):
    banner('Installing macOS LaunchAgent (auto-start at login)')
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    server_path = INSTALL_DIR / 'server.py'
    log_path    = str(LOG_PATH)

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{PLIST_ID}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>{server_path}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>ANTHROPIC_API_KEY</key>
        <string>{config.get('anthropic_api_key', '')}</string>
    </dict>
</dict>
</plist>"""

    PLIST_PATH.write_text(plist)
    ok(f'Plist written → {PLIST_PATH}')

    # Unload old instance if present
    subprocess.run(['launchctl', 'unload', str(PLIST_PATH)], capture_output=True)
    # Load new
    result = subprocess.run(['launchctl', 'load', str(PLIST_PATH)], capture_output=True, text=True)
    if result.returncode == 0:
        ok('LaunchAgent loaded — will auto-start at every login')
    else:
        warn(f'launchctl load: {result.stderr.strip()}')
        info('You can start manually: python3 ~/signal-dashboard/server.py &')


def start_server(config: dict):
    banner('Starting server now')
    server_path = INSTALL_DIR / 'server.py'
    port        = config.get('port', 8765)

    # Check if already running
    check = subprocess.run(
        ['curl', '-s', '--connect-timeout', '2', f'http://localhost:{port}/'],
        capture_output=True
    )
    if check.returncode == 0 and check.stdout:
        ok(f'Server already running at http://localhost:{port}')
        return

    subprocess.Popen(
        [sys.executable, str(server_path)],
        stdout=open(LOG_PATH, 'a'), stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    import time
    time.sleep(3)
    check2 = subprocess.run(
        ['curl', '-s', '--connect-timeout', '3', f'http://localhost:{port}/'],
        capture_output=True
    )
    if check2.returncode == 0:
        ok(f'Server running → http://localhost:{port}')
    else:
        warn('Server may still be starting. Check logs:')
        info(f'tail -f {LOG_PATH}')


def main():
    print('\n\033[1;35m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m')
    print('\033[1;35m  SIGNAL DASHBOARD — Setup\033[0m')
    print('\033[1;35m━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\033[0m')

    copy_scripts()
    install_deps()
    config = create_config()
    install_launch_agent(config)
    start_server(config)

    port = config.get('port', 8765)
    print()
    print(f'\033[1;32m  ✓ Setup complete!\033[0m')
    print(f'  Dashboard → \033[1mhttp://localhost:{port}\033[0m')
    print(f'  Logs      → \033[90m{LOG_PATH}\033[0m')
    print(f'  Config    → \033[90m{CONFIG_PATH}\033[0m')
    print()
    # Try to open browser
    subprocess.Popen(['open', f'http://localhost:{port}'], capture_output=True)


if __name__ == '__main__':
    main()
