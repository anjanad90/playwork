#!/usr/bin/env python3
"""
PlayWork - alternating play/work timer with a game overlay and work-block lockout.

Runs at login, sits dormant, and wakes when any game on its watch list starts.
Pins a countdown to that game's window, warns you before the play block ends so
you can save, then closes or freezes the game for the work block.

Windows only.
  pip install psutil pywin32
  pyinstaller --onefile --noconsole --name PlayWork playwork.py
"""

import csv
import ctypes
import io
import json
import math
import struct
import wave
import os
import random
import subprocess
import sys
import threading
import time
import winreg
import smtplib
import urllib.request
from email.message import EmailMessage
import tkinter as tk
from tkinter import ttk
from tkinter import font as tkfont

try:
    import psutil
    import win32con
    import win32gui
    import win32process
except ImportError:
    ctypes.windll.user32.MessageBoxW(
        0, "Missing dependencies.\n\nRun:  pip install psutil pywin32",
        "PlayWork", 0x10)
    sys.exit(1)

try:
    import winsound
except ImportError:
    winsound = None


# ==========================================================================
# config
# ==========================================================================

VERSION = "1.0.0"

APP_DIR = os.path.dirname(os.path.abspath(
    sys.executable if getattr(sys, "frozen", False) else __file__))

# Settings live under %APPDATA% so the script and a built exe share one set,
# and nothing lands in a synced folder like OneDrive.
DATA_DIR = os.path.join(os.environ.get("APPDATA") or APP_DIR, "PlayWork")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    DATA_DIR = APP_DIR

CONFIG_PATH = os.path.join(DATA_DIR, "playwork.json")


def _migrate_old_files():
    """Move settings and log out of the program folder, once."""
    for name in ("playwork.json", "playwork-log.csv"):
        old = os.path.join(APP_DIR, name)
        new = os.path.join(DATA_DIR, name)
        if os.path.exists(old) and not os.path.exists(new):
            try:
                with open(old, "rb") as src_fh, open(new, "wb") as dst_fh:
                    dst_fh.write(src_fh.read())
                os.rename(old, old + ".moved")
            except Exception:
                pass


_migrate_old_files()

DEFAULTS = {
    "watched_games": ["VintageStory.exe"],
    "play_minutes": 45,
    "work_minutes": 60,
    "warn_minutes": 5,
    "grace_seconds": 90,
    "lock_mode": "close",          # close | freeze | none

    # Short work block before the day's first play block.
    "warmup_enabled": True,
    "warmup_minutes": 10,
    "warmup_date": "",             # last date the warm-up was served

    # Daily ceiling on play time. 0 turns it off.
    "daily_play_cap_minutes": 0,
    "today": {"date": "", "play": 0, "work": 0},
    "log_sessions": True,

    # Hold the clock when you walk away. 0 turns it off.
    "idle_hold_seconds": 300,

    # Only count work time while one of these is focused. Empty = always count.
    "gate_work_on_focus": False,
    "work_apps": [],

    "borrow_minutes": 5,
    "dev_no_phrase": False,
    "dev_mode": False,
    "volume": 0.5,
    "standby_until_game": True,
    "on_game_exit": "continue",      # continue | pause | reset
    "exit_grace_seconds": 20,

    # Tell someone when you bail out of a work block early.
    "alert_on_escape": False,
    "alert_method": "ntfy",          # ntfy | discord | email
    "alert_webhook": "",
    "alert_ntfy_topic": "",
    "alert_smtp_host": "smtp.gmail.com",
    "alert_smtp_port": 587,
    "alert_smtp_user": "",
    "alert_smtp_pass": "",
    "alert_email_to": "",
    "alert_name": "Someone",
    "relaunch_on_play": False,
    # Background launchers (Steam and friends) sit running with no window.
    # Requiring a visible window keeps them from counting as "the game".
    "require_window": True,
    "follow_window": True,
    "overlay_offset": [-250, 14],
    "fallback_position": [40, 40],
    "opacity": 0.88,
    "scale": 1.0,
    "show_flowers": True,
    # Let clicks pass through, but only while the game itself is focused, so
    # the overlay is always reachable when you alt-tab out.
    "click_through": True,
    "sound": True,
    "unlock_phrase": "let me out",
}


def load_config():
    cfg = dict(DEFAULTS)
    raw = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception:
            raw = {}

    # migrate the old single-target format
    if "watched_games" not in raw and "target_process" in raw:
        games = [raw["target_process"]] + list(raw.get("recent_targets", []))
        seen, merged = set(), []
        for name in games:
            if name and name.lower() not in seen:
                seen.add(name.lower())
                merged.append(name)
        raw["watched_games"] = merged
    if "on_game_exit" not in raw and "standby_on_game_exit" in raw:
        raw["on_game_exit"] = "reset" if raw["standby_on_game_exit"] else "continue"
    raw.pop("standby_on_game_exit", None)
    raw.pop("target_process", None)
    raw.pop("recent_targets", None)

    cfg.update(raw)
    if not cfg.get("watched_games"):
        cfg["watched_games"] = list(DEFAULTS["watched_games"])
    return cfg


def save_config(cfg):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except Exception:
        pass


# ==========================================================================
# Windows startup registration (per-user, no admin needed)
# ==========================================================================

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "PlayWork"


def _autostart_command():
    if getattr(sys, "frozen", False):
        return '"%s"' % sys.executable
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pyw):
        pyw = sys.executable
    return '"%s" "%s"' % (pyw, os.path.abspath(__file__))


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return bool(winreg.QueryValueEx(key, RUN_NAME)[0])
    except Exception:
        return False


def set_autostart(enabled):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ,
                                  _autostart_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_NAME)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


# ==========================================================================
# process / window inspection
# ==========================================================================

# Launchers and chat apps sit running in the background with no window. They
# make terrible watch targets: the timer would wake constantly and the lockout
# would fight something that is not the game.
LAUNCHER_EXES = {
    "steam.exe", "steamwebhelper.exe", "epicgameslauncher.exe",
    "battle.net.exe", "galaxyclient.exe", "origin.exe", "eadesktop.exe",
    "ubisoftconnect.exe", "riotclientservices.exe", "discord.exe",
    "playnite.desktopapp.exe", "gog galaxy.exe", "itch.exe",
}

IGNORED_EXES = {
    "explorer.exe", "applicationframehost.exe", "textinputhost.exe",
    "searchhost.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "systemsettings.exe", "python.exe", "pythonw.exe", "playwork.exe",
    "widgets.exe", "lockapp.exe",
}


def list_windowed_processes():
    """Executables that currently own a real, visible top-level window."""
    found = {}

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        width, height = right - left, bottom - top
        if width < 240 or height < 140:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            name = psutil.Process(pid).name()
        except Exception:
            return
        if name.lower() in IGNORED_EXES:
            return
        area = width * height
        if name not in found or area > found[name][1]:
            found[name] = (title, area)

    try:
        win32gui.EnumWindows(visit, None)
    except Exception:
        pass
    return [(n, d[0]) for n, d in sorted(found.items(), key=lambda kv: -kv[1][1])]


PROCESS_ALL_ACCESS = 0x1F0FFF
_ntdll = ctypes.WinDLL("ntdll")
_kernel32 = ctypes.WinDLL("kernel32")


def _with_handle(pid, fn):
    handle = _kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)
    if not handle:
        return False
    try:
        return fn(handle) == 0
    finally:
        _kernel32.CloseHandle(handle)


def alerts_ready(cfg):
    """True if the configured alert channel has enough to send with."""
    method = cfg.get("alert_method", "discord")
    if method == "discord":
        return cfg.get("alert_webhook", "").strip().startswith("http")
    if method == "ntfy":
        return bool(cfg.get("alert_ntfy_topic", "").strip())
    if method == "email":
        return all(cfg.get(k, "").strip() for k in
                   ("alert_smtp_user", "alert_smtp_pass", "alert_email_to"))
    return False


def send_alert(cfg, text, result):
    """Send on a worker thread. Writes ('ok'|'error', detail) into result."""

    def work():
        method = cfg.get("alert_method", "discord")
        try:
            if method == "discord":
                payload = json.dumps({"content": text}).encode("utf-8")
                req = urllib.request.Request(
                    cfg["alert_webhook"].strip(), data=payload,
                    headers={"Content-Type": "application/json",
                             "User-Agent": "PlayWork"})
                urllib.request.urlopen(req, timeout=12).read()

            elif method == "ntfy":
                topic = cfg["alert_ntfy_topic"].strip().rstrip("/")
                url = topic if topic.startswith("http") else "https://ntfy.sh/" + topic
                req = urllib.request.Request(
                    url, data=text.encode("utf-8"),
                    headers={"Title": "PlayWork", "Priority": "high",
                             "Tags": "warning", "User-Agent": "PlayWork"})
                urllib.request.urlopen(req, timeout=12).read()

            elif method == "email":
                msg = EmailMessage()
                msg["Subject"] = "PlayWork"
                msg["From"] = cfg["alert_smtp_user"].strip()
                msg["To"] = cfg["alert_email_to"].strip()
                msg.set_content(text)
                port = int(cfg.get("alert_smtp_port", 587))
                with smtplib.SMTP(cfg["alert_smtp_host"].strip(), port,
                                  timeout=20) as server:
                    server.starttls()
                    server.login(cfg["alert_smtp_user"].strip(),
                                 cfg["alert_smtp_pass"])
                    server.send_message(msg)
            else:
                result["status"] = ("error", "no method set")
                return
            result["status"] = ("ok", "")
        except Exception as exc:
            result["status"] = ("error", str(exc)[:120])

    threading.Thread(target=work, daemon=True).start()


class GameWatcher:
    """Watches a list of executables. Locks on to whichever one is running."""

    def __init__(self, names, require_window=True):
        self.require_window = require_window
        self.names = []
        self.exe_paths = {}
        self.pid = None
        self.hwnd = None
        self.active = None       # exe name of the game currently running
        self.frozen = False
        self.set_names(names)

    def set_names(self, names):
        self.names = [n.strip().lower() for n in names if n and n.strip()]
        self.pid = None
        self.hwnd = None
        self.active = None
        self.frozen = False

    def refresh(self):
        """Find a watched process, preferring the one already locked on to."""
        matches = {}
        for proc in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = (proc.info["name"] or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if name in self.names and name not in matches:
                matches[name] = proc.info["pid"]
                if proc.info.get("exe"):
                    self.exe_paths[name] = proc.info["exe"]

        if not matches:
            self.pid = self.hwnd = self.active = None
            self.frozen = False
            return False

        # keep the current game if it's still up, else take the first match
        chosen = self.active if self.active in matches else next(iter(matches))
        hwnd = self._main_window(matches[chosen])
        if hwnd is None and self.require_window:
            # try the others before giving up
            for name, pid in matches.items():
                other = self._main_window(pid)
                if other is not None:
                    chosen, hwnd = name, other
                    break
        if hwnd is None and self.require_window:
            self.pid = self.hwnd = self.active = None
            self.frozen = False
            return False
        self.active = chosen
        self.pid = matches[chosen]
        self.hwnd = hwnd
        return True

    @staticmethod
    def _main_window(pid):
        best = [None, 0]

        def visit(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return
            try:
                _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                return
            if wpid != pid:
                return
            try:
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            except Exception:
                return
            area = max(0, right - left) * max(0, bottom - top)
            if area > best[1]:
                best[0], best[1] = hwnd, area

        try:
            win32gui.EnumWindows(visit, None)
        except Exception:
            pass
        return best[0]

    def rect(self):
        if not self.hwnd:
            return None
        try:
            return win32gui.GetWindowRect(self.hwnd)
        except Exception:
            return None

    def is_running(self):
        return self.pid is not None and psutil.pid_exists(self.pid)

    def close(self):
        if self.hwnd:
            try:
                win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)
                return True
            except Exception:
                pass
        if self.pid:
            try:
                psutil.Process(self.pid).terminate()
                return True
            except Exception:
                pass
        return False

    def freeze(self):
        if self.pid and _with_handle(self.pid, _ntdll.NtSuspendProcess):
            self.frozen = True
            return True
        return False

    def thaw(self):
        result = bool(self.pid and _with_handle(self.pid, _ntdll.NtResumeProcess))
        self.frozen = False
        return result

    def launch(self):
        path = self.exe_paths.get(self.active or "")
        if path and os.path.exists(path):
            try:
                subprocess.Popen([path], cwd=os.path.dirname(path))
                return True
            except Exception:
                pass
        return False


# ==========================================================================
# sound
# ==========================================================================

CHIME = {
    "warmup": [(392, .16), (523, .16), (587, .40)],
    "play":   [(523, .13), (659, .13), (784, .34)],
    "work":   [(392, .20), (330, .20), (262, .50)],
    "warn":   [(440, .11), (392, .09), (440, .24)],
    "lock":   [(196, .30), (165, .55)],
    "done":   [(523, .12), (659, .12), (784, .12), (1047, .40)],
}
_WAV_CACHE = {}


def _render_wav(tones, volume):
    """Build a small WAV in memory so we can control volume, unlike Beep."""
    rate = 22050
    frames = bytearray()
    for freq, seconds in tones:
        count = max(1, int(rate * seconds))
        attack = max(1, int(rate * 0.008))
        for i in range(count):
            env = min(1.0, i / attack) * math.exp(-3.2 * i / count)
            sample = math.sin(2 * math.pi * freq * i / rate)
            sample += 0.25 * math.sin(4 * math.pi * freq * i / rate)
            value = int(32767 * 0.55 * volume * env * sample)
            frames += struct.pack("<h", max(-32768, min(32767, value)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(bytes(frames))
    return buf.getvalue()


def chime(name, volume=0.5, enabled=True):
    if not enabled or not winsound:
        return
    key = (name, round(float(volume), 2))
    try:
        if key not in _WAV_CACHE:
            _WAV_CACHE[key] = _render_wav(CHIME[name], max(0.0, min(1.0, volume)))
        winsound.PlaySound(_WAV_CACHE[key],
                           winsound.SND_MEMORY | winsound.SND_ASYNC)
    except Exception:
        try:                       # fall back to the old beeper
            for freq, seconds in CHIME[name]:
                winsound.Beep(int(freq), int(seconds * 1000))
        except Exception:
            pass


# ==========================================================================
# idle and focus
# ==========================================================================

class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds():
    """Seconds since the last keypress or mouse movement, anywhere in Windows."""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        try:
            now = _kernel32.GetTickCount64()
        except AttributeError:
            now = _kernel32.GetTickCount()
        return max(0.0, (now - info.dwTime) / 1000.0)
    except Exception:
        return 0.0


def foreground_process():
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return ""
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


# ==========================================================================
# session log
# ==========================================================================

LOG_PATH = os.path.join(DATA_DIR, "playwork-log.csv")


def log_block(phase, seconds, game):
    """Append one finished block. Never let logging break the timer."""
    try:
        new = not os.path.exists(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            if new:
                writer.writerow(["date", "finished", "phase", "minutes", "game"])
            writer.writerow([time.strftime("%Y-%m-%d"),
                             time.strftime("%H:%M"), phase,
                             round(seconds / 60.0, 1), game or ""])
    except Exception:
        pass


def read_log(days=7):
    """{date: {'play': minutes, 'work': minutes}} for the last N days."""
    out = {}
    try:
        if not os.path.exists(LOG_PATH):
            return out
        with open(LOG_PATH, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                day = row.get("date", "")
                phase = row.get("phase", "")
                if phase == "warmup":
                    phase = "work"
                if not day or phase not in ("play", "work"):
                    continue
                try:
                    minutes = float(row.get("minutes") or 0)
                except ValueError:
                    continue
                out.setdefault(day, {"play": 0.0, "work": 0.0})[phase] += minutes
    except Exception:
        return out
    recent = sorted(out)[-days:]
    return {d: out[d] for d in recent}


# ==========================================================================
# flora - the stats garden and the overlay border
# ==========================================================================

LEAF = "#63917c"
LEAF_DIM = "#47614f"
STEM = "#6d8f6f"


def _shade(dim, bright, dull):
    return dull if dim else bright


def draw_lily(c, x, ground, h, blooms, s=1.0, dim=False):
    """Lily of the valley: two long blades, an arching stem, bells beneath it."""
    leaf = _shade(dim, LEAF, LEAF_DIM)
    leaf2 = _shade(dim, "#527c68", "#3d5544")
    bell = _shade(dim, "#f6f3e9", "#bdb9a8")
    edge = _shade(dim, "#cdc7b2", "#8e8b7c")
    stem = _shade(dim, STEM, LEAF_DIM)

    # basal leaves, long and lance-shaped
    for side, fill, reach in ((-1, leaf2, 0.78), (1, leaf, 0.92)):
        tip_x = x + side * 9 * s
        tip_y = ground - h * reach
        c.create_polygon(
            x, ground,
            x + side * 8.5 * s, ground - h * reach * 0.55,
            tip_x, tip_y,
            x + side * 2.5 * s, ground - h * reach * 0.5,
            fill=fill, outline="", smooth=True)

    # arching stem: up, then bending over to one side
    bend = 9 * s
    tip = (x + bend, ground - h)
    c.create_line(x, ground,
                  x + 0.5 * s, ground - h * 0.5,
                  x + bend * 0.45, ground - h * 0.88,
                  tip[0], tip[1],
                  smooth=True, fill=stem, width=max(1, int(1.3 * s)))

    # bells hang below the arch, spaced along its outer half
    count = max(3, min(7, blooms))
    for i in range(count):
        t = (i + 0.5) / count
        bx = x + 0.5 * s + (tip[0] - x - 0.5 * s) * (0.30 + 0.70 * t)
        by = ground - h * (0.60 + 0.40 * t)
        drop = 2.6 * s
        c.create_line(bx, by, bx - 0.6 * s, by + drop, fill=stem)
        w, hh = 2.4 * s, 3.1 * s
        cx, cy = bx - 0.6 * s, by + drop
        c.create_oval(cx - w, cy, cx + w, cy + hh, fill=bell, outline=edge)
        c.create_oval(cx - w * 0.55, cy + hh * 0.55,
                      cx + w * 0.55, cy + hh * 1.05, fill=bell, outline="")


def draw_daisy(c, x, ground, h, blooms, s=1.0, dim=False):
    stem = _shade(dim, STEM, LEAF_DIM)
    petal = _shade(dim, "#efe9d8", "#adaa9b")
    heart = _shade(dim, "#c89b4a", "#8a6f38")
    c.create_line(x, ground, x, ground - h, fill=stem, width=max(1, int(1.2 * s)))
    for side in (-1, 1):
        c.create_polygon(x, ground - h * 0.38,
                         x + side * 5 * s, ground - h * 0.46,
                         x + side * 2 * s, ground - h * 0.28,
                         fill=_shade(dim, LEAF, LEAF_DIM), outline="",
                         smooth=True)
    top = ground - h
    r = 3.6 * s
    for k in range(9):
        a = math.pi * 2 * k / 9
        c.create_oval(x + math.cos(a) * r - 1.8 * s,
                      top + math.sin(a) * r - 1.8 * s,
                      x + math.cos(a) * r + 1.8 * s,
                      top + math.sin(a) * r + 1.8 * s,
                      fill=petal, outline="")
    c.create_oval(x - 1.9 * s, top - 1.9 * s, x + 1.9 * s, top + 1.9 * s,
                  fill=heart, outline="")


def draw_lavender(c, x, ground, h, blooms, s=1.0, dim=False):
    stem = _shade(dim, STEM, LEAF_DIM)
    bud = _shade(dim, "#8a7bb0", "#5d5478")
    c.create_line(x, ground, x + 1.5 * s, ground - h, smooth=True,
                  fill=stem, width=max(1, int(1.2 * s)))
    span = h * 0.42
    for i in range(max(4, blooms + 2)):
        t = i / max(1, (max(4, blooms + 2) - 1))
        by = ground - h + span * t
        bx = x + 1.5 * s - 1.5 * s * t
        off = 1.9 * s * (1 - t * 0.4)
        c.create_oval(bx - off, by - 1.5 * s, bx + off, by + 1.5 * s,
                      fill=bud, outline="")


def draw_poppy(c, x, ground, h, blooms, s=1.0, dim=False):
    stem = _shade(dim, STEM, LEAF_DIM)
    cup = _shade(dim, "#b04a2c", "#7a3a25")
    cup2 = _shade(dim, "#8e3a22", "#63301e")
    c.create_line(x, ground, x - 1.5 * s, ground - h * 0.5, x, ground - h,
                  smooth=True, fill=stem, width=max(1, int(1.2 * s)))
    top = ground - h
    for side, fill in ((-1, cup2), (1, cup)):
        c.create_polygon(x, top + 3.2 * s,
                         x + side * 5.2 * s, top + 1.4 * s,
                         x + side * 4.4 * s, top - 3.0 * s,
                         x + side * 0.6 * s, top - 3.6 * s,
                         fill=fill, outline="", smooth=True)
    c.create_oval(x - 1.3 * s, top - 1.0 * s, x + 1.3 * s, top + 1.6 * s,
                  fill=_shade(dim, "#2a1a12", "#241a14"), outline="")


def draw_forgetmenot(c, x, ground, h, blooms, s=1.0, dim=False):
    stem = _shade(dim, STEM, LEAF_DIM)
    sky = _shade(dim, "#7fa8c9", "#55708a")
    c.create_line(x, ground, x, ground - h, fill=stem, width=max(1, int(1.1 * s)))
    top = ground - h
    for cluster in range(min(3, max(1, blooms // 2))):
        cx = x + (cluster - 1) * 3.4 * s
        cy = top + cluster * 2.2 * s
        for k in range(5):
            a = math.pi * 2 * k / 5
            c.create_oval(cx + math.cos(a) * 1.9 * s - 1.1 * s,
                          cy + math.sin(a) * 1.9 * s - 1.1 * s,
                          cx + math.cos(a) * 1.9 * s + 1.1 * s,
                          cy + math.sin(a) * 1.9 * s + 1.1 * s,
                          fill=sky, outline="")
        c.create_oval(cx - 0.9 * s, cy - 0.9 * s, cx + 0.9 * s, cy + 0.9 * s,
                      fill=_shade(dim, "#e8d9a8", "#9a9377"), outline="")


# --- top-down blooms for the overlay border -------------------------------

PETAL_CREAM = "#efe9d8"
PETAL_LILAC = "#b3a6dd"
PETAL_GOLD = "#e3c684"
PETAL_ROSE = "#eec6d4"
HEART_GOLD = "#d8a33f"
BORDER_LEAF = "#5f8f6d"
BORDER_LEAF2 = "#48765a"


def draw_bloom(c, x, y, r, petal, n=6, heart=HEART_GOLD, tag="flora"):
    """A flat, face-on flower like the ones in a printed border."""
    for k in range(n):
        a = math.pi * 2 * k / n + (0.4 if n % 2 else 0)
        px = x + math.cos(a) * r * 0.66
        py = y + math.sin(a) * r * 0.66
        pr = r * 0.50
        c.create_oval(px - pr, py - pr, px + pr, py + pr,
                      fill=petal, outline="", tags=tag)
    hr = max(1.2, r * 0.30)
    c.create_oval(x - hr, y - hr, x + hr, y + hr,
                  fill=heart, outline="", tags=tag)


def draw_leaf(c, x, y, dirx, diry, length, fill=None, tag="flora"):
    """One pointed leaf growing out from (x, y). Always drawn behind a bloom,
    so it never stands on its own."""
    tipx, tipy = x + dirx * length, y + diry * length
    midx, midy = x + dirx * length * 0.48, y + diry * length * 0.48
    px, py = -diry, dirx
    wide = length * 0.30
    c.create_polygon(
        x, y,
        midx + px * wide, midy + py * wide,
        tipx, tipy,
        midx - px * wide, midy - py * wide,
        fill=fill or BORDER_LEAF, outline="", smooth=True, tags=tag)


def draw_carnation(c, x, y, r, shades, tag="flora"):
    """Frilly layered petals - denser and rounder than a daisy."""
    outer, mid, inner = shades
    for ring, (spread, count, size, fill) in enumerate((
            (0.94, 10, 0.40, outer), (0.60, 8, 0.36, mid),
            (0.26, 6, 0.30, inner))):
        for k in range(count):
            a = math.pi * 2 * k / count + ring * 0.42
            px = x + math.cos(a) * r * spread
            py = y + math.sin(a) * r * spread
            pr = r * size
            c.create_oval(px - pr, py - pr, px + pr, py + pr,
                          fill=fill, outline="", tags=tag)
    hr = max(1.0, r * 0.20)
    c.create_oval(x - hr, y - hr, x + hr, y + hr, fill=inner, outline="",
                  tags=tag)


def draw_minisun(c, x, y, r, tag="flora"):
    """Small face-on sunflower for the border."""
    for k in range(12):
        a = math.pi * 2 * k / 12
        ca, sa = math.cos(a), math.sin(a)
        c.create_polygon(
            x + ca * r * 0.42, y + sa * r * 0.42,
            x + ca * r * 1.05 - sa * r * 0.26,
            y + sa * r * 1.05 + ca * r * 0.26,
            x + ca * r * 1.30, y + sa * r * 1.30,
            x + ca * r * 1.05 + sa * r * 0.26,
            y + sa * r * 1.05 - ca * r * 0.26,
            fill="#e2ab33" if k % 2 else "#c98f22", outline="",
            smooth=True, tags=tag)
    c.create_oval(x - r * 0.60, y - r * 0.60, x + r * 0.60, y + r * 0.60,
                  fill="#4a3418", outline="", tags=tag)
    c.create_oval(x - r * 0.28, y - r * 0.34, x + r * 0.16, y + r * 0.12,
                  fill="#5d4520", outline="", tags=tag)


CARNATION_ROSE = ("#cf88a2", "#e5aac0", "#f3cfdb")
CARNATION_CORAL = ("#cf7458", "#e59a80", "#f2c2b0")
CARNATION_CREAM = ("#d6cbb2", "#e9e2d1", "#f7f3e9")
CARNATION_LILAC = ("#9d8fc9", "#b9addd", "#d8d0ee")


# The spray is a garland of flowers running out from the corner: one arm along
# the top edge, one down the side. Leaves are not separate items - each flower
# carries its own, drawn behind it, so greenery can never appear on its own.
# Sizes follow a fixed large/small/medium rhythm rather than anything random.

DAISY_COLOURS = [PETAL_CREAM, PETAL_LILAC, PETAL_GOLD, PETAL_ROSE]
CARNATIONS = [CARNATION_ROSE, CARNATION_CREAM, CARNATION_LILAC, CARNATION_CORAL]

#            kind         size  off  leaves (angles in degrees, local frame)
_RHYTHM = [
    ("carnation", 7.8, -3, (-118, -58)),
    ("daisy",     5.4,  3, ()),
    ("sun",       6.4, -2, (-100,)),
    ("daisy",     5.0,  3, ()),
    ("carnation", 7.2, -3, (-125, -55)),
    ("daisy",     6.0,  2, ()),
    ("sun",       5.2, -3, (-95,)),
    ("carnation", 6.8,  3, (-70,)),
    ("daisy",     5.6, -2, ()),
]


def _unit(kind, dx, dy, size, seq, leaves):
    if kind == "carnation":
        colour = CARNATIONS[seq % len(CARNATIONS)]
    elif kind == "daisy":
        colour = DAISY_COLOURS[seq % len(DAISY_COLOURS)]
    else:
        colour = None
    return (kind, dx, dy, size, colour, 6 if seq % 2 else 5, leaves)


def _build_corner_layout():
    units = [_unit("carnation", 3, 3, 8.4, 1, (-140, -95, -40))]

    for i in range(9):                       # arm along the top edge
        kind, size, off, leaves = _RHYTHM[i % len(_RHYTHM)]
        units.append(_unit(kind, 13 + i * 10.2, off, size, i, leaves))

    for i in range(6):                       # arm down the side
        kind, size, off, leaves = _RHYTHM[(i + 4) % len(_RHYTHM)]
        turned = tuple(a - 90 for a in leaves)   # point away from the panel
        units.append(_unit(kind, off, 14 + i * 10.6, size, i + 3, turned))

    anchor, rest = units[0], units[1:]
    rest.sort(key=lambda u: (u[1] ** 2 + u[2] ** 2) ** 0.5)
    return [anchor] + rest


CORNER_LAYOUT = _build_corner_layout()


def draw_corner(c, ox, oy, flip_x, flip_y, count, scale, tag="flora"):
    """Draw the first `count` units of the spray at one corner."""
    revealed = CORNER_LAYOUT[:max(0, count)]

    for kind, dx, dy, size, colour, petals, leaves in revealed:
        x = ox + dx * scale * flip_x
        y = oy + dy * scale * flip_y
        for i, angle in enumerate(leaves):
            rad = math.radians(angle)
            dirx = math.cos(rad) * flip_x
            diry = math.sin(rad) * flip_y
            draw_leaf(c, x, y, dirx, diry, size * 2.35 * scale,
                      BORDER_LEAF if i % 2 == 0 else BORDER_LEAF2, tag)

    for kind, dx, dy, size, colour, petals, leaves in revealed:
        x = ox + dx * scale * flip_x
        y = oy + dy * scale * flip_y
        if kind == "daisy":
            draw_bloom(c, x, y, size * scale, colour, petals, tag=tag)
        elif kind == "carnation":
            draw_carnation(c, x, y, size * scale, colour, tag=tag)
        elif kind == "sun":
            draw_minisun(c, x, y, size * scale, tag=tag)


def draw_sunflower(c, x, ground, h, blooms, s=1.0, dim=False):
    """Tall, heavy-headed, with big leaves. Stands above the rest."""
    stem = _shade(dim, "#5f8a55", "#44643e")
    leaf = _shade(dim, "#578f52", "#3e6640")
    ray = _shade(dim, "#e2ab33", "#9a7628")
    ray2 = _shade(dim, "#c98f22", "#836221")
    disc = _shade(dim, "#4a3418", "#33240f")

    h = h * 1.22                      # sunflowers overtop their neighbours
    c.create_line(x, ground, x - 1 * s, ground - h * 0.55, x, ground - h,
                  smooth=True, fill=stem, width=max(1, int(2.0 * s)))
    for side, at in ((-1, 0.42), (1, 0.62)):
        ly = ground - h * at
        c.create_polygon(
            x, ly,
            x + side * 6 * s, ly - 5 * s,
            x + side * 11 * s, ly - 1 * s,
            x + side * 6 * s, ly + 4 * s,
            fill=leaf, outline="", smooth=True, tags="")
    top = ground - h
    r = (5.0 + min(3.0, h * 0.045)) * s
    for k in range(14):
        a = math.pi * 2 * k / 14
        px, py = x + math.cos(a) * r * 1.28, top + math.sin(a) * r * 1.28
        c.create_polygon(
            x + math.cos(a) * r * 0.55, top + math.sin(a) * r * 0.55,
            px - math.sin(a) * r * 0.30, py + math.cos(a) * r * 0.30,
            px, py,
            px + math.sin(a) * r * 0.30, py - math.cos(a) * r * 0.30,
            fill=ray if k % 2 else ray2, outline="", smooth=True)
    c.create_oval(x - r * 0.72, top - r * 0.72, x + r * 0.72, top + r * 0.72,
                  fill=disc, outline="")
    c.create_oval(x - r * 0.34, top - r * 0.40, x + r * 0.20, top + r * 0.14,
                  fill=_shade(dim, "#5d4520", "#3d2c14"), outline="")


# lily of the valley and sunflowers are the ones worth seeing most often
SPECIES = [draw_lily, draw_sunflower, draw_lily, draw_daisy,
           draw_lily, draw_lavender, draw_sunflower, draw_poppy,
           draw_lily, draw_forgetmenot, draw_sunflower, draw_daisy]


def species_for(day):
    return SPECIES[sum(ord(ch) for ch in day) % len(SPECIES)]


# ==========================================================================
# overlay
# ==========================================================================

PALETTE = {
    "play":    ("#63917c", "#0e1512"),
    "work":    ("#a86a3d", "#170f0a"),
    "warn":    ("#c89b4a", "#171208"),
    "warmup":  ("#8a7bb0", "#14111a"),
    "locked":  ("#b04a2c", "#180c08"),
    "standby": ("#6f7a70", "#111410"),
}
INK = "#ded3bc"
DIM = "#7d8a80"
PANEL = "#171208"
FIELD = "#0d0a06"
CHROMA = "#010203"


class Overlay(tk.Tk):
    def __init__(self, app):
        super().__init__()
        self.app = app
        s = float(app.cfg.get("scale", 1.0))
        self.s = s
        # The canvas is bigger than the panel on purpose: the corner sprays
        # overhang by ~19px and the extra margin is transparent, so nothing
        # gets clipped. Panel size itself is unchanged.
        self.pad = int(24 * s)
        self.W = int(250 * s) + self.pad * 2
        self.H = int(126 * s) + self.pad * 2

        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.attributes("-alpha", float(app.cfg.get("opacity", 0.88)))
        self.configure(bg=CHROMA)
        try:
            self.attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass

        # One canvas for everything, so blooms can spill past the frame.
        self.c = tk.Canvas(self, width=self.W, height=self.H, bg=CHROMA,
                           highlightthickness=0, bd=0)
        self.c.pack()

        pad, W, H = self.pad, self.W, self.H
        self.item_panel = self.c.create_rectangle(pad, pad, W - pad, H - pad,
                                                  fill="#0e1512", width=0)
        self.item_border = self.c.create_rectangle(
            pad + int(4 * s), pad + int(4 * s),
            W - pad - int(4 * s), H - pad - int(4 * s),
            outline="#63917c", width=1)

        mid = W / 2
        self.item_phase = self.c.create_text(
            mid, pad + int(21 * s), text="STANDBY", fill="#6f7a70",
            font=("Segoe UI", max(7, int(8 * s)), "bold"))
        self.item_clock = self.c.create_text(
            mid, pad + int(50 * s), text="--:--", fill=INK,
            font=("Consolas", max(14, int(26 * s)), "bold"))
        self.item_note = self.c.create_text(
            mid, pad + int(78 * s), text="right-click for menu", fill=DIM,
            width=int(176 * s), justify="center", anchor="n",
            font=("Segoe UI", max(7, int(8 * s))))

        self._flora_state = None
        self._click_through = False
        self._drag = None
        self.c.bind("<Button-1>", self._drag_start)
        self.c.bind("<B1-Motion>", self._drag_move)
        self.c.bind("<Button-3>", self._popup)
        self.c.bind("<Double-Button-1>", lambda e: app.open_settings())

        self.menu = tk.Menu(self, tearoff=0)
        self.build_menu()
        self.after(60, self._no_focus_steal)
        self.place_at(*app.cfg.get("fallback_position", [40, 40]))

    def build_menu(self):
        app = self.app
        m = self.menu
        m.delete(0, "end")
        if app.standby:
            m.add_command(label="Start a block now", command=app.wake)
        else:
            m.add_command(label="Resume timer" if app.paused else "Pause timer",
                          command=app.toggle_pause)
            if app.phase == "play":
                m.add_command(label="Start working now", command=app.skip)
            else:
                m.add_command(
                    label="Skip to play" + (" (needs phrase)"
                                            if app.locked_in() else ""),
                    command=app.skip)
            if app.phase == "play" and not app.locking:
                m.add_command(
                    label="%d more minutes (off the next block)"
                          % max(1, int(app.cfg.get("borrow_minutes", 5))),
                    command=app.borrow)
            elif app.phase in ("work", "warmup"):
                m.add_command(label="Work 5 minutes longer",
                              command=lambda: app.extend(5))
                m.add_command(label="Work 10 minutes longer",
                              command=lambda: app.extend(10))
            m.add_command(
                label="Back to standby" + (" (needs phrase)"
                                           if app.locked_in() else ""),
                command=app.request_sleep)
        m.add_separator()
        m.add_command(label="Settings...", command=app.open_settings)
        m.add_separator()
        m.add_command(label="Quit", command=app.request_quit)

    def _no_focus_steal(self):
        try:
            hwnd = self._hwnd()
            if not hwnd:
                return
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            style |= win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)
        except Exception:
            pass

    def _hwnd(self):
        try:
            return (ctypes.windll.user32.GetParent(self.winfo_id())
                    or self.winfo_id())
        except Exception:
            return None

    def set_click_through(self, on):
        if on == self._click_through:
            return
        hwnd = self._hwnd()
        if not hwnd:
            return
        try:
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if on:
                style |= win32con.WS_EX_TRANSPARENT
            else:
                style &= ~win32con.WS_EX_TRANSPARENT
            win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, style)
            self._click_through = on
        except Exception:
            pass

    def set_opacity(self, value):
        try:
            self.attributes("-alpha", max(0.2, min(1.0, float(value))))
        except Exception:
            pass

    def _drag_start(self, e):
        self._drag = (e.x_root, e.y_root, self.winfo_x(), self.winfo_y())

    def _drag_move(self, e):
        if not self._drag:
            return
        sx, sy, ox, oy = self._drag
        nx, ny = ox + (e.x_root - sx), oy + (e.y_root - sy)
        self.place_at(nx, ny)
        self.app.remember_position(nx, ny)

    def _popup(self, e):
        self.build_menu()
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def place_at(self, x, y):
        self.geometry("+%d+%d" % (int(x), int(y)))

    def paint(self, mood, phase_text, clock_text, note, grown=0):
        accent, bg = PALETTE[mood]
        self.c.itemconfigure(self.item_panel, fill=bg)
        self.c.itemconfigure(self.item_border, outline=accent)
        self.c.itemconfigure(self.item_phase, text=phase_text, fill=accent)
        self.c.itemconfigure(self.item_clock, text=clock_text,
                             fill=INK if mood in ("play", "work") else accent)
        self.c.itemconfigure(self.item_note, text=note)
        self.paint_flora(grown)
        self.attributes("-topmost", True)

    def paint_flora(self, grown):
        """Blooms accumulate at both corners as today's work adds up."""
        if not self.app.cfg.get("show_flowers", True):
            if self._flora_state:
                self.c.delete("flora")
                self._flora_state = 0
            return
        per_corner = len(CORNER_LAYOUT)
        total = max(0, min(per_corner * 2, grown))
        if total == self._flora_state:
            return
        self._flora_state = total
        self.c.delete("flora")
        if not total:
            return
        # alternate corners so both sprays fill together
        top_left = (total + 1) // 2
        bottom_right = total // 2
        pad, W, H, s = self.pad, self.W, self.H, self.s
        draw_corner(self.c, pad, pad, 1, 1, top_left, s)
        draw_corner(self.c, W - pad, H - pad, -1, -1, bottom_right, s)
        self.c.tag_raise(self.item_phase)
        self.c.tag_raise(self.item_clock)
        self.c.tag_raise(self.item_note)


# ==========================================================================
# settings window
# ==========================================================================

class Settings(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.ui)
        self.app = app
        self.title("PlayWork settings")
        self.configure(bg=PANEL)
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.games = list(app.cfg.get("watched_games", []))
        self.vars = {}          # bool / int
        self.strs = {}          # text
        self.closed = False
        self._test = {}

        head = tk.Frame(self, bg=PANEL, padx=16, pady=12)
        head.pack(fill="x")
        tk.Label(head, text="PlayWork", bg=PANEL, fg=INK,
                 font=("Georgia", 15)).pack(side="left")
        tk.Label(head, text="v" + VERSION, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side="left", padx=(10, 0), pady=(6, 0))
        tk.Frame(self, bg="#2e2419", height=1).pack(fill="x")

        self._style_tabs()
        book = ttk.Notebook(self, style="PW.TNotebook")
        book.pack(padx=14, pady=(12, 0), fill="both", expand=True)
        self.tab_games = self._tab(book, "Games")
        self.tab_time = self._tab(book, "Timing")
        self.tab_limits = self._tab(book, "Limits")
        self.tab_stats = self._tab(book, "Stats")
        self.tab_alert = self._tab(book, "Accountability")
        self.tab_start = self._tab(book, "Startup")
        self.dev_on = bool(app.cfg.get("dev_mode")) or "--dev" in sys.argv
        self.tab_dev = self._tab(book, "Dev") if self.dev_on else None

        self._build_games()
        self._build_timing()
        self._build_limits()
        self._build_stats()
        self._build_alerts()
        self._build_startup()
        if self.dev_on:
            self._build_dev()

        tk.Frame(self, bg="#2e2419", height=1).pack(fill="x", pady=(12, 0))
        bar = tk.Frame(self, bg=PANEL)
        bar.pack(fill="x", padx=16, pady=12)
        self.status = tk.Label(bar, bg=PANEL, fg=DIM, font=("Segoe UI", 8), text="")
        self.status.pack(side="left")
        self._btn(bar, "Close", self.close).pack(side="right")
        apply_btn = self._btn(bar, "Apply", self.apply)
        apply_btn.configure(bg="#a86a3d", fg="#ffffff",
                            activebackground="#c07f4b", font=("Segoe UI", 8, "bold"))
        apply_btn.pack(side="right", padx=6)

        self.poll_detected()

    # ---- widget helpers ----

    def _style_tabs(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")     # the only built-in theme that restyles cleanly
        except tk.TclError:
            pass
        style.configure("PW.TNotebook", background=PANEL, borderwidth=0,
                        tabmargins=(0, 0, 0, 0))
        style.configure("PW.TNotebook.Tab", background="#100c07", foreground=DIM,
                        padding=(16, 8), borderwidth=0, font=("Segoe UI", 9))
        style.map("PW.TNotebook.Tab",
                  background=[("selected", PANEL)],
                  foreground=[("selected", "#c89b4a")],
                  expand=[("selected", (0, 0, 0, 0))])
        style.layout("PW.TNotebook.Tab", [("Notebook.tab", {"sticky": "nswe",
                     "children": [("Notebook.padding", {"side": "top",
                                   "sticky": "nswe", "children":
                                   [("Notebook.label", {"side": "top",
                                     "sticky": ""})]})]})])

    def _tab(self, book, label):
        frame = tk.Frame(book, bg=PANEL, padx=16, pady=6, width=430)
        book.add(frame, text=label)
        return frame

    def _section(self, parent, text):
        wrap = tk.Frame(parent, bg=PANEL)
        wrap.pack(fill="x", pady=(14, 6))
        tk.Label(wrap, text=text.upper(), bg=PANEL, fg="#c89b4a",
                 font=("Segoe UI", 8, "bold")).pack(side="left")
        tk.Frame(wrap, bg="#2e2419", height=1).pack(side="left", fill="x",
                                                    expand=True, padx=(10, 0),
                                                    pady=(6, 0))

    def _card(self, parent, title):
        """A bordered instruction panel."""
        outer = tk.Frame(parent, bg="#2e2419", padx=1, pady=1)
        inner = tk.Frame(outer, bg="#100c07", padx=13, pady=11)
        inner.pack(fill="both", expand=True)
        if title:
            tk.Label(inner, text=title, bg="#100c07", fg="#c89b4a",
                     font=("Segoe UI", 9, "bold")).pack(anchor="w",
                                                        pady=(0, 7))
        return outer, inner

    def _steps(self, parent, lines, bg="#100c07"):
        for i, line in enumerate(lines, 1):
            row = tk.Frame(parent, bg=bg)
            row.pack(fill="x", pady=1)
            tk.Label(row, text="%d" % i, bg=bg, fg="#7a6236",
                     font=("Consolas", 9, "bold"), width=2,
                     anchor="nw").pack(side="left", anchor="n")
            tk.Label(row, text=line, bg=bg, fg=INK, font=("Segoe UI", 9),
                     justify="left", wraplength=340,
                     anchor="w").pack(side="left", fill="x", expand=True)

    def _btn(self, parent, text, command):
        return tk.Button(parent, text=text, command=command, bg="#241c14",
                         fg=INK, relief="flat", padx=10, pady=4,
                         activebackground="#33281c", activeforeground=INK,
                         font=("Segoe UI", 8))

    def _spin(self, parent, label, key, lo, hi, row):
        tk.Label(parent, text=label, bg=PANEL, fg=INK,
                 font=("Segoe UI", 9)).grid(row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=str(self.app.cfg.get(key, DEFAULTS[key])))
        tk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=6,
                   bg=FIELD, fg=INK, relief="flat", justify="center",
                   buttonbackground="#241c14",
                   font=("Consolas", 10)).grid(row=row, column=1, padx=10, pady=2)
        self.vars[key] = var

    def _check(self, parent, text, var):
        tk.Checkbutton(parent, text=text, variable=var, bg=PANEL, fg=INK,
                       selectcolor=FIELD, activebackground=PANEL,
                       activeforeground=INK, font=("Segoe UI", 9), anchor="w",
                       highlightthickness=0, bd=0).pack(anchor="w")

    def _flag(self, parent, text, key):
        var = tk.BooleanVar(value=bool(self.app.cfg.get(key, DEFAULTS[key])))
        self.vars[key] = var
        self._check(parent, text, var)
        return var

    def _radios(self, parent, key, options):
        var = tk.StringVar(value=str(self.app.cfg.get(key, DEFAULTS[key])))
        for value, text in options:
            tk.Radiobutton(parent, text=text, value=value, variable=var,
                           bg=PANEL, fg=INK, selectcolor=FIELD,
                           activebackground=PANEL, activeforeground=INK,
                           font=("Segoe UI", 9), anchor="w",
                           highlightthickness=0, bd=0).pack(anchor="w")
        self.strs[key] = var
        return var

    def _entry(self, parent, label, key, show=None, width=42):
        tk.Label(parent, text=label, bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(6, 1))
        var = tk.StringVar(value=str(self.app.cfg.get(key, DEFAULTS.get(key, ""))))
        tk.Entry(parent, textvariable=var, width=width, bg=FIELD, fg=INK,
                 relief="flat", insertbackground="#c89b4a",
                 show=show).pack(anchor="w", ipady=3)
        self.strs[key] = var
        return var

    # ---- tabs ----

    def _build_games(self):
        parent = self.tab_games
        self._section(parent, "Games it watches for")
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x")
        edge = tk.Frame(row, bg="#2e2419", padx=1, pady=1)
        edge.pack(side="left", fill="both", expand=True)
        self.box = tk.Listbox(edge, width=32, height=7, bg=FIELD, fg=INK,
                              relief="flat", activestyle="none", bd=0,
                              highlightthickness=0, selectbackground="#a86a3d",
                              selectforeground="#ffffff", font=("Consolas", 9))
        self.box.pack(fill="both", expand=True)
        side = tk.Frame(row, bg=PANEL)
        side.pack(side="left", padx=(8, 0), fill="y")
        self._btn(side, "Add running...", self.add_running).pack(fill="x")
        self._btn(side, "Add by name", self.add_typed).pack(fill="x", pady=4)
        self._btn(side, "Remove", self.remove_selected).pack(fill="x")
        self.refresh_list()

        self.detected = tk.Label(parent, bg=PANEL, fg=DIM, font=("Consolas", 8),
                                 anchor="w", justify="left")
        self.detected.pack(anchor="w", pady=(8, 0))

    def _build_timing(self):
        parent = self.tab_time
        self._section(parent, "Block lengths")
        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(anchor="w")
        self._spin(grid, "Play (min)", "play_minutes", 1, 240, 0)
        self._spin(grid, "Work (min)", "work_minutes", 1, 240, 1)
        self._spin(grid, "Warn me this early (min)", "warn_minutes", 0, 30, 2)
        self._spin(grid, "Grace before locking (sec)", "grace_seconds", 0, 600, 3)
        self._spin(grid, "Wait after quitting (sec)", "exit_grace_seconds", 3, 600, 4)

        tk.Label(parent, text="Presets", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(10, 3))
        presets = tk.Frame(parent, bg=PANEL)
        presets.pack(anchor="w")
        for label, p, w in (("60/60", 60, 60), ("45/60", 45, 60),
                            ("50/10", 50, 10), ("25/5", 25, 5)):
            self._btn(presets, label,
                      lambda p=p, w=w: self.apply_preset(p, w)).pack(side="left",
                                                                     padx=(0, 4))

        self._section(parent, "When a work block starts")
        self._radios(parent, "lock_mode", (
            ("close", "Close the game (safest for saves)"),
            ("freeze", "Freeze it, resume at play time"),
            ("none", "Do nothing - overlay only")))

        self._section(parent, "When I quit the game mid-block")
        self._radios(parent, "on_game_exit", (
            ("continue", "Keep the clock running"),
            ("pause", "Hold the clock, resume when I reopen it"),
            ("reset", "Back to standby, start fresh next time")))
        tk.Label(parent, bg=PANEL, fg=DIM, font=("Segoe UI", 8), justify="left",
                 wraplength=360,
                 text="Keep running means closing the game to watch something "
                      "else still burns your play block. Hold is for short "
                      "breaks, but nothing stops you holding it forever."
                 ).pack(anchor="w", pady=(4, 0))

    def _build_limits(self):
        parent = self.tab_limits

        self._section(parent, "Warm-up")
        self._flag(parent, "Start the day with a short work block",
                   "warmup_enabled")
        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(anchor="w")
        self._spin(grid, "Warm-up length (min)", "warmup_minutes", 1, 120, 0)
        tk.Label(parent, bg=PANEL, fg=DIM, font=("Segoe UI", 8), justify="left",
                 wraplength=380,
                 text="Once a day, the first time you open a game, you get this "
                      "instead of a play block. Short on purpose - it's a "
                      "starter, not a shift."
                 ).pack(anchor="w", pady=(4, 0))

        self._section(parent, "Daily play cap")
        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(anchor="w")
        self._spin(grid, "Max play per day (min, 0 = off)",
                   "daily_play_cap_minutes", 0, 1440, 0)
        self.cap_state = tk.Label(parent, bg=PANEL, fg=DIM,
                                  font=("Consolas", 8), anchor="w")
        self.cap_state.pack(anchor="w", pady=(4, 0))

        self._section(parent, "Walking away")
        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(anchor="w")
        self._spin(grid, "Hold clock after idle (sec, 0 = off)",
                   "idle_hold_seconds", 0, 3600, 0)
        self._spin(grid, "Borrowed minutes per click", "borrow_minutes", 1, 30, 1)

        self._section(parent, "What counts as a game")
        self._flag(parent, "Only count a game once it has a visible window",
                   "require_window")
        tk.Label(parent, bg=PANEL, fg=DIM, font=("Segoe UI", 8), justify="left",
                 wraplength=380,
                 text="Keeps background launchers like Steam from waking the "
                      "timer or being closed over and over."
                 ).pack(anchor="w", pady=(0, 2))

        self._section(parent, "Work must look like work")
        self._flag(parent, "Only count work time while a work app is focused",
                   "gate_work_on_focus")
        row = tk.Frame(parent, bg=PANEL)
        row.pack(fill="x", pady=(4, 0))
        edge = tk.Frame(row, bg="#2e2419", padx=1, pady=1)
        edge.pack(side="left", fill="both", expand=True)
        self.workbox = tk.Listbox(edge, width=30, height=4, bg=FIELD, fg=INK,
                                  relief="flat", activestyle="none", bd=0,
                                  highlightthickness=0,
                                  selectbackground="#a86a3d",
                                  selectforeground="#ffffff",
                                  font=("Consolas", 9))
        self.workbox.pack(fill="both", expand=True)
        self.work_apps = list(self.app.cfg.get("work_apps", []))
        for name in self.work_apps:
            self.workbox.insert("end", name)
        side = tk.Frame(row, bg=PANEL)
        side.pack(side="left", padx=(8, 0), fill="y")
        self._btn(side, "Add running...", self.add_work_running).pack(fill="x")
        self._btn(side, "Remove", self.remove_work).pack(fill="x", pady=4)
        tk.Label(parent, bg=PANEL, fg=DIM, font=("Segoe UI", 8), justify="left",
                 wraplength=380,
                 text="The work clock pauses whenever something else is in "
                      "front. It never blocks anything - it just doesn't give "
                      "you credit."
                 ).pack(anchor="w", pady=(4, 0))
        self.refresh_cap()

    def refresh_cap(self):
        try:
            record = self.app.roll_day()
            played = record.get("play", 0) / 60.0
            worked = record.get("work", 0) / 60.0
            cap = int(self.vars["daily_play_cap_minutes"].get() or 0)
            text = "today: %.0f min played, %.0f min worked" % (played, worked)
            if cap:
                text += "  |  %.0f of %d used" % (played, cap)
            self.cap_state.configure(text=text)
        except Exception:
            pass
        if not self.closed:
            self.after(3000, self.refresh_cap)

    def add_work_running(self):
        rows = list_windowed_processes()
        win = tk.Toplevel(self)
        win.title("Add work apps")
        win.configure(bg=PANEL, padx=14, pady=12)
        win.attributes("-topmost", True)
        box = tk.Listbox(win, width=52, height=min(12, max(4, len(rows))),
                         bg=FIELD, fg=INK, relief="flat", activestyle="none",
                         selectbackground="#a86a3d", selectforeground="#ffffff",
                         selectmode="extended", font=("Consolas", 9))
        box.pack(pady=6, fill="both", expand=True)
        for name, title in rows:
            short = title if len(title) <= 30 else title[:29] + "\u2026"
            box.insert("end", "%-24s %s" % (name, short))

        def take(_=None):
            for index in box.curselection():
                name = rows[index][0]
                if name.lower() not in [w.lower() for w in self.work_apps]:
                    self.work_apps.append(name)
                    self.workbox.insert("end", name)
            win.destroy()

        box.bind("<Double-Button-1>", take)
        self._btn(win, "Add selected", take).pack(anchor="e")

    def remove_work(self):
        for index in reversed(list(self.workbox.curselection())):
            del self.work_apps[index]
            self.workbox.delete(index)

    def _build_stats(self):
        parent = self.tab_stats

        self._section(parent, "Your field")
        self.garden = tk.Canvas(parent, width=392, height=172, bg="#12160f",
                                highlightthickness=1,
                                highlightbackground="#2e2419")
        self.garden.pack(anchor="w")
        tk.Label(parent, bg=PANEL, fg=DIM, font=("Segoe UI", 8),
                 text="One plant per day worked. It grows with the hours."
                 ).pack(anchor="w", pady=(4, 0))

        self._section(parent, "Last 7 days")
        self.chart = tk.Canvas(parent, width=390, height=180, bg=PANEL,
                               highlightthickness=0)
        self.chart.pack(anchor="w")
        legend = tk.Frame(parent, bg=PANEL)
        legend.pack(anchor="w", pady=(8, 0))
        for colour, text in (("#63917c", "play"), ("#a86a3d", "work")):
            tk.Frame(legend, bg=colour, width=10, height=10).pack(side="left",
                                                                  pady=2)
            tk.Label(legend, text=text, bg=PANEL, fg=DIM,
                     font=("Segoe UI", 8)).pack(side="left", padx=(4, 14))
        self.totals = tk.Label(parent, bg=PANEL, fg=INK, font=("Segoe UI", 9),
                               anchor="w", justify="left")
        self.totals.pack(anchor="w", pady=(10, 0))
        self._btn(parent, "Open the log file", self.open_log).pack(anchor="w",
                                                                   pady=(10, 0))
        self.draw_chart()
        self.draw_garden()

    def open_log(self):
        try:
            if os.path.exists(LOG_PATH):
                os.startfile(LOG_PATH)
            else:
                self.status.configure(fg=DIM, text="no blocks logged yet")
        except Exception:
            pass

    def draw_garden(self):
        """Every day worked plants something. More days pack tighter, so a
        long stretch reads as a dense meadow rather than a tidy row."""
        c = self.garden
        c.delete("all")
        width, height = 392, 172

        for y in range(0, height, 2):
            t = y / float(height)
            c.create_rectangle(
                0, y, width, y + 2, width=0,
                fill="#%02x%02x%02x" % (16 + int(9 * t), 20 + int(11 * t),
                                        14 + int(7 * t)))

        days = read_log(100000)
        worked = [(d, v) for d, v in sorted(days.items()) if v["work"] >= 1]
        if not worked:
            c.create_text(width / 2, height / 2 - 6, fill=DIM,
                          font=("Segoe UI", 9), text="Nothing planted yet.")
            c.create_text(width / 2, height / 2 + 12, fill="#5c6a5e",
                          font=("Segoe UI", 8),
                          text="Finish a work block and the first one appears.")
            return

        total_days = len(worked)
        total_hours = sum(v["work"] for _, v in worked) / 60.0

        # Cap what we actually draw, but keep the spread across the whole run.
        drawn = worked
        if len(drawn) > 320:
            stride = len(drawn) / 320.0
            drawn = [worked[int(i * stride)] for i in range(320)]

        n = len(drawn)
        rows = 2 if n <= 22 else 3 if n <= 70 else 4 if n <= 160 else 5
        rows = min(rows, max(1, n))

        # oldest at the back, newest in front
        buckets = [[] for _ in range(rows)]
        for i, item in enumerate(drawn):
            buckets[min(rows - 1, i * rows // n)].append(item)

        back_y, front_y = 74, height - 10
        for r in range(rows):
            t = r / float(max(1, rows - 1)) if rows > 1 else 1.0
            ground = back_y + (front_y - back_y) * t
            scale = 0.52 + 0.48 * t
            dim = t < 0.55
            plants = buckets[r]

            c.create_rectangle(0, ground, width, ground + 9, width=0,
                               fill="#1c2517" if dim else "#26311d")
            for g in range(0, width, 6):
                seed = (g * 7919 + r * 131) % 13
                lean = (seed % 5) - 2
                tall = (4 + seed % 5) * scale
                c.create_line(g, ground + 2, g + lean, ground - tall,
                              fill="#2b4530" if dim else "#3a5c3e")
            if not plants:
                continue

            # spacing shrinks as the row fills, so plants start to overlap
            span = width - 16
            step = span / float(len(plants))
            for i, (day, value) in enumerate(plants):
                jitter = ((sum(ord(ch) for ch in day) % 7) - 3) * 0.9
                x = 8 + step * (i + 0.5) + jitter
                minutes = value["work"]
                h = (16 + min(58, minutes * 0.55)) * scale
                blooms = int(min(7, 3 + minutes // 22))
                species_for(day)(c, x, ground, h, blooms, scale, dim)

        label = "%d days   %.0f h" % (total_days, total_hours)
        if total_days > 320:
            label += "   (showing 320)"
        c.create_text(width - 8, 12, anchor="e", fill="#9fb0a4",
                      font=("Consolas", 8), text=label)

    def draw_chart(self):
        canvas = self.chart
        canvas.delete("all")
        data = read_log(7)
        if not data:
            canvas.create_text(8, 20, anchor="w", fill=DIM,
                               font=("Segoe UI", 9),
                               text="No finished blocks yet. Come back "
                                    "after a session.")
            return
        peak = max(max(v["play"], v["work"]) for v in data.values()) or 1
        left, width, top, gap = 34, 270, 14, 23
        total_play = total_work = 0.0
        for i, (day, value) in enumerate(sorted(data.items())):
            y = top + i * gap
            label = time.strftime("%a", time.strptime(day, "%Y-%m-%d"))
            canvas.create_text(4, y + 7, anchor="w", fill=DIM,
                               font=("Consolas", 8), text=label)
            for j, (key, colour) in enumerate((("play", "#63917c"),
                                               ("work", "#a86a3d"))):
                minutes = value[key]
                span = minutes / peak * width
                bar_y = y + j * 8
                canvas.create_rectangle(left, bar_y, left + width, bar_y + 6,
                                        fill="#100c07", width=0)
                if span > 0.5:
                    canvas.create_rectangle(left, bar_y, left + span, bar_y + 6,
                                            fill=colour, width=0)
                if minutes >= 1:
                    canvas.create_text(left + width + 6, bar_y + 3, anchor="w",
                                       fill=DIM, font=("Consolas", 7),
                                       text="%.0f" % minutes)
            total_play += value["play"]
            total_work += value["work"]
        self.totals.configure(
            text="7-day total   play %.1f h     work %.1f h"
                 % (total_play / 60.0, total_work / 60.0))

    def _build_dev(self):
        parent = self.tab_dev
        app = self.app

        self._section(parent, "Live state")
        edge = tk.Frame(parent, bg="#2e2419", padx=1, pady=1)
        edge.pack(anchor="w", fill="x")
        self.state_box = tk.Label(edge, bg="#100c07", fg="#9fb0a4",
                                  font=("Consolas", 8), justify="left",
                                  anchor="nw", padx=10, pady=8)
        self.state_box.pack(fill="both", expand=True)

        self._section(parent, "Jump the clock")
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w")
        for label, secs in (("5s left", 5), ("30s left", 30),
                            ("-1 min", -60), ("+1 min", 60)):
            self._btn(row, label,
                      lambda s=secs: app.dev_nudge(s)).pack(side="left",
                                                            padx=(0, 4))

        self._section(parent, "Force phase")
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w")
        for label, phase in (("Warm-up", "warmup"), ("Play", "play"),
                             ("Work", "work")):
            self._btn(row, label,
                      lambda p=phase: app.dev_phase(p)).pack(side="left",
                                                             padx=(0, 4))
        self._btn(row, "Standby", app.sleep).pack(side="left", padx=(0, 4))

        self._section(parent, "Day state")
        grid = tk.Frame(parent, bg=PANEL)
        grid.pack(anchor="w")
        tk.Label(grid, text="Played today (min)", bg=PANEL, fg=INK,
                 font=("Segoe UI", 9)).grid(row=0, column=0, sticky="w", pady=2)
        self.dev_played = tk.StringVar(value="0")
        tk.Spinbox(grid, from_=0, to=1440, textvariable=self.dev_played, width=6,
                   bg=FIELD, fg=INK, relief="flat", justify="center",
                   buttonbackground="#241c14",
                   font=("Consolas", 10)).grid(row=0, column=1, padx=8)
        self._btn(grid, "Set",
                  lambda: self.dev_apply_today("play")).grid(row=0, column=2)

        tk.Label(grid, text="Worked today (min)", bg=PANEL, fg=INK,
                 font=("Segoe UI", 9)).grid(row=1, column=0, sticky="w", pady=2)
        self.dev_worked = tk.StringVar(value="0")
        tk.Spinbox(grid, from_=0, to=1440, textvariable=self.dev_worked, width=6,
                   bg=FIELD, fg=INK, relief="flat", justify="center",
                   buttonbackground="#241c14",
                   font=("Consolas", 10)).grid(row=1, column=1, padx=8)
        self._btn(grid, "Set",
                  lambda: self.dev_apply_today("work")).grid(row=1, column=2)

        tk.Label(parent, text="Blooms (1 per 6 min worked)", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(8, 3))
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w")
        for label, mins in (("+6 min", 6), ("+30 min", 30), ("+1 h", 60)):
            self._btn(row, label,
                      lambda m=mins: self.dev_bump_work(m)).pack(side="left",
                                                                 padx=(0, 4))
        self._btn(row, "Fill", lambda: self.dev_bump_work(200)).pack(side="left",
                                                                     padx=(0, 4))
        self._btn(row, "Clear", lambda: self.dev_bump_work(-100000)).pack(
            side="left")

        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w", pady=(8, 0))
        self._btn(row, "Re-arm warm-up",
                  app.dev_rearm_warmup).pack(side="left", padx=(0, 4))
        self._btn(row, "Reset today's totals",
                  app.dev_reset_today).pack(side="left")

        self._section(parent, "Sounds")
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w")
        for name in ("warmup", "play", "work", "warn", "lock", "done"):
            self._btn(row, name,
                      lambda n=name: app.chime(n)).pack(side="left", padx=(0, 3))

        self._section(parent, "Log")
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w")
        self._btn(row, "+1 day",
                  lambda: self.dev_add_days(1)).pack(side="left", padx=(0, 4))
        self._btn(row, "+7 days",
                  lambda: self.dev_add_days(7)).pack(side="left", padx=(0, 4))
        self._btn(row, "Delete log", self.dev_clear_log).pack(side="left")
        tk.Label(parent, bg=PANEL, fg=DIM, font=("Segoe UI", 8),
                 text="Each click plants another day in the field."
                 ).pack(anchor="w", pady=(4, 0))

        self._section(parent, "Shortcut")
        self._flag(parent, "Skip the escape phrase (testing only)",
                   "dev_no_phrase")

        self.poll_state()

    def poll_state(self):
        if not getattr(self, "dev_on", False):
            return
        app = self.app
        try:
            watcher = app.watcher
            record = app.roll_day()
            cap = app.cap_seconds()
            lines = [
                "phase       %s%s" % (app.phase,
                                      "  (standby)" if app.standby else ""),
                "remaining   %s" % app.mmss(app._held if app.held()
                                            else app.remaining()),
                "held        %s" % (", ".join(
                    n for n, v in (("paused", app.paused),
                                   ("idle", app.idle_held),
                                   ("focus", app.focus_held),
                                   ("game-closed", app.autopaused)) if v)
                    or "no"),
                "locking     %s   borrowed %ds" % (app.locking, app.borrowed),
                "game        %s  pid=%s  running=%s" % (
                    watcher.active or "-", watcher.pid, watcher.is_running()),
                "foreground  %s" % (foreground_process() or "-"),
                "idle        %.0fs" % idle_seconds(),
                "today       play %.1fm / work %.1fm" % (
                    record.get("play", 0) / 60.0, record.get("work", 0) / 60.0),
                "cap         %s" % ("%d min (%s)" % (
                    cap // 60, "reached" if app.cap_hit() else "ok")
                    if cap else "off"),
                "blooms      %d of %d" % (
                    min(len(CORNER_LAYOUT) * 2,
                        int(record.get("work", 0) // 360)),
                    len(CORNER_LAYOUT) * 2),
                "warm-up     %s" % ("due" if app.warmup_due() else "done today"),
                "phrase gate %s" % ("on" if app.locked_in() else "off"),
            ]
            self.state_box.configure(text="\n".join(lines))
        except Exception as exc:
            self.state_box.configure(text="state error: %s" % exc)
        if not self.closed:
            self.after(500, self.poll_state)

    def dev_apply_today(self, which):
        var = self.dev_played if which == "play" else self.dev_worked
        try:
            minutes = int(var.get())
        except ValueError:
            return
        if which == "play":
            self.app.dev_set_today(play_minutes=minutes)
        else:
            self.app.dev_set_today(work_minutes=minutes)

    def dev_bump_work(self, minutes):
        record = self.app.roll_day()
        record["work"] = max(0, record.get("work", 0) + minutes * 60)
        save_config(self.app.cfg)
        self.dev_worked.set(str(int(record["work"] // 60)))

    def dev_add_days(self, count):
        """Append fake days before the earliest one, so the field fills up."""
        import datetime
        existing = set()
        if os.path.exists(LOG_PATH):
            try:
                with open(LOG_PATH, "r", encoding="utf-8", newline="") as fh:
                    for row in csv.DictReader(fh):
                        if row.get("date"):
                            existing.add(row["date"])
            except Exception:
                pass
        try:
            anchor = datetime.date.fromisoformat(min(existing)) if existing \
                else datetime.date.today() + datetime.timedelta(days=1)
        except ValueError:
            anchor = datetime.date.today()

        try:
            fresh = not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0
            with open(LOG_PATH, "a", encoding="utf-8", newline="") as fh:
                writer = csv.writer(fh)
                if fresh:
                    writer.writerow(["date", "finished", "phase", "minutes",
                                     "game"])
                added = 0
                step = 1
                while added < count:
                    day = (anchor - datetime.timedelta(days=step)).isoformat()
                    step += 1
                    if day in existing:
                        continue
                    existing.add(day)
                    added += 1
                    writer.writerow([day, "10:00", "warmup", 10,
                                     "VintageStory.exe"])
                    writer.writerow([day, "12:00", "play",
                                     random.choice([25, 40, 55, 70]),
                                     "VintageStory.exe"])
                    writer.writerow([day, "14:00", "work",
                                     random.choice([20, 45, 60, 85, 110]), ""])
        except Exception:
            pass
        self.draw_chart()
        self.draw_garden()

    def dev_clear_log(self):
        try:
            if os.path.exists(LOG_PATH):
                os.remove(LOG_PATH)
        except Exception:
            pass
        self.draw_chart()
        self.draw_garden()

    def _build_alerts(self):
        parent = self.tab_alert
        self._section(parent, "Tell someone if I bail early")
        self._flag(parent, "Send a message when I use the escape phrase",
                   "alert_on_escape")

        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w", fill="x", pady=(6, 0))
        tk.Label(row, text="Their name", bg=PANEL, fg=DIM,
                 font=("Segoe UI", 8)).pack(side="left")
        name_var = tk.StringVar(value=str(self.app.cfg.get("alert_name", "")))
        tk.Entry(row, textvariable=name_var, width=24, bg=FIELD, fg=INK,
                 relief="flat", insertbackground="#c89b4a").pack(side="left",
                                                                 padx=8, ipady=3)
        self.strs["alert_name"] = name_var

        self._section(parent, "How to send it")
        picker = tk.Frame(parent, bg=PANEL)
        picker.pack(anchor="w")
        self.method = tk.StringVar(
            value=str(self.app.cfg.get("alert_method", "ntfy")))
        self.strs["alert_method"] = self.method
        for value, text in (("ntfy", "Phone push"), ("discord", "Discord"),
                            ("email", "Email")):
            tk.Radiobutton(picker, text=text, value=value, variable=self.method,
                           bg=PANEL, fg=INK, selectcolor=FIELD,
                           activebackground=PANEL, activeforeground=INK,
                           font=("Segoe UI", 9), highlightthickness=0, bd=0,
                           command=self._show_method).pack(side="left",
                                                           padx=(0, 14))

        self.panels = {}

        # ---------------- ntfy ----------------
        outer, card = self._card(parent, "ntfy.sh - free, no account needed")
        self.panels["ntfy"] = outer
        self._steps(card, [
            "Pick a topic below. Anyone who knows the word can read the "
            "messages, so make it unguessable.",
            "Have them install the free app \u201cntfy\u201d from the App Store "
            "or Google Play.",
            "In the app, tap + and Subscribe to topic, then type exactly the "
            "same word.",
            "Send a test below. It should land on their phone in seconds.",
        ])
        trow = tk.Frame(card, bg="#100c07")
        trow.pack(anchor="w", fill="x", pady=(10, 0))
        tk.Label(trow, text="Topic", bg="#100c07", fg=DIM,
                 font=("Segoe UI", 8)).pack(side="left")
        topic_var = tk.StringVar(
            value=str(self.app.cfg.get("alert_ntfy_topic", "")))
        tk.Entry(trow, textvariable=topic_var, width=22, bg=FIELD, fg=INK,
                 relief="flat", insertbackground="#c89b4a",
                 font=("Consolas", 9)).pack(side="left", padx=6, ipady=3)
        self.strs["alert_ntfy_topic"] = topic_var
        self._btn(trow, "Generate", self._gen_topic).pack(side="left")

        lrow = tk.Frame(card, bg="#100c07")
        lrow.pack(anchor="w", fill="x", pady=(8, 0))
        self.ntfy_link = tk.Label(lrow, text="", bg="#100c07", fg="#63917c",
                                  font=("Consolas", 9))
        self.ntfy_link.pack(side="left")
        self._btn(lrow, "Copy link", self._copy_link).pack(side="left", padx=8)
        topic_var.trace_add("write", lambda *_: self._refresh_link())
        self._refresh_link()

        # ---------------- discord ----------------
        outer, card = self._card(parent, "Discord webhook")
        self.panels["discord"] = outer
        self._steps(card, [
            "In the server, open Server Settings, then Integrations, "
            "then Webhooks.",
            "Click New Webhook, choose which channel it posts to, and give "
            "it a name.",
            "Click Copy Webhook URL and paste it below.",
        ])
        tk.Label(card, text="Webhook URL", bg="#100c07", fg=DIM,
                 font=("Segoe UI", 8)).pack(anchor="w", pady=(10, 2))
        hook_var = tk.StringVar(value=str(self.app.cfg.get("alert_webhook", "")))
        tk.Entry(card, textvariable=hook_var, width=46, bg=FIELD, fg=INK,
                 relief="flat", insertbackground="#c89b4a",
                 font=("Consolas", 8)).pack(anchor="w", ipady=3)
        self.strs["alert_webhook"] = hook_var

        # ---------------- email ----------------
        outer, card = self._card(parent, "Email")
        self.panels["email"] = outer
        self._steps(card, [
            "For Gmail, turn on 2-Step Verification, then go to App "
            "passwords and create one for PlayWork.",
            "Use that 16-character password below, never your real one.",
            "It is stored as plain text in playwork.json, so prefer phone "
            "push or Discord if you can.",
        ])
        grid = tk.Frame(card, bg="#100c07")
        grid.pack(anchor="w", pady=(10, 0))
        for i, (label, key, hide) in enumerate((
                ("SMTP host", "alert_smtp_host", None),
                ("Port", "alert_smtp_port", None),
                ("Username", "alert_smtp_user", None),
                ("App password", "alert_smtp_pass", "*"),
                ("Send to", "alert_email_to", None))):
            tk.Label(grid, text=label, bg="#100c07", fg=DIM,
                     font=("Segoe UI", 8)).grid(row=i, column=0, sticky="w",
                                                pady=2)
            var = tk.StringVar(value=str(self.app.cfg.get(key, "")))
            tk.Entry(grid, textvariable=var, width=30, bg=FIELD, fg=INK,
                     relief="flat", insertbackground="#c89b4a", show=hide,
                     font=("Consolas", 9)).grid(row=i, column=1, padx=8, pady=2,
                                                ipady=2)
            self.strs[key] = var

        bar = tk.Frame(parent, bg=PANEL)
        bar.pack(anchor="w", fill="x", pady=(14, 4))
        self._btn(bar, "Send a test message", self.send_test).pack(side="left")
        self.test_status = tk.Label(bar, bg=PANEL, fg=DIM,
                                    font=("Segoe UI", 8), text="")
        self.test_status.pack(side="left", padx=10)

        self._show_method()

    def _show_method(self):
        for key, panel in self.panels.items():
            panel.pack_forget()
        chosen = self.panels.get(self.method.get())
        if chosen:
            chosen.pack(fill="x", pady=(10, 0))

    def _gen_topic(self):
        word = "playwork-" + "".join(
            random.choice("abcdefghijkmnpqrstuvwxyz23456789") for _ in range(8))
        self.strs["alert_ntfy_topic"].set(word)

    def _refresh_link(self):
        topic = self.strs["alert_ntfy_topic"].get().strip()
        self.ntfy_link.configure(
            text="ntfy.sh/" + topic if topic else "pick a topic first",
            fg="#63917c" if topic else DIM)

    def _copy_link(self):
        topic = self.strs["alert_ntfy_topic"].get().strip()
        if not topic:
            return
        self.clipboard_clear()
        self.clipboard_append("https://ntfy.sh/" + topic)
        self.test_status.configure(fg="#63917c", text="link copied")
        self.after(2000, lambda: self.test_status.configure(text=""))

    def _build_startup(self):
        parent = self.tab_start
        self._section(parent, "Startup")
        self.autostart = tk.BooleanVar(value=autostart_enabled())
        self._check(parent, "Start PlayWork when Windows starts", self.autostart)
        self._flag(parent, "Wait for a watched game before starting the timer",
                   "standby_until_game")

        self._section(parent, "Overlay")
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w", fill="x")
        tk.Label(row, text="Transparency", bg=PANEL, fg=INK,
                 font=("Segoe UI", 9)).pack(side="left")
        self.opacity = tk.DoubleVar(
            value=float(self.app.cfg.get("opacity", 0.88)))
        tk.Scale(row, from_=0.25, to=1.0, resolution=0.05,
                 variable=self.opacity, orient="horizontal", length=180,
                 bg=PANEL, fg=INK, troughcolor=FIELD, highlightthickness=0,
                 bd=0, sliderrelief="flat", activebackground="#c89b4a",
                 font=("Consolas", 7),
                 command=lambda v: self.app.ui.set_opacity(v)).pack(side="left",
                                                                    padx=8)
        tk.Label(parent, bg=PANEL, fg=DIM, font=("Segoe UI", 8),
                 text="Slide it and the overlay updates as you go."
                 ).pack(anchor="w")

        self._flag(parent, "Show flowers on the overlay", "show_flowers")
        self._flag(parent, "Keep the overlay pinned to the game window",
                   "follow_window")
        self._flag(parent, "Let clicks pass through it", "click_through")
        tk.Label(parent, bg=PANEL, fg=DIM, font=("Segoe UI", 8), justify="left",
                 wraplength=380,
                 text="Clicks only pass through while the game itself is "
                      "focused. Alt-tab away and the overlay is clickable "
                      "again, so you can never lock yourself out of the menu."
                 ).pack(anchor="w", pady=(0, 4))
        self._flag(parent, "Play chimes", "sound")

        self._section(parent, "Escape phrase")
        tk.Label(parent, bg=PANEL, fg=DIM, font=("Segoe UI", 8), justify="left",
                 wraplength=380,
                 text="What you type to quit during a work block. Make it long "
                      "if you don't trust yourself.").pack(anchor="w")
        self._entry(parent, "", "unlock_phrase", width=44)

        self._section(parent, "Reset")
        row = tk.Frame(parent, bg=PANEL)
        row.pack(anchor="w")
        self._btn(row, "Clear history", self.confirm_clear).pack(side="left",
                                                                 padx=(0, 6))
        danger = self._btn(row, "Reset everything", self.confirm_reset)
        danger.configure(bg="#3a1d14", activebackground="#4d281b")
        danger.pack(side="left")
        self.reset_note = tk.Label(parent, bg=PANEL, fg=DIM,
                                   font=("Segoe UI", 8), justify="left",
                                   wraplength=380,
                                   text="Clear history wipes the log and the "
                                        "field, keeping your settings. Reset "
                                        "everything also restores defaults and "
                                        "removes the Windows startup entry.")
        self.reset_note.pack(anchor="w", pady=(5, 0))

    # ---- watch list ----

    def confirm_clear(self):
        self._confirm("Delete the block log?",
                      "The field and the 7-day chart start over. "
                      "Settings and games are kept.",
                      "Clear history", self._do_clear)

    def confirm_reset(self):
        self._confirm("Reset everything?",
                      "Settings go back to defaults, the log is deleted, and "
                      "PlayWork is removed from Windows startup. "
                      "This cannot be undone.",
                      "Reset everything", self._do_reset)

    def _confirm(self, title, body, verb, action):
        win = tk.Toplevel(self)
        win.title("PlayWork")
        win.configure(bg=PANEL, padx=18, pady=16)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        tk.Label(win, text=title, bg=PANEL, fg=INK,
                 font=("Segoe UI", 10, "bold")).pack(anchor="w")
        tk.Label(win, text=body, bg=PANEL, fg=DIM, font=("Segoe UI", 9),
                 justify="left", wraplength=320).pack(anchor="w", pady=(6, 12))
        bar = tk.Frame(win, bg=PANEL)
        bar.pack(anchor="e")
        self._btn(bar, "Cancel", win.destroy).pack(side="left", padx=(0, 6))
        go = self._btn(bar, verb, lambda: (win.destroy(), action()))
        go.configure(bg="#8c3a22", fg="#ffffff", activebackground="#a8492c")
        go.pack(side="left")

    def _do_clear(self):
        ok = self.app.clear_history()
        self.reset_note.configure(
            fg="#63917c" if ok else "#b04a2c",
            text="History cleared." if ok else "Could not delete the log file.")
        self.draw_chart()
        self.draw_garden()

    def _do_reset(self):
        self.app.factory_reset()
        self.games = list(self.app.cfg.get("watched_games", []))
        self.refresh_list()
        self.autostart.set(False)
        self.reset_note.configure(
            fg="#63917c",
            text="Everything reset. Close and reopen Settings to see the "
                 "restored defaults.")
        self.draw_chart()
        self.draw_garden()

    def poll_detected(self):
        try:
            watched = {p.lower() for p in self.games}
            live = set()
            for proc in psutil.process_iter(["name"]):
                try:
                    name = (proc.info["name"] or "").lower()
                except Exception:
                    continue
                if name in watched:
                    live.add(name)
            if live:
                self.detected.configure(
                    fg="#63917c",
                    text="\u25cf  running now: " + ", ".join(sorted(live)))
            else:
                self.detected.configure(
                    fg=DIM, text="\u25cb  none of these are running right now")
        except Exception:
            pass
        if not self.closed:
            self.after(2000, self.poll_detected)

    def refresh_list(self):
        self.box.delete(0, "end")
        for i, name in enumerate(self.games):
            launcher = name.lower() in LAUNCHER_EXES
            self.box.insert("end", ("!  " if launcher else "   ") + name)
            if launcher:
                self.box.itemconfigure(i, foreground="#c89b4a")

    def add_name(self, name):
        name = name.strip()
        if not name:
            return
        if not name.lower().endswith(".exe"):
            name += ".exe"
        if name.lower() in [g.lower() for g in self.games]:
            return
        self.games.append(name)
        self.refresh_list()
        if name.lower() in LAUNCHER_EXES:
            self.detected.configure(
                fg="#c89b4a",
                text="!  %s is a launcher, not a game. Watch the game's own "
                     "exe instead." % name)

    def remove_selected(self):
        for index in reversed(list(self.box.curselection())):
            del self.games[index]
        self.refresh_list()

    def add_typed(self):
        self._prompt("Executable name", "e.g. factorio.exe", self.add_name)

    def add_running(self):
        rows = list_windowed_processes()
        win = tk.Toplevel(self)
        win.title("Add a running program")
        win.configure(bg=PANEL, padx=14, pady=12)
        win.attributes("-topmost", True)
        tk.Label(win, bg=PANEL, fg=INK, font=("Segoe UI", 9),
                 text="Biggest window first - your game is usually at the top."
                 ).pack(anchor="w")
        box = tk.Listbox(win, width=56, height=min(12, max(4, len(rows))),
                         bg=FIELD, fg=INK, relief="flat", activestyle="none",
                         selectbackground="#a86a3d", selectforeground="#ffffff",
                         selectmode="extended", font=("Consolas", 9))
        box.pack(pady=8, fill="both", expand=True)
        for name, title in rows:
            short = title if len(title) <= 32 else title[:31] + "\u2026"
            box.insert("end", "%-26s %s" % (name, short))

        def take(_=None):
            for index in box.curselection():
                self.add_name(rows[index][0])
            win.destroy()

        box.bind("<Double-Button-1>", take)
        bar = tk.Frame(win, bg=PANEL)
        bar.pack(anchor="e")
        self._btn(bar, "Cancel", win.destroy).pack(side="left", padx=4)
        self._btn(bar, "Add selected", take).pack(side="left")

    def _prompt(self, title, hint, callback):
        win = tk.Toplevel(self)
        win.title(title)
        win.configure(bg=PANEL, padx=14, pady=12)
        win.attributes("-topmost", True)
        tk.Label(win, bg=PANEL, fg=DIM, font=("Segoe UI", 8),
                 text=hint).pack(anchor="w")
        entry = tk.Entry(win, width=30, bg=FIELD, fg=INK, relief="flat",
                         insertbackground="#c89b4a")
        entry.pack(pady=6, ipady=3)
        entry.focus_set()

        def done(_=None):
            callback(entry.get())
            win.destroy()

        entry.bind("<Return>", done)
        self._btn(win, "Add", done).pack(anchor="e")

    def apply_preset(self, play, work):
        self.vars["play_minutes"].set(str(play))
        self.vars["work_minutes"].set(str(work))

    # ---- test send ----

    def send_test(self):
        self.apply(quiet=True)
        if not alerts_ready(self.app.cfg):
            self.test_status.configure(fg="#b04a2c",
                                       text="fill in the fields for that method first")
            return
        self.test_status.configure(fg=DIM, text="sending...")
        self._test = {}
        send_alert(self.app.cfg,
                   "PlayWork test message. If you got this, the alert works.",
                   self._test)
        self._await_test()

    def _await_test(self, n=0):
        status = self._test.get("status")
        if status:
            ok, detail = status
            if ok == "ok":
                self.test_status.configure(fg="#63917c", text="sent")
            else:
                self.test_status.configure(fg="#b04a2c", text="failed: " + detail)
        elif n < 60 and not self.closed:
            self.after(250, lambda: self._await_test(n + 1))
        else:
            self.test_status.configure(fg="#b04a2c", text="timed out")

    # ---- save ----

    def apply(self, quiet=False):
        cfg = self.app.cfg
        if not self.games:
            self.status.configure(text="Add at least one game.", fg="#b04a2c")
            return
        cfg["watched_games"] = list(self.games)
        cfg["work_apps"] = list(getattr(self, "work_apps", []))
        for key, var in self.vars.items():
            value = var.get()
            if isinstance(value, bool):
                cfg[key] = value
            else:
                try:
                    cfg[key] = int(str(value).strip())
                except ValueError:
                    pass
        for key, var in self.strs.items():
            cfg[key] = var.get().strip()
        try:
            cfg["opacity"] = round(float(self.opacity.get()), 2)
        except (AttributeError, ValueError):
            pass
        if not cfg.get("unlock_phrase"):
            cfg["unlock_phrase"] = "let me out"
        save_config(cfg)
        set_autostart(self.autostart.get())
        self.app.apply_config()
        try:
            self.draw_chart()
            self.draw_garden()
        except Exception:
            pass
        if not quiet:
            self.status.configure(text="Saved.", fg="#63917c")
            self.after(2500, lambda: self.status.configure(text=""))

    def close(self):
        self.app.settings_win = None
        self.closed = True
        self.destroy()


# ==========================================================================
# session
# ==========================================================================

class App:
    def __init__(self):
        self.first_run = not os.path.exists(CONFIG_PATH)
        self.cfg = load_config()
        self.watcher = GameWatcher(self.cfg["watched_games"],
                                   self.cfg.get("require_window", True))
        self.settings_win = None

        self.phase = "play"
        self.paused = False
        self.warned = False
        self.locking = False
        self.standby = bool(self.cfg.get("standby_until_game", True))
        self.lock_deadline = 0.0
        self.gone_since = 0.0
        self.autopaused = False
        self.idle_held = False
        self.focus_held = False
        self.started_at = 0.0
        self.last_accrue = 0.0
        self.lock_tries = 0
        self.lock_failed = ""
        self.borrowed = 0
        self._held = 0.0
        self.last_poll = 0.0
        self.last_save = time.time()
        self.ends_at = time.time() + self.minutes("play") * 60

        self.ui = Overlay(self)
        self.ui.after(200, self.tick)
        if self.first_run:
            self.ui.after(700, self.open_settings)

    # ---- helpers ----

    def minutes(self, phase):
        key = {"play": "play_minutes", "work": "work_minutes",
               "warmup": "warmup_minutes"}[phase]
        return max(1, int(self.cfg.get(key, DEFAULTS[key])))

    # ---- daily tallies ----

    def roll_day(self):
        """Reset today's totals when the date changes."""
        today = time.strftime("%Y-%m-%d")
        record = self.cfg.get("today") or {}
        if record.get("date") != today:
            self.cfg["today"] = {"date": today, "play": 0, "work": 0}
            save_config(self.cfg)
        return self.cfg["today"]

    def accrue(self, now):
        """Add elapsed time to today's tally for the running phase."""
        if self.last_accrue and not self.held():
            delta = now - self.last_accrue
            if 0 < delta < 30:          # ignore sleep/hibernate jumps
                record = self.roll_day()
                bucket = "play" if self.phase == "play" else "work"
                record[bucket] = record.get(bucket, 0) + delta
        self.last_accrue = now

    def phase_label(self):
        return {"play": "PLAY", "work": "WORK",
                "warmup": "WARM-UP"}.get(self.phase, self.phase.upper())

    def held(self):
        return self.paused or self.autopaused or self.idle_held or self.focus_held

    def played_today(self):
        return self.roll_day().get("play", 0)

    def cap_seconds(self):
        try:
            return max(0, int(self.cfg.get("daily_play_cap_minutes", 0))) * 60
        except (TypeError, ValueError):
            return 0

    def cap_hit(self):
        cap = self.cap_seconds()
        return bool(cap and self.played_today() >= cap)

    def warmup_due(self):
        if not self.cfg.get("warmup_enabled", True):
            return False
        return self.cfg.get("warmup_date", "") != time.strftime("%Y-%m-%d")

    @staticmethod
    def mmss(seconds):
        seconds = max(0, int(round(seconds)))
        return "%02d:%02d" % (seconds // 60, seconds % 60)

    def exit_grace(self):
        try:
            return max(3, int(self.cfg.get("exit_grace_seconds", 20)))
        except (TypeError, ValueError):
            return 20

    def remaining(self):
        return self.ends_at - time.time()

    def remember_position(self, x, y):
        rect = self.watcher.rect() if self.cfg.get("follow_window") else None
        if rect:
            self.cfg["overlay_offset"] = [int(x - rect[2]), int(y - rect[1])]
        else:
            self.cfg["fallback_position"] = [int(x), int(y)]

    def apply_config(self):
        """Called after settings are saved."""
        self.watcher.require_window = bool(self.cfg.get("require_window", True))
        if self.focus_held and not (self.cfg.get("gate_work_on_focus")
                                    and self.cfg.get("work_apps")):
            self.focus_held = False
            if not self.held():
                self.ends_at = time.time() + self._held
        if self.idle_held and not self.cfg.get("idle_hold_seconds"):
            self.idle_held = False
            if not self.held():
                self.ends_at = time.time() + self._held
        self.watcher.set_names(self.cfg["watched_games"])
        self.watcher.refresh()
        try:
            self.ui.attributes("-alpha", float(self.cfg.get("opacity", 0.88)))
        except Exception:
            pass
        if not self.cfg.get("standby_until_game", True) and self.standby:
            self.wake()
        self.ui.build_menu()

    def open_settings(self):
        if self.settings_win is not None:
            try:
                self.settings_win.lift()
                return
            except Exception:
                pass
        self.settings_win = Settings(self)

    # ---- phase machine ----

    def switch_to(self, phase, chime=True, log_previous=True):
        now = time.time()
        self.accrue(now)
        if log_previous and self.cfg.get("log_sessions", True) and self.started_at:
            spent = now - self.started_at
            if spent > 60:
                log_block(self.phase, spent, self.watcher.active)

        # A play block can't start if you've used up the day.
        if phase == "play" and self.cap_hit():
            phase = "work"

        self.phase = phase
        self.warned = False
        self.locking = False
        self.idle_held = False
        self.focus_held = False
        self.lock_tries = 0
        self.lock_failed = ""
        self.started_at = now
        self.last_accrue = now
        span = self.minutes(phase) * 60
        if phase == "play" and self.borrowed:
            span = max(60, span - self.borrowed)
            self.borrowed = 0
        self.ends_at = now + span

        if phase == "warmup":
            self.cfg["warmup_date"] = time.strftime("%Y-%m-%d")
            save_config(self.cfg)
        elif phase == "play":
            if self.cfg.get("lock_mode") == "freeze" and self.watcher.frozen:
                self.watcher.thaw()
            elif self.cfg.get("relaunch_on_play") and not self.watcher.is_running():
                self.watcher.launch()
        if chime:
            self.chime(phase if phase in CHIME else "work")

    def chime(self, name):
        chime(name, float(self.cfg.get("volume", 0.5)),
              bool(self.cfg.get("sound", True)))

    # ---- dev helpers ----

    def dev_nudge(self, seconds):
        """Jump the clock. Time skipped forward is banked as if it were spent,
        so today's totals, the log and the blooms all react."""
        before = self._held if self.held() else self.remaining()
        if seconds in (5, 30):
            target = float(seconds)
            if self.held():
                self._held = target
            else:
                self.ends_at = time.time() + target
        else:
            if self.held():
                self._held = max(1.0, self._held + seconds)
            else:
                self.ends_at = max(time.time() + 1, self.ends_at + seconds)
        after = self._held if self.held() else self.remaining()

        skipped = before - after
        if skipped > 0:
            self.credit(skipped)
            self.started_at -= skipped      # so the logged block is full length

    def credit(self, seconds):
        """Add seconds to today's tally for the current phase."""
        record = self.roll_day()
        bucket = "play" if self.phase == "play" else "work"
        record[bucket] = max(0, record.get(bucket, 0) + seconds)
        save_config(self.cfg)

    def dev_set_today(self, play_minutes=None, work_minutes=None):
        record = self.roll_day()
        if play_minutes is not None:
            record["play"] = max(0, play_minutes) * 60
        if work_minutes is not None:
            record["work"] = max(0, work_minutes) * 60
        save_config(self.cfg)

    def dev_phase(self, phase):
        self.standby = False
        self.paused = self.idle_held = self.focus_held = self.autopaused = False
        self.switch_to(phase, chime=False, log_previous=False)
        self.ui.build_menu()

    def clear_history(self):
        """Delete the block log. Settings are untouched."""
        try:
            if os.path.exists(LOG_PATH):
                os.remove(LOG_PATH)
        except Exception:
            return False
        self.cfg["today"] = {"date": time.strftime("%Y-%m-%d"),
                             "play": 0, "work": 0}
        save_config(self.cfg)
        return True

    def factory_reset(self):
        """Back to a fresh install: settings, history and the startup entry."""
        try:
            if os.path.exists(LOG_PATH):
                os.remove(LOG_PATH)
        except Exception:
            pass
        set_autostart(False)
        fresh = json.loads(json.dumps(DEFAULTS))
        self.cfg.clear()
        self.cfg.update(fresh)
        self.cfg["today"] = {"date": time.strftime("%Y-%m-%d"),
                             "play": 0, "work": 0}
        save_config(self.cfg)
        if self.watcher.frozen:
            self.watcher.thaw()
        self.watcher = GameWatcher(self.cfg["watched_games"],
                                   self.cfg.get("require_window", True))
        self.watcher.refresh()
        self.sleep()
        return True

    def dev_rearm_warmup(self):
        self.cfg["warmup_date"] = ""
        save_config(self.cfg)

    def dev_reset_today(self):
        self.cfg["today"] = {"date": time.strftime("%Y-%m-%d"),
                             "play": 0, "work": 0}
        save_config(self.cfg)

    def extend(self, minutes):
        """Add time to the current work block. Costs nothing - it's more work."""
        if self.held():
            self._held += minutes * 60
        else:
            self.ends_at += minutes * 60
        self.ui.build_menu()

    def borrow(self):
        """Five more minutes now, taken off the next play block."""
        extra = max(1, int(self.cfg.get("borrow_minutes", 5))) * 60
        self.ends_at += extra
        self.borrowed += extra
        self.ui.build_menu()

    def skip(self):
        target = "play" if self.phase in ("work", "warmup") else "work"

        def do():
            if target == "play" and self.phase in ("work", "warmup"):
                log_block("skipped_" + self.phase,
                          max(0, time.time() - self.started_at),
                          self.watcher.active)
            self.switch_to(target, chime=False)
            self.ui.build_menu()

        self.require_phrase("skip to " + target, do)

    def toggle_pause(self):
        if self.autopaused:
            self.autopaused = False
            self.ends_at = time.time() + self._held
            self.ui.build_menu()
            return
        if self.paused:
            self.ends_at = time.time() + self._held
            self.paused = False
        else:
            self._held = max(0, self.remaining())
            self.paused = True
        self.ui.build_menu()

    def wake(self):
        self.standby = False
        self.gone_since = 0.0
        self.autopaused = False
        if self.cap_hit():
            self.switch_to("work", log_previous=False)
        elif self.warmup_due():
            self.switch_to("warmup", log_previous=False)
        else:
            self.switch_to("play", log_previous=False)
        self.ui.build_menu()

    def request_sleep(self):
        self.require_phrase("stop and go idle", self.sleep)

    def sleep(self):
        self.standby = True
        self.paused = False
        self.autopaused = False
        self.locking = False
        self.phase = "play"
        self.gone_since = 0.0
        if self.watcher.frozen:
            self.watcher.thaw()
        self.ui.build_menu()

    def enforce_lock(self):
        mode = self.cfg.get("lock_mode", "close")
        if mode == "none" or not self.watcher.is_running():
            self.lock_tries = 0
            return
        if mode == "freeze":
            if not self.watcher.frozen:
                self.watcher.freeze()
                self.chime("lock")
            return
        # Something that ignores WM_CLOSE would otherwise be hammered every
        # two seconds forever, chiming each time. Give up after a few tries.
        if self.lock_tries >= 5:
            self.lock_failed = self.watcher.active or "it"
            return
        self.watcher.close()
        if self.lock_tries == 0:
            self.chime("lock")
        self.lock_tries += 1

    # ---- main loop ----

    def tick(self):
        now = time.time()

        if now - self.last_save > 60:
            self.last_save = now
            save_config(self.cfg)

        if now - self.last_poll > 2.0:
            self.last_poll = now
            self.watcher.refresh()
            if self.cfg.get("click_through", True):
                active = (self.watcher.active or "").lower()
                self.ui.set_click_through(
                    bool(active and foreground_process() == active))
            else:
                self.ui.set_click_through(False)

            if self.standby:
                if self.watcher.is_running():
                    self.wake()
            elif self.phase in ("work", "warmup") and not self.locking:
                self.enforce_lock()
            elif self.phase == "play" and not self.locking:
                mode = self.cfg.get("on_game_exit", "continue")
                if self.watcher.is_running():
                    self.gone_since = 0.0
                    if self.autopaused:      # you're back - pick up where you left off
                        self.autopaused = False
                        self.ends_at = time.time() + self._held
                elif mode != "continue":
                    if not self.gone_since:
                        self.gone_since = now
                    elif now - self.gone_since >= self.exit_grace():
                        if mode == "reset":
                            self.sleep()
                        elif not self.autopaused:
                            self.autopaused = True
                            self._held = max(0, self.remaining())
                else:
                    if not self.gone_since:
                        self.gone_since = now

        # --- idle: hold the clock when you walk away ---
        try:
            idle_limit = max(0, int(self.cfg.get("idle_hold_seconds", 300)))
        except (TypeError, ValueError):
            idle_limit = 0
        idle_gate = bool(not self.standby and idle_limit and not self.paused)
        if idle_gate:
            idle = idle_seconds()
            if idle >= idle_limit and not self.idle_held:
                if not self.held():          # capture the real figure once
                    self._held = max(0, self.remaining())
                self.idle_held = True
            elif idle < 2 and self.idle_held:
                self.idle_held = False
                if not self.held():
                    self.ends_at = time.time() + self._held
        elif self.idle_held:
            # setting switched off, or phase changed - never stay stuck
            self.idle_held = False
            if not self.held():
                self.ends_at = time.time() + self._held

        # --- focus gate: work time only counts in a work app ---
        focus_gate = bool(not self.standby and self.phase in ("work", "warmup")
                          and self.cfg.get("gate_work_on_focus")
                          and self.cfg.get("work_apps")
                          and not self.paused and not self.idle_held)
        if focus_gate:
            allowed = {a.lower() for a in self.cfg["work_apps"]}
            focused = foreground_process()
            if focused and focused not in allowed and not self.focus_held:
                if not self.held():
                    self._held = max(0, self.remaining())
                self.focus_held = True
            elif focused in allowed and self.focus_held:
                self.focus_held = False
                if not self.held():
                    self.ends_at = time.time() + self._held
        elif self.focus_held:
            self.focus_held = False
            if not self.held():
                self.ends_at = time.time() + self._held

        self.accrue(now)
        if log_previous and self.cfg.get("log_sessions", True) and self.started_at:
            spent = now - self.started_at
            if spent > 60:
                log_block(self.phase, spent, self.watcher.active)

        # A play block can't start if you've used up the day.
        if phase == "play" and self.cap_hit():
            phase = "work"

        self.phase = phase
        self.warned = False
        self.locking = False
        self.idle_held = False
        self.focus_held = False
        self.lock_tries = 0
        self.lock_failed = ""
        self.started_at = now
        self.last_accrue = now
        span = self.minutes(phase) * 60
        if phase == "play" and self.borrowed:
            span = max(60, span - self.borrowed)
            self.borrowed = 0
        self.ends_at = now + span

        if phase == "warmup":
            self.cfg["warmup_date"] = time.strftime("%Y-%m-%d")
            save_config(self.cfg)
        elif phase == "play":
            if self.cfg.get("lock_mode") == "freeze" and self.watcher.frozen:
                self.watcher.thaw()
            elif self.cfg.get("relaunch_on_play") and not self.watcher.is_running():
                self.watcher.launch()
        if chime:
            self.chime(phase if phase in CHIME else "work")

    def chime(self, name):
        chime(name, float(self.cfg.get("volume", 0.5)),
              bool(self.cfg.get("sound", True)))

    # ---- dev helpers ----

    def dev_nudge(self, seconds):
        """Jump the clock. Time skipped forward is banked as if it were spent,
        so today's totals, the log and the blooms all react."""
        before = self._held if self.held() else self.remaining()
        if seconds in (5, 30):
            target = float(seconds)
            if self.held():
                self._held = target
            else:
                self.ends_at = time.time() + target
        else:
            if self.held():
                self._held = max(1.0, self._held + seconds)
            else:
                self.ends_at = max(time.time() + 1, self.ends_at + seconds)
        after = self._held if self.held() else self.remaining()

        skipped = before - after
        if skipped > 0:
            self.credit(skipped)
            self.started_at -= skipped      # so the logged block is full length

    def credit(self, seconds):
        """Add seconds to today's tally for the current phase."""
        record = self.roll_day()
        bucket = "play" if self.phase == "play" else "work"
        record[bucket] = max(0, record.get(bucket, 0) + seconds)
        save_config(self.cfg)

    def dev_set_today(self, play_minutes=None, work_minutes=None):
        record = self.roll_day()
        if play_minutes is not None:
            record["play"] = max(0, play_minutes) * 60
        if work_minutes is not None:
            record["work"] = max(0, work_minutes) * 60
        save_config(self.cfg)

    def dev_phase(self, phase):
        self.standby = False
        self.paused = self.idle_held = self.focus_held = self.autopaused = False
        self.switch_to(phase, chime=False, log_previous=False)
        self.ui.build_menu()

    def clear_history(self):
        """Delete the block log. Settings are untouched."""
        try:
            if os.path.exists(LOG_PATH):
                os.remove(LOG_PATH)
        except Exception:
            return False
        self.cfg["today"] = {"date": time.strftime("%Y-%m-%d"),
                             "play": 0, "work": 0}
        save_config(self.cfg)
        return True

    def factory_reset(self):
        """Back to a fresh install: settings, history and the startup entry."""
        try:
            if os.path.exists(LOG_PATH):
                os.remove(LOG_PATH)
        except Exception:
            pass
        set_autostart(False)
        fresh = json.loads(json.dumps(DEFAULTS))
        self.cfg.clear()
        self.cfg.update(fresh)
        self.cfg["today"] = {"date": time.strftime("%Y-%m-%d"),
                             "play": 0, "work": 0}
        save_config(self.cfg)
        if self.watcher.frozen:
            self.watcher.thaw()
        self.watcher = GameWatcher(self.cfg["watched_games"],
                                   self.cfg.get("require_window", True))
        self.watcher.refresh()
        self.sleep()
        return True

    def dev_rearm_warmup(self):
        self.cfg["warmup_date"] = ""
        save_config(self.cfg)

    def dev_reset_today(self):
        self.cfg["today"] = {"date": time.strftime("%Y-%m-%d"),
                             "play": 0, "work": 0}
        save_config(self.cfg)

    def extend(self, minutes):
        """Add time to the current work block. Costs nothing - it's more work."""
        if self.held():
            self._held += minutes * 60
        else:
            self.ends_at += minutes * 60
        self.ui.build_menu()

    def borrow(self):
        """Five more minutes now, taken off the next play block."""
        extra = max(1, int(self.cfg.get("borrow_minutes", 5))) * 60
        self.ends_at += extra
        self.borrowed += extra
        self.ui.build_menu()

    def skip(self):
        target = "play" if self.phase in ("work", "warmup") else "work"

        def do():
            if target == "play" and self.phase in ("work", "warmup"):
                log_block("skipped_" + self.phase,
                          max(0, time.time() - self.started_at),
                          self.watcher.active)
            self.switch_to(target, chime=False)
            self.ui.build_menu()

        self.require_phrase("skip to " + target, do)

    def toggle_pause(self):
        if self.autopaused:
            self.autopaused = False
            self.ends_at = time.time() + self._held
            self.ui.build_menu()
            return
        if self.paused:
            self.ends_at = time.time() + self._held
            self.paused = False
        else:
            self._held = max(0, self.remaining())
            self.paused = True
        self.ui.build_menu()

    def wake(self):
        self.standby = False
        self.gone_since = 0.0
        self.autopaused = False
        if self.cap_hit():
            self.switch_to("work", log_previous=False)
        elif self.warmup_due():
            self.switch_to("warmup", log_previous=False)
        else:
            self.switch_to("play", log_previous=False)
        self.ui.build_menu()

    def request_sleep(self):
        self.require_phrase("stop and go idle", self.sleep)

    def sleep(self):
        self.standby = True
        self.paused = False
        self.autopaused = False
        self.locking = False
        self.phase = "play"
        self.gone_since = 0.0
        if self.watcher.frozen:
            self.watcher.thaw()
        self.ui.build_menu()

    def enforce_lock(self):
        mode = self.cfg.get("lock_mode", "close")
        if mode == "none" or not self.watcher.is_running():
            self.lock_tries = 0
            return
        if mode == "freeze":
            if not self.watcher.frozen:
                self.watcher.freeze()
                self.chime("lock")
            return
        # Something that ignores WM_CLOSE would otherwise be hammered every
        # two seconds forever, chiming each time. Give up after a few tries.
        if self.lock_tries >= 5:
            self.lock_failed = self.watcher.active or "it"
            return
        self.watcher.close()
        if self.lock_tries == 0:
            self.chime("lock")
        self.lock_tries += 1

    # ---- main loop ----

    def tick(self):
        now = time.time()

        if now - self.last_save > 60:
            self.last_save = now
            save_config(self.cfg)

        if now - self.last_poll > 2.0:
            self.last_poll = now
            self.watcher.refresh()
            if self.cfg.get("click_through", True):
                active = (self.watcher.active or "").lower()
                self.ui.set_click_through(
                    bool(active and foreground_process() == active))
            else:
                self.ui.set_click_through(False)

            if self.standby:
                if self.watcher.is_running():
                    self.wake()
            elif self.phase in ("work", "warmup") and not self.locking:
                self.enforce_lock()
            elif self.phase == "play" and not self.locking:
                mode = self.cfg.get("on_game_exit", "continue")
                if self.watcher.is_running():
                    self.gone_since = 0.0
                    if self.autopaused:      # you're back - pick up where you left off
                        self.autopaused = False
                        self.ends_at = time.time() + self._held
                elif mode != "continue":
                    if not self.gone_since:
                        self.gone_since = now
                    elif now - self.gone_since >= self.exit_grace():
                        if mode == "reset":
                            self.sleep()
                        elif not self.autopaused:
                            self.autopaused = True
                            self._held = max(0, self.remaining())
                else:
                    if not self.gone_since:
                        self.gone_since = now

        # --- idle: hold the clock when you walk away ---
        idle_limit = 0
        try:
            idle_limit = max(0, int(self.cfg.get("idle_hold_seconds", 300)))
        except (TypeError, ValueError):
            idle_limit = 0
        if not self.standby and idle_limit and not self.paused:
            idle = idle_seconds()
            if idle >= idle_limit and not self.idle_held:
                self.idle_held = True
                self._held = max(0, self.remaining())
            elif idle < 2 and self.idle_held:
                self.idle_held = False
                self.ends_at = time.time() + self._held

        # --- focus gate: work time only counts in a work app ---
        if (not self.standby and self.phase in ("work", "warmup")
                and self.cfg.get("gate_work_on_focus")
                and self.cfg.get("work_apps") and not self.paused
                and not self.idle_held):
            allowed = {a.lower() for a in self.cfg["work_apps"]}
            focused = foreground_process()
            if focused and focused not in allowed and not self.focus_held:
                self.focus_held = True
                self._held = max(0, self.remaining())
            elif focused in allowed and self.focus_held:
                self.focus_held = False
                self.ends_at = time.time() + self._held
        elif self.focus_held and self.phase == "play":
            self.focus_held = False

        self.accrue(now)

        if self.standby:
            watching = self.cfg.get("watched_games", [])
            note = ("Watching %d games" % len(watching) if len(watching) > 1
                    else "Waiting for %s" % (watching[0] if watching else "?"))
            self.ui.paint("standby", "STANDBY", "--:--", note,
                          int(self.roll_day().get("work", 0) // 360))
            self.reposition()
            self.ui.after(400, self.tick)
            return

        left = self._held if self.held() else self.remaining()
        warn_s = max(0, int(self.cfg.get("warn_minutes", 5))) * 60
        grace_s = max(0, int(self.cfg.get("grace_seconds", 90)))
        mood, note = self.phase, ""

        label = None
        if self.paused:
            label = self.phase_label() + " - PAUSED"
            note = "Paused - right-click to resume"
        elif self.idle_held:
            mood = "warn"
            label = self.phase_label() + " - AWAY"
            note = "You stepped away - clock held. Move the mouse to resume."
        elif self.focus_held:
            mood = "warn"
            label = self.phase_label() + " - HELD"
            note = "Clock held - focus a work app to keep counting."
        elif self.autopaused and not self.locking and self.phase == "play":
            mood = "warn"
            label = "PLAY - HELD"
            note = "Game closed - clock held. Reopen to continue."
        elif self.locking:
            mood = "locked"
            left = self.lock_deadline - now
            note = "Closing the game. Save now."
            if left <= 0:
                self.locking = False
                self.enforce_lock()
                self.switch_to("work")
                left = self.remaining()
                mood, note = "work", "Game locked until the timer runs out."
        elif self.phase == "warmup":
            if left <= 0:
                self.chime("done")
                self.switch_to("play")
                left = self.remaining()
                mood, note = "play", "Warm-up done. Go play."
            else:
                mood = "warmup"
                note = "Warm-up - %d min of work to open the day." % \
                    self.minutes("warmup")
        elif self.phase == "play":
            if left <= 0:
                self.locking = True
                self.lock_deadline = now + grace_s
                self.chime("work")
                left, mood = grace_s, "locked"
                note = "Time's up. Get somewhere safe and save."
            elif left <= warn_s:
                mood = "warn"
                note = "Wrap up - head home and save."
                if not self.warned:
                    self.warned = True
                    self.chime("warn")
            elif self.autopaused:
                note = "Game closed - clock held. Reopen to continue."
            else:
                mode = self.cfg.get("on_game_exit", "continue")
                if self.gone_since and mode == "continue":
                    note = "Game closed - clock still running."
                elif self.gone_since and mode == "reset":
                    wait = self.exit_grace() - (now - self.gone_since)
                    note = "Game closed - standby in %ds" % max(0, int(wait))
                elif self.gone_since:
                    wait = self.exit_grace() - (now - self.gone_since)
                    note = "Game closed - holding clock in %ds" % max(0, int(wait))
                elif self.watcher.active:
                    note = "Playing %s" % self.watcher.active
                else:
                    note = "No watched game running"
        else:
            if left <= 0:
                self.switch_to("play")
                left = self.remaining()
                mood, note = "play", "Back to it."
            elif self.lock_failed:
                mood = "warn"
                note = "Couldn't close %s - close it yourself." % self.lock_failed
            elif self.cap_hit():
                note = "Daily play limit reached. Done for today."
            else:
                mode = self.cfg.get("lock_mode", "close")
                note = ("Frozen until the block ends." if mode == "freeze"
                        else "Game blocked until the block ends."
                        if mode == "close" else "Working")

        label = label or {"play": "PLAY", "work": "WORK", "standby": "STANDBY",
                          "warmup": "WARM-UP", "warn": "PLAY - WRAP UP",
                          "locked": "LOCKING"}[mood]
        grown = int(self.roll_day().get("work", 0) // 360)
        self.ui.paint(mood, label, self.mmss(left), note, grown)
        self.reposition()
        self.ui.after(250, self.tick)

    def reposition(self):
        if not self.cfg.get("follow_window", True):
            return
        rect = self.watcher.rect()
        if not rect:
            return
        ox, oy = self.cfg.get("overlay_offset", [-250, 14])
        self.ui.place_at(rect[2] + int(ox), rect[1] + int(oy))

    # ---- quit ----

    def locked_in(self):
        """True when leaving the current block early should cost something."""
        if self.cfg.get("dev_no_phrase"):
            return False
        if self.standby or self.cfg.get("lock_mode") == "none":
            return False
        return self.phase in ("work", "warmup")

    def require_phrase(self, what, on_success):
        """Gate an early exit behind the phrase, and tell your contact."""
        if not self.locked_in():
            on_success()
            return

        phrase = str(self.cfg.get("unlock_phrase", "let me out"))
        will_tell = bool(self.cfg.get("alert_on_escape")
                         and alerts_ready(self.cfg))
        win = tk.Toplevel(self.ui)
        win.title("PlayWork")
        win.attributes("-topmost", True)
        win.configure(bg=PANEL, padx=18, pady=16)
        tk.Label(win, bg=PANEL, fg=INK, font=("Segoe UI", 9), justify="left",
                 wraplength=330,
                 text="You're in a %s block. To %s, type:\n\n  %s"
                      % (self.phase, what, phrase)
                      + ("\n\n%s will be told."
                         % (self.cfg.get("alert_name")
                            or "Your accountability contact")
                         if will_tell else "")).pack(anchor="w")
        entry = tk.Entry(win, width=40, bg=FIELD, fg=INK, relief="flat",
                         insertbackground="#c89b4a")
        entry.pack(pady=10, ipady=4)
        entry.focus_set()
        msg = tk.Label(win, bg=PANEL, fg="#b04a2c", font=("Segoe UI", 8), text="")
        msg.pack(anchor="w")

        def finish():
            win.destroy()
            on_success()

        def attempt(_=None):
            if entry.get().strip().lower() != phrase.strip().lower():
                msg.configure(text="Doesn't match.")
                return
            if not will_tell:
                finish()
                return
            left = self.mmss(max(0, self.remaining()))
            text = ("%s chose to %s a %s block early, with %s left. (%s)"
                    % (self.cfg.get("alert_name") and
                       os.environ.get("USERNAME", "Someone")
                       or os.environ.get("USERNAME", "Someone"),
                       what, self.phase, left, time.strftime("%a %H:%M")))
            msg.configure(fg="#c89b4a", text="Letting them know...")
            result = {}
            send_alert(self.cfg, text, result)

            def wait(n=0):
                if result.get("status") or n > 40:
                    finish()
                else:
                    win.after(250, lambda: wait(n + 1))

            wait()

        entry.bind("<Return>", attempt)
        tk.Button(win, text="Confirm", command=attempt, bg="#2a2118", fg=INK,
                  relief="flat", padx=10, pady=4).pack(anchor="e", pady=(8, 0))

    def request_quit(self):
        self.require_phrase("quit", self.shutdown)

    def shutdown(self):
        if self.watcher.frozen:
            self.watcher.thaw()
        save_config(self.cfg)
        self.ui.destroy()

    def run(self):
        self.ui.protocol("WM_DELETE_WINDOW", self.request_quit)
        self.ui.mainloop()


if __name__ == "__main__":
    if not sys.platform.startswith("win"):
        print("PlayWork is Windows only.")
        sys.exit(1)
    App().run()
