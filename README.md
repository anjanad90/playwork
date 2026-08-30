# PlayWork

Alternating play/work timer for Windows. Runs at login, waits quietly, and
starts itself when a game on its watch list launches. Pins a countdown to that
game's window, warns you before your play block ends so you can save, then
closes or freezes the game for the work block.

---

## Install

1. Install **Python 3.10+** from python.org, ticking **"Add python.exe to PATH"**.
2. Put `playwork.py` and `PlayWork.bat` in a folder you'll keep.
3. Double-click **PlayWork.bat**. First run installs `psutil` and `pywin32`,
   then opens Settings automatically.
4. On the **Games** tab, add the games you want watched. Start the game first
   and use *Add running...* — it lists open programs with the biggest window
   at the top, and a live readout confirms the process is detected. Watch the
   game's own executable, not a launcher like Steam.
5. Set your block lengths on **Timing**, then tick **Start with Windows** on
   the **Startup** tab.

Nothing is preset, so until you add a game the overlay reads **SET UP** and
the timer stays idle.

Settings and history live in `%APPDATA%\PlayWork`, not the program folder, so
they survive updates and never end up in a synced folder.

### Required game setting

Set your games to **windowed** or **borderless fullscreen**. No normal Windows
app can draw over a game in *exclusive* fullscreen; that needs DirectX hooking,
which is what anti-cheat software flags. Borderless looks identical.

---

## Using it

The overlay is a small pill. **Drag** to move, **right-click** for the menu,
**double-click** to open Settings.

| Tab | What's there |
|---|---|
| **Games** | The watch list. "Add running..." lists open programs, biggest window first. A live readout shows which watched games are running right now. |
| **Timing** | Block lengths, warning time, lock grace, and what happens when you quit a game mid-block. |
| **Limits** | Warm-up, daily play cap, idle hold, and the work-app focus gate. |
| **Stats** | Your field, plus a seven-day bar chart and a link to the CSV log. |
| **Accountability** | Optional message to someone when you bail out of a work block early. |
| **Startup** | Autostart, overlay transparency, flowers, click-through, chimes, escape phrase. |

### How a day goes

1. You launch a game. PlayWork wakes.
2. **Warm-up**: 10 minutes of work opens the day. The game closes for it.
3. **Play block**. Amber warning a few minutes before the end so you can save.
4. Grace countdown, then the game closes or freezes.
5. **Work block**. Leaving early costs the escape phrase.
6. Repeat until the daily play cap, if you set one.

### Lock modes

- **Close** (default) — sends a normal close request so the game saves through
  its own code path. Relaunching during a work block closes it again in ~2s.
- **Freeze** — suspends the process, resumes at play time. Instant resume, but
  don't use it on multiplayer servers and it holds VRAM the whole block.
- **Do nothing** — overlay only.

### Flowers

The overlay grows a floral border as you work: one bloom per 6 minutes, both
corners full at about 3.2 hours. The Stats field plants one flower per day
worked, taller with more hours, packing denser as the months add up. Both can
be turned off under Startup.

---

## Building a release exe

Run **Build-Release.bat**. It needs `playwork.ico` and `version.txt` beside it,
and writes `release\PlayWork.exe`. PyInstaller's scratch files go to `%TEMP%`.

Because settings live in `%APPDATA%`, the exe picks up the same config the
script was using. After building, open Settings from the exe and re-tick
**Start with Windows** so the startup entry points at the exe rather than the
script.

**Expect a SmartScreen warning.** Unsigned executables get "Windows protected
your PC" — click More info, then Run anyway. The only real fix is a code
signing certificate (~$100-400/year), which isn't worth it for personal use.

---

## Developer tools

The Dev tab is hidden in normal use. To bring it back, either run with
`--dev`, or add `"dev_mode": true` to `%APPDATA%\PlayWork\playwork.json`.

It has a live state readout, clock jumps that bank the skipped time, phase
forcing, day-total setters, bloom controls, chime tests, and fake-day seeding
for the field.

---

## Files

| Path | What |
|---|---|
| `playwork.py` | The program |
| `PlayWork.bat` | Launcher — installs deps, runs without a console |
| `Build-Release.bat` | Builds the exe |
| `playwork.ico`, `version.txt` | Icon and version metadata for the build |
| `%APPDATA%\PlayWork\playwork.json` | Settings |
| `%APPDATA%\PlayWork\playwork-log.csv` | Every finished block |

---

## License

MIT - see `LICENSE`.
