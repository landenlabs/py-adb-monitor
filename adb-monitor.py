#!/usr/bin/env python3
# ----------------------------------------------------------------------
# Copyright (c) 2026 LanDen Labs - Dennis Lang
# https://landenlabs.com
# ----------------------------------------------------------------------
"""adb-monitor - live CPU and memory monitor for Android devices over adb.

Shows system CPU, system memory and load average for every connected device
(USB, Wi-Fi or emulator), plus CPU, RSS, threads and (optionally) PSS for one
target app package.

Low-overhead design:
  * One long-lived `adb shell` stream per device.  A small shell loop runs on
    the device and reads /proc with shell builtins (`read`, `case`), so the
    only process forked per sample is `sleep` (plus `pidof` while the target
    app is not running).  No per-sample adb handshake, no `top`, no `dumpsys`.
  * CPU % is computed on the host from /proc jiffy deltas.
  * PSS needs `dumpsys meminfo <pkg>`, which binder-calls into the app and
    costs ~100-500 ms on the device, so it runs on a separate, slow,
    optional timer (default off).

Runs as a Qt GUI (default) or as a headless console sampler (--console).
"""

import argparse
import csv
import os
import re
import shutil
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass, fields
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import (
    QCoreApplication, QObject, QProcess, QSettings, QSize, Qt, QTimer,
    pyqtSignal,
)
from PyQt6.QtGui import (
    QAction, QColor, QFont, QIcon, QImageReader, QMovie,
    QPainter, QPainterPath, QPen, QPixmap,
)
from PyQt6.QtWidgets import (
    QApplication, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGridLayout, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox,
    QPushButton, QScrollArea, QSizePolicy, QSystemTrayIcon, QTextBrowser,
    QToolBar, QVBoxLayout, QWidget,
)

VERSION = "v6.11.19"

APP_NAME = "ADB Monitor"
WINDOW_TITLE = "%s - %s - LanDen Labs 2026" % (APP_NAME, VERSION)
SETTINGS_ORG = "LanDenLabs"
SETTINGS_APP = "adb-monitor"

DEVICE_SCAN_MS = 3000          # host-only `adb devices -l`, cheap
STREAM_RESTART_MS = 2000       # back-off before re-opening a dropped stream
HISTORY = 180                  # samples shown per sparkline (3 min at 1 s)
CSV_MAX_ROWS = 200_000         # samples kept per device for CSV export
INTERVALS = ["0.5", "1", "2", "5"]
PSS_INTERVALS = [("Off", 0), ("10 s", 10), ("30 s", 30), ("60 s", 60)]
PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.:]+$")


# ======================================================================
# adb discovery
# ======================================================================

def find_adb(explicit: str = "") -> str:
    """Locate the developer's own adb (ANDROID_HOME/SDK_ROOT, PATH, defaults).

    Using the same adb binary as Android Studio avoids two adb versions
    fighting over the adb server on port 5037.
    """
    exe = "adb.exe" if sys.platform == "win32" else "adb"
    if explicit:
        return explicit
    for var in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        root = os.environ.get(var)
        if root:
            cand = Path(root) / "platform-tools" / exe
            if cand.is_file():
                return str(cand)
    found = shutil.which("adb")
    if found:
        return found
    home = Path.home()
    defaults = [
        home / "Library" / "Android" / "sdk" / "platform-tools" / exe,
        home / "Android" / "Sdk" / "platform-tools" / exe,
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        defaults.append(Path(local) / "Android" / "Sdk" / "platform-tools" / exe)
    for cand in defaults:
        if cand.is_file():
            return str(cand)
    return "adb"


def valid_package(pkg: str) -> bool:
    """Package names are spliced into a device shell script -- whitelist chars."""
    return bool(pkg) and bool(PACKAGE_RE.match(pkg))


# ======================================================================
# Device-side script and parsers (pure functions, no Qt)
# ======================================================================

def build_stream_script(package: str, interval: str) -> str:
    """Shell loop run once per device via `adb shell`.

    Emits one frame per interval:
        @@B
        @@C cpu  user nice system idle iowait irq softirq steal ...
        @@M MemTotal: kB      (also MemAvailable, SwapTotal, SwapFree)
        @@L load1 load5 load15 running/total lastpid
        @@P pid <contents of /proc/pid/stat>   or  @@P -  (app not running)
        @@S VmRSS: kB          (also VmSwap, Threads)
        @@E
    Kept on a single line so it survives Windows command-line quoting.
    """
    proc = ""
    if valid_package(package):
        proc = (
            'if [ -z "$P" ] || [ ! -d /proc/$P ]; then '
            'P=$(pidof %s); P=${P%%%% *}; fi; '
            'if [ -n "$P" ] && read -r L 2>/dev/null < /proc/$P/stat; then '
            'echo "@@P $P $L"; '
            'while read -r K V U; do case "$K" in '
            'VmRSS:|VmSwap:|Threads:) echo "@@S $K $V";; esac; '
            'done 2>/dev/null < /proc/$P/status; '
            "else P=''; echo '@@P -'; fi; " % package
        )
    return (
        "I=%s; P=''; while :; do echo @@B; "
        'read -r L < /proc/stat; echo "@@C $L"; '
        'while read -r K V U; do case "$K" in '
        'MemTotal:|MemAvailable:|SwapTotal:|SwapFree:) echo "@@M $K $V";; '
        "esac; done < /proc/meminfo; "
        'read -r L < /proc/loadavg; echo "@@L $L"; '
        "%secho @@E; sleep $I; done" % (interval, proc)
    )


def parse_frame(lines):
    """Parse the lines between @@B and @@E into a dict of raw values."""
    raw = {"cpu": None, "mem": {}, "load": None, "pid": None,
           "ticks": None, "state": "", "status": {}}
    for line in lines:
        tag, _, rest = line.partition(" ")
        if tag == "@@C":
            parts = rest.split()
            if parts and parts[0] == "cpu":
                raw["cpu"] = [int(x) for x in parts[1:9] if x.isdigit()]
        elif tag == "@@M":
            k, _, v = rest.partition(" ")
            if v.strip().isdigit():
                raw["mem"][k.rstrip(":")] = int(v)
        elif tag == "@@L":
            parts = rest.split()
            if parts:
                try:
                    raw["load"] = float(parts[0])
                except ValueError:
                    pass
        elif tag == "@@P":
            pid, _, stat = rest.partition(" ")
            if pid.isdigit():
                raw["pid"] = int(pid)
                # comm may contain spaces/parens -- split after the last ')'.
                tail = stat[stat.rfind(")") + 1:].split()
                if len(tail) >= 13:
                    raw["state"] = tail[0]
                    raw["ticks"] = int(tail[11]) + int(tail[12])  # utime+stime
        elif tag == "@@S":
            k, _, v = rest.partition(" ")
            if v.strip().isdigit():
                raw["status"][k.rstrip(":")] = int(v)
    return raw


_PSS_RE = re.compile(r"TOTAL PSS:\s+(\d+)")
_PSS_LEGACY_RE = re.compile(r"^\s*TOTAL\s+(\d+)", re.MULTILINE)
_MEMINFO_PID_RE = re.compile(r"MEMINFO in pid (\d+)")
_SUMMARY_RE = {
    "java_kb": re.compile(r"Java Heap:\s+(\d+)"),
    "native_kb": re.compile(r"Native Heap:\s+(\d+)"),
    "graphics_kb": re.compile(r"Graphics:\s+(\d+)"),
}


def parse_meminfo(text: str):
    """Parse `dumpsys meminfo <pkg>`; returns dict or None if no process."""
    m = _PSS_RE.search(text) or _PSS_LEGACY_RE.search(text)
    if not m:
        return None
    out = {"pss_kb": int(m.group(1)), "pid": None}
    pm = _MEMINFO_PID_RE.search(text)
    if pm:
        out["pid"] = int(pm.group(1))
    for key, rx in _SUMMARY_RE.items():
        sm = rx.search(text)
        out[key] = int(sm.group(1)) if sm else None
    return out


def parse_devices(text: str):
    """Parse `adb devices -l` into [(serial, state, {key: value})]."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices") or line.startswith("*"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        attrs = {}
        for p in parts[2:]:
            k, sep, v = p.partition(":")
            if sep:
                attrs[k] = v
        out.append((parts[0], parts[1], attrs))
    return out


def transport_of(serial: str, attrs: dict) -> str:
    if serial.startswith("emulator-"):
        return "Emulator"
    if "usb" in attrs:
        return "USB"
    if ":" in serial or "_adb-tls" in serial or "._tcp" in serial:
        return "Wi-Fi"
    return "?"


@dataclass
class Sample:
    t: float = 0.0
    cpu_sys: float | None = None     # % of whole device
    mem_total_kb: int | None = None
    mem_avail_kb: int | None = None
    swap_total_kb: int | None = None
    swap_free_kb: int | None = None
    load1: float | None = None
    pid: int | None = None
    cpu_app: float | None = None     # % of whole device
    rss_kb: int | None = None
    swap_app_kb: int | None = None
    threads: int | None = None
    pss_kb: int | None = None
    java_kb: int | None = None
    native_kb: int | None = None
    graphics_kb: int | None = None

    @property
    def mem_used_kb(self):
        if self.mem_total_kb is None or self.mem_avail_kb is None:
            return None
        return self.mem_total_kb - self.mem_avail_kb


class SampleBuilder:
    """Turns raw frames into Samples by differencing jiffy counters."""

    def __init__(self):
        self.prev_cpu = None
        self.prev_pid = None
        self.prev_ticks = None

    def update(self, raw, t: float) -> Sample:
        s = Sample(t=t)
        mem = raw["mem"]
        s.mem_total_kb = mem.get("MemTotal")
        s.mem_avail_kb = mem.get("MemAvailable")
        s.swap_total_kb = mem.get("SwapTotal")
        s.swap_free_kb = mem.get("SwapFree")
        s.load1 = raw["load"]

        total_delta = None
        cpu = raw["cpu"]
        if cpu and len(cpu) >= 5:
            if self.prev_cpu and len(self.prev_cpu) == len(cpu):
                total_delta = sum(cpu) - sum(self.prev_cpu)
                idle_delta = (cpu[3] + cpu[4]) - (self.prev_cpu[3] + self.prev_cpu[4])
                if total_delta > 0:
                    s.cpu_sys = max(0.0, min(100.0, 100.0 * (1 - idle_delta / total_delta)))
            self.prev_cpu = cpu

        s.pid = raw["pid"]
        st = raw["status"]
        s.rss_kb = st.get("VmRSS")
        s.swap_app_kb = st.get("VmSwap")
        s.threads = st.get("Threads")
        ticks = raw["ticks"]
        if s.pid is not None and ticks is not None:
            if (self.prev_pid == s.pid and self.prev_ticks is not None
                    and total_delta and total_delta > 0):
                s.cpu_app = max(0.0, min(100.0, 100.0 * (ticks - self.prev_ticks) / total_delta))
            self.prev_pid, self.prev_ticks = s.pid, ticks
        else:
            self.prev_pid, self.prev_ticks = None, None
        return s


# ======================================================================
# Qt process plumbing (works under QCoreApplication too)
# ======================================================================

def run_adb(parent: QObject, adb: str, args, callback, timeout_ms: int = 15000):
    """Run a one-shot adb command asynchronously; callback(exit_code, stdout).

    Returns the QProcess; pass it to cancel_adb() to abandon it (no callback).
    """
    proc = QProcess(parent)
    proc.setProgram(adb)
    proc.setArguments(list(args))
    proc.setProperty("adb_done", False)

    def finish(code):
        if proc.property("adb_done"):
            return
        proc.setProperty("adb_done", True)
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")
        proc.deleteLater()
        callback(code, out)

    def on_error(err):
        if err == QProcess.ProcessError.FailedToStart:
            finish(-1)

    def on_timeout():
        if proc.state() != QProcess.ProcessState.NotRunning:
            proc.kill()

    proc.finished.connect(lambda code, _status: finish(code))
    proc.errorOccurred.connect(on_error)
    timer = QTimer(proc)
    timer.setSingleShot(True)
    timer.timeout.connect(on_timeout)
    timer.start(timeout_ms)
    proc.start()
    return proc


def cancel_adb(proc: QProcess):
    """Abandon a run_adb() call: no callback, process killed and reaped."""
    try:
        if proc.property("adb_done"):
            return
        proc.setProperty("adb_done", True)
        proc.kill()
        proc.waitForFinished(1000)
        proc.deleteLater()
    except RuntimeError:        # C++ object already gone
        pass


class DeviceStream(QObject):
    """One persistent `adb -s SERIAL shell <loop>` producing Samples."""

    sample = pyqtSignal(str, object)
    status = pyqtSignal(str, str)

    def __init__(self, adb: str, serial: str, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.serial = serial
        self.package = ""
        self.interval = "1"
        self._proc: QProcess | None = None
        self._buf = ""
        self._frame = None
        self._builder = SampleBuilder()
        self._running = False
        self._last_frame = 0.0
        self._restart = QTimer(self)
        self._restart.setSingleShot(True)
        self._restart.timeout.connect(self._spawn)
        self._watchdog = QTimer(self)
        self._watchdog.timeout.connect(self._check_stale)

    def start(self, package: str, interval: str):
        self.package, self.interval = package, interval
        self._running = True
        self._kill()
        self._spawn()
        self._watchdog.start(2000)

    def stop(self):
        self._running = False
        self._restart.stop()
        self._watchdog.stop()
        self._kill()

    def _kill(self):
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                proc.finished.disconnect()
            except TypeError:
                pass
            proc.kill()
            proc.waitForFinished(1000)
            proc.deleteLater()

    def _spawn(self):
        if not self._running:
            return
        self._buf, self._frame = "", None
        self._builder = SampleBuilder()
        self._last_frame = time.monotonic()
        proc = QProcess(self)
        proc.setProgram(self.adb)
        proc.setArguments(["-s", self.serial, "shell",
                           build_stream_script(self.package, self.interval)])
        proc.readyReadStandardOutput.connect(self._on_ready)
        proc.readyReadStandardError.connect(lambda: proc.readAllStandardError())
        proc.finished.connect(self._on_finished)
        self._proc = proc
        proc.start()
        self.status.emit(self.serial, "streaming")

    def _on_finished(self, *_):
        if self._proc is not None:
            self._proc.deleteLater()
            self._proc = None
        if self._running:
            self.status.emit(self.serial, "stream closed - reconnecting")
            self._restart.start(STREAM_RESTART_MS)

    def _check_stale(self):
        try:
            limit = 3 * float(self.interval) + 5
        except ValueError:
            limit = 8
        if (self._running and self._proc is not None
                and time.monotonic() - self._last_frame > limit):
            self.status.emit(self.serial, "no data - restarting stream")
            self._kill()
            self._restart.start(STREAM_RESTART_MS)

    def _on_ready(self):
        if self._proc is None:
            return
        self._buf += bytes(self._proc.readAllStandardOutput()).decode("utf-8", "replace")
        *lines, self._buf = self._buf.split("\n")
        for line in lines:
            line = line.rstrip("\r")
            if line == "@@B":
                self._frame = []
            elif line == "@@E":
                if self._frame is not None:
                    self._last_frame = time.monotonic()
                    raw = parse_frame(self._frame)
                    self.sample.emit(self.serial, self._builder.update(raw, time.time()))
                self._frame = None
            elif self._frame is not None:
                self._frame.append(line)


class Monitor(QObject):
    """Tracks devices, owns one DeviceStream per online device, polls PSS."""

    device_added = pyqtSignal(str, str, str)    # serial, state, transport
    device_state = pyqtSignal(str, str)         # serial, state
    device_info = pyqtSignal(str, dict)         # serial, {model, release, sdk, cores}
    device_removed = pyqtSignal(str)
    sample = pyqtSignal(str, object)
    status = pyqtSignal(str, str)
    adb_error = pyqtSignal(str)

    def __init__(self, adb: str, package: str = "", interval: str = "1",
                 pss_secs: int = 0, parent=None):
        super().__init__(parent)
        self.adb = adb
        self.package = package if valid_package(package) else ""
        self.interval = interval
        self.pss_secs = pss_secs
        self.paused = False
        self.devices = {}       # serial -> state
        self.info = {}          # serial -> dict
        self.streams = {}       # serial -> DeviceStream
        self._pss = {}          # serial -> parsed meminfo dict
        self._pss_busy = set()
        self._scan_busy = False
        self._calls = set()     # outstanding one-shot QProcesses
        self._scan = QTimer(self)
        self._scan.timeout.connect(self.scan)
        self._pss_timer = QTimer(self)
        self._pss_timer.timeout.connect(self._poll_pss)

    def start(self):
        self.scan()
        self._scan.start(DEVICE_SCAN_MS)
        self._apply_pss_timer()

    def shutdown(self):
        self._scan.stop()
        self._pss_timer.stop()
        for st in self.streams.values():
            st.stop()
        for proc in list(self._calls):
            cancel_adb(proc)
        self._calls.clear()

    def _adb(self, args, callback, timeout_ms=15000):
        """run_adb() tracked in self._calls so shutdown() can cancel it."""
        holder = {}

        def done(code, out):
            self._calls.discard(holder.get("proc"))
            callback(code, out)
        holder["proc"] = run_adb(self, self.adb, args, done, timeout_ms)
        if not holder["proc"].property("adb_done"):
            self._calls.add(holder["proc"])

    # ---- configuration -------------------------------------------------
    def configure(self, package=None, interval=None, pss_secs=None):
        changed = False
        if package is not None:
            package = package.strip()
            if package and not valid_package(package):
                raise ValueError("Invalid package name: %s" % package)
            changed |= package != self.package
            self.package = package
            self._pss.clear()
        if interval is not None:
            changed |= interval != self.interval
            self.interval = interval
        if pss_secs is not None:
            self.pss_secs = pss_secs
            self._apply_pss_timer()
        if changed and not self.paused:
            for st in self.streams.values():
                st.start(self.package, self.interval)

    def set_paused(self, paused: bool):
        self.paused = paused
        for serial, st in self.streams.items():
            if paused:
                st.stop()
                self.status.emit(serial, "paused")
            else:
                st.start(self.package, self.interval)
        self._apply_pss_timer()

    def _apply_pss_timer(self):
        if self.pss_secs > 0 and not self.paused:
            self._pss_timer.start(self.pss_secs * 1000)
            QTimer.singleShot(500, self._poll_pss)
        else:
            self._pss_timer.stop()
            self._pss.clear()

    # ---- device discovery ----------------------------------------------
    def scan(self):
        if self._scan_busy:
            return
        self._scan_busy = True
        self._adb(["devices", "-l"], self._on_scan, 10000)

    def _on_scan(self, code, out):
        self._scan_busy = False
        if code != 0:
            self.adb_error.emit("Could not run adb (%s)" % self.adb)
            return
        seen = {}
        for serial, state, attrs in parse_devices(out):
            seen[serial] = state
            if serial not in self.devices:
                self.devices[serial] = state
                self.device_added.emit(serial, state, transport_of(serial, attrs))
                if state == "device":
                    self._online(serial)
            elif self.devices[serial] != state:
                self.devices[serial] = state
                self.device_state.emit(serial, state)
                if state == "device":
                    self._online(serial)
                else:
                    self._offline(serial)
        for serial in [s for s in self.devices if s not in seen]:
            self._offline(serial)
            self.devices.pop(serial, None)
            self.info.pop(serial, None)
            self.device_removed.emit(serial)

    def _online(self, serial):
        if serial not in self.info:
            cmd = ("getprop ro.product.model; getprop ro.build.version.release; "
                   "getprop ro.build.version.sdk; nproc")
            self._adb(["-s", serial, "shell", cmd],
                      lambda c, o, s=serial: self._on_info(s, c, o), 10000)
        if serial not in self.streams:
            st = DeviceStream(self.adb, serial, self)
            st.sample.connect(self._on_sample)
            st.status.connect(self.status)
            self.streams[serial] = st
        if not self.paused:
            self.streams[serial].start(self.package, self.interval)

    def _offline(self, serial):
        st = self.streams.pop(serial, None)
        if st is not None:
            st.stop()
            st.deleteLater()
        self._pss.pop(serial, None)

    def _on_info(self, serial, code, out):
        if code != 0 or serial not in self.devices:
            return
        vals = [v.strip() for v in out.splitlines()] + ["", "", "", ""]
        info = {"model": vals[0], "release": vals[1], "sdk": vals[2],
                "cores": int(vals[3]) if vals[3].isdigit() else None}
        self.info[serial] = info
        self.device_info.emit(serial, info)

    # ---- samples ---------------------------------------------------------
    def _on_sample(self, serial, s: Sample):
        pss = self._pss.get(serial)
        if pss and s.pid is not None and pss.get("pid") in (None, s.pid):
            s.pss_kb = pss["pss_kb"]
            s.java_kb = pss.get("java_kb")
            s.native_kb = pss.get("native_kb")
            s.graphics_kb = pss.get("graphics_kb")
        self.sample.emit(serial, s)

    def _poll_pss(self):
        if not self.package or self.paused:
            return
        for serial in list(self.streams):
            if serial in self._pss_busy:
                continue
            self._pss_busy.add(serial)
            self._adb(["-s", serial, "shell", "dumpsys meminfo %s" % self.package],
                      lambda c, o, s=serial, p=self.package: self._on_pss(s, p, c, o),
                      20000)

    def _on_pss(self, serial, package, code, out):
        self._pss_busy.discard(serial)
        if package != self.package or serial not in self.streams:
            return
        parsed = parse_meminfo(out) if code == 0 else None
        if parsed:
            self._pss[serial] = parsed
        else:
            self._pss.pop(serial, None)

    def foreground_package(self, serial, callback):
        """callback(package or '') with the device's resumed activity package."""
        cmd = ("dumpsys activity activities | "
               "grep -m 1 -E 'topResumedActivity|mResumedActivity|ResumedActivity:'")

        def done(code, out):
            m = re.search(r"\su\d+\s+([A-Za-z0-9_.]+)/", out)
            callback(m.group(1) if m else "")
        self._adb(["-s", serial, "shell", cmd], done, 10000)


# ======================================================================
# Formatting
# ======================================================================

def fmt_kb(kb):
    if kb is None:
        return "-"
    if kb >= 1024 * 1024:
        return "%.2f GB" % (kb / (1024 * 1024))
    return "%.0f MB" % (kb / 1024)


def fmt_pct(v):
    return "-" if v is None else "%.1f%%" % v


# ======================================================================
# GUI: resources, About, Help
# ======================================================================

def _build_date() -> str:
    """Release/build date, derived from this file's mtime -- set-version.bash
    rewrites the VERSION line on every release, so this tracks the last publish."""
    try:
        return datetime.fromtimestamp(Path(__file__).stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return "unknown"


def resource_path(name: str) -> Path:
    """Locate a bundled resource both when run from source and when frozen by
    PyInstaller, which unpacks --add-data into _MEIPASS."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


def app_icon() -> QIcon:
    path = resource_path("icon.png")
    return QIcon(str(path)) if path.is_file() else QIcon()


def _bold_label(text: str) -> QLabel:
    lbl = QLabel(text)
    f = lbl.font()
    f.setBold(True)
    lbl.setFont(f)
    return lbl


_DIALOG_WIDTH = 420
_ANIM_MAX_W = _DIALOG_WIDTH - 32


def _animation_path() -> Path:
    return resource_path("screens/landenlabs_400.webp")


def _animation_display_size(path: Path) -> QSize:
    """Display size that preserves the animation's native aspect ratio."""
    native = QImageReader(str(path)).size()
    if not native.isValid() or native.width() == 0:
        return QSize(_ANIM_MAX_W, _ANIM_MAX_W)
    scale = min(1.0, _ANIM_MAX_W / native.width())
    return QSize(int(native.width() * scale), int(native.height() * scale))


class AboutDialog(QDialog):
    """About box: LanDen Labs animation (plays once, then freezes)."""

    def __init__(self, adb: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("About %s" % APP_NAME)
        self.setModal(True)
        self.setFixedWidth(_DIALOG_WIDTH)
        self._movie: QMovie | None = None
        self._final_pixmap: QPixmap | None = None
        self._last_frame_num = -1

        root = QVBoxLayout(self)
        root.setSpacing(10)
        root.setContentsMargins(16, 16, 16, 16)

        self._anim_label = QLabel()
        self._anim_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        anim_path = _animation_path()
        if anim_path.exists():
            size = _animation_display_size(anim_path)
            self._anim_label.setFixedSize(size)
            self._movie = QMovie(str(anim_path))
            self._movie.setScaledSize(size)
            self._anim_label.setMovie(self._movie)
            self._movie.frameChanged.connect(self._on_frame_changed)
            root.addWidget(self._anim_label, alignment=Qt.AlignmentFlag.AlignCenter)

        header = QHBoxLayout()
        icon_lbl = QLabel()
        icon_lbl.setPixmap(app_icon().pixmap(48, 48))
        header.addWidget(icon_lbl)
        name_font = QFont()
        name_font.setPointSize(15)
        name_font.setBold(True)
        name_lbl = QLabel(APP_NAME)
        name_lbl.setFont(name_font)
        header.addWidget(name_lbl)
        header.addStretch(1)
        root.addLayout(header)

        desc = QLabel(
            "%s  —  Live CPU and memory monitor for Android devices "
            "connected over USB or Wi-Fi adb." % VERSION
        )
        desc.setWordWrap(True)
        root.addWidget(desc)
        root.addSpacing(4)

        form = QFormLayout()
        form.setSpacing(5)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        form.addRow(_bold_label("Author:"), QLabel("Dennis Lang"))
        form.addRow(_bold_label("Built:"), QLabel(_build_date()))
        adb_lbl = QLabel(adb)
        adb_lbl.setWordWrap(True)
        adb_lbl.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        form.addRow(_bold_label("adb:"), adb_lbl)
        form.addRow(QLabel(""), QLabel("Created by LanDen Labs (2026)"))
        link = QLabel('<a href="https://landenlabs.com">https://landenlabs.com</a>')
        link.setOpenExternalLinks(True)
        link.setTextFormat(Qt.TextFormat.RichText)
        form.addRow(_bold_label("Web:"), link)
        root.addLayout(form)
        root.addSpacing(6)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        root.addLayout(btn_row)

    def showEvent(self, event):
        super().showEvent(event)
        if self._movie is not None:
            self._last_frame_num = -1
            self._final_pixmap = None
            self._movie.start()

    def _on_frame_changed(self, frame_num: int):
        # QMovie has no reliable "play once" for animated WebP; detect the wrap
        # back to frame 0 and freeze on the cached final frame.
        if self._movie is None:
            return
        if frame_num == 0 and self._last_frame_num > 0:
            self._movie.stop()
            if self._final_pixmap is not None:
                self._anim_label.setMovie(None)
                self._anim_label.setPixmap(self._final_pixmap)
            return
        self._final_pixmap = self._movie.currentPixmap()
        self._last_frame_num = frame_num


_HELP_HTML = """
<h2>ADB Monitor</h2>
<p>Every connected device (USB, Wi-Fi, emulator) gets a card. Set a
<b>Package</b> to also track one app; <b>Foreground</b> fills it in from the
app currently on screen of the first device.</p>
<h3>Metrics</h3>
<ul>
<li><b>Sys CPU</b> &mdash; whole-device busy %, from <code>/proc/stat</code> deltas.</li>
<li><b>App CPU</b> &mdash; the app's utime+stime as % of the whole device
(100% = every core busy); the cores figure is the same load expressed in cores.</li>
<li><b>Sys Mem</b> &mdash; MemTotal &minus; MemAvailable.</li>
<li><b>App RSS</b> &mdash; resident set size from <code>/proc/&lt;pid&gt;/status</code>.</li>
<li><b>App PSS</b> &mdash; from <code>dumpsys meminfo</code>; matches Android Studio's
memory number. Off by default.</li>
<li><b>Load</b> &mdash; 1-minute load average.</li>
</ul>
<h3>Overhead</h3>
<p>One persistent <code>adb shell</code> per device runs a loop that reads
<code>/proc</code> using shell builtins, so a sample costs microseconds on
the device. <code>dumpsys meminfo</code> binder-calls into your app and takes
~100&ndash;500&nbsp;ms, so keep the PSS interval at 10&nbsp;s or more, or Off
while measuring CPU.</p>
<h3>Tips</h3>
<ul>
<li>View &rarr; Stay on Top keeps the window over Android Studio.</li>
<li>View &rarr; Close to Tray keeps sampling with the window hidden.</li>
<li>File &rarr; Export CSV saves every sample collected this session.</li>
</ul>
"""


# ======================================================================
# GUI widgets
# ======================================================================

class Sparkline(QWidget):
    """Small history chart with a title, current value and min/max."""

    def __init__(self, title: str, color: str, fixed_max: float | None = None,
                 parent=None):
        super().__init__(parent)
        self.title = title
        self.color = QColor(color)
        self.fixed_max = fixed_max
        self.values = deque(maxlen=HISTORY)
        self.text = "-"
        self.setMinimumSize(170, 64)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def add(self, value, text: str):
        self.values.append(value)
        self.text = text
        self.update()

    def clear(self):
        self.values.clear()
        self.text = "-"
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        pal = self.palette()
        r = self.rect().adjusted(0, 0, -1, -1)
        base = pal.base().color()
        p.fillRect(r, base)
        grid = pal.mid().color()
        grid.setAlpha(90)
        p.setPen(QPen(grid, 1))
        p.drawRect(r)
        text_h = self.fontMetrics().height() + 2
        r = r.adjusted(1, text_h, -1, -1)
        nums = [v for v in self.values if v is not None]
        hi = self.fixed_max if self.fixed_max else (max(nums) * 1.15 if nums else 1.0)
        hi = hi or 1.0
        for frac in (0.25, 0.5, 0.75):
            y = r.top() + r.height() * frac
            p.drawLine(int(r.left()), int(y), int(r.right()), int(y))
        n = self.values.maxlen
        step = r.width() / max(1, n - 1)
        x0 = r.right() - step * (len(self.values) - 1)

        path, fill = QPainterPath(), QPainterPath()
        seg_start = None
        last_x = None
        for i, v in enumerate(self.values):
            x = x0 + step * i
            if v is None:
                if seg_start is not None:
                    fill.lineTo(last_x, r.bottom())
                    fill.lineTo(seg_start, r.bottom())
                    fill.closeSubpath()
                seg_start = None
                continue
            y = r.bottom() - (min(v, hi) / hi) * (r.height() - 2)
            if seg_start is None:
                path.moveTo(x, y)
                fill.moveTo(x, r.bottom())
                fill.lineTo(x, y)
                seg_start = x
            else:
                path.lineTo(x, y)
                fill.lineTo(x, y)
            last_x = x
        if seg_start is not None:
            fill.lineTo(last_x, r.bottom())
            fill.lineTo(seg_start, r.bottom())
            fill.closeSubpath()
        area = QColor(self.color)
        area.setAlpha(60)
        p.fillPath(fill, area)
        p.setPen(QPen(self.color, 1.6))
        p.drawPath(path)

        p.setPen(pal.text().color())
        f = p.font()
        f.setPointSizeF(max(8.0, f.pointSizeF() - 1))
        p.setFont(f)
        tr = self.rect().adjusted(5, 1, -5, -3)
        p.drawText(tr, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop, self.title)
        bold = QFont(f)
        bold.setBold(True)
        p.setFont(bold)
        p.drawText(tr, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop, self.text)
        if nums and not self.fixed_max:
            p.setFont(f)
            dim = pal.placeholderText().color()
            p.setPen(dim)
            p.drawText(tr, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignBottom,
                       "max %s" % self._fmt_scale(max(nums)))
        p.end()

    def _fmt_scale(self, v):
        return fmt_kb(v * 1024) if self.title.endswith(("RSS", "PSS", "Mem")) else "%.2f" % v


class DeviceCard(QFrame):
    def __init__(self, serial: str, state: str, transport: str, parent=None):
        super().__init__(parent)
        self.serial = serial
        self.transport = transport
        self.cores = None
        self.info_text = ""
        self.samples = deque(maxlen=CSV_MAX_ROWS)
        self.last: Sample | None = None
        self.setFrameShape(QFrame.Shape.StyledPanel)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 8)
        lay.setSpacing(4)
        head = QHBoxLayout()
        self.title = QLabel()
        tf = self.title.font()
        tf.setBold(True)
        self.title.setFont(tf)
        self.title.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.state_lbl = QLabel()
        self.state_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        head.addWidget(self.title, 1)
        head.addWidget(self.state_lbl)
        lay.addLayout(head)

        grid = QGridLayout()
        grid.setSpacing(4)
        self.sys_cpu = Sparkline("Sys CPU", "#2e9d5b", 100)
        self.app_cpu = Sparkline("App CPU", "#e8833a", 100)
        self.sys_mem = Sparkline("Sys Mem", "#4285f4", 100)
        self.app_rss = Sparkline("App RSS", "#9b59b6")
        self.load = Sparkline("Load", "#7f8c8d")
        self.app_pss = Sparkline("App PSS", "#16a085")
        grid.addWidget(self.sys_cpu, 0, 0)
        grid.addWidget(self.app_cpu, 0, 1)
        grid.addWidget(self.sys_mem, 1, 0)
        grid.addWidget(self.app_rss, 1, 1)
        grid.addWidget(self.load, 2, 0)
        grid.addWidget(self.app_pss, 2, 1)
        lay.addLayout(grid)
        self.set_state(state)
        self._update_title()

    def set_info(self, info: dict):
        self.cores = info.get("cores")
        bits = [info.get("model") or "?"]
        if info.get("release"):
            bits.append("Android %s (SDK %s)" % (info["release"], info.get("sdk", "?")))
        if self.cores:
            bits.append("%d cores" % self.cores)
        self.info_text = "  ·  ".join(bits)
        self._update_title()

    def _update_title(self):
        head = self.info_text or "…"
        self.title.setText("%s  ·  %s  ·  %s" % (head, self.serial, self.transport))

    def set_state(self, state: str):
        if state == "device":
            self.state_lbl.setText("")
        elif state == "unauthorized":
            self.state_lbl.setText("unauthorized - accept the prompt on the device")
        else:
            self.state_lbl.setText(state)

    def set_status(self, text: str):
        if text != "streaming":
            self.state_lbl.setText(text)

    def reset_app(self):
        for sp in (self.app_cpu, self.app_rss, self.app_pss):
            sp.clear()

    def add_sample(self, s: Sample, package: str):
        self.samples.append(s)
        self.last = s
        self.sys_cpu.add(s.cpu_sys, fmt_pct(s.cpu_sys))
        used = s.mem_used_kb
        if used is not None and s.mem_total_kb:
            pct = 100.0 * used / s.mem_total_kb
            self.sys_mem.add(pct, "%s / %s" % (fmt_kb(used), fmt_kb(s.mem_total_kb)))
        else:
            self.sys_mem.add(None, "-")
        self.load.add(s.load1, "-" if s.load1 is None else "%.2f" % s.load1)
        if s.cpu_app is not None and self.cores:
            app_text = "%.1f%%  (%.2f cores)" % (s.cpu_app, s.cpu_app * self.cores / 100)
        else:
            app_text = fmt_pct(s.cpu_app)
        self.app_cpu.add(s.cpu_app, app_text)
        rss_text = fmt_kb(s.rss_kb)
        if s.threads:
            rss_text += "  ·  %d thr" % s.threads
        self.app_rss.add(None if s.rss_kb is None else s.rss_kb / 1024, rss_text)
        if s.pss_kb is not None:
            pss_text = fmt_kb(s.pss_kb)
            if s.java_kb is not None and s.native_kb is not None:
                pss_text += "  (J %s  N %s)" % (fmt_kb(s.java_kb), fmt_kb(s.native_kb))
            self.app_pss.add(s.pss_kb / 1024, pss_text)
        else:
            self.app_pss.add(None, "-")
        if package:
            self.state_lbl.setText("pid %d" % s.pid if s.pid else "%s not running" % package)
        else:
            self.state_lbl.setText("")


class MainWindow(QMainWindow):
    def __init__(self, monitor: Monitor, settings: QSettings):
        super().__init__()
        self.monitor = monitor
        self.settings = settings
        self.cards = {}
        self._quitting = False
        self.setWindowTitle(WINDOW_TITLE)
        self.setWindowIcon(app_icon())

        # ---- toolbar --------------------------------------------------
        tb = QToolBar("Controls")
        tb.setMovable(False)
        self.addToolBar(tb)
        tb.addWidget(QLabel(" Package "))
        self.pkg_combo = QComboBox()
        self.pkg_combo.setEditable(True)
        self.pkg_combo.setMinimumWidth(260)
        self.pkg_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.pkg_combo.lineEdit().setPlaceholderText("com.example.app (blank = system only)")
        history = settings.value("package_history", [], type=list) or []
        self.pkg_combo.addItems([h for h in history if valid_package(h)])
        self.pkg_combo.setEditText(monitor.package)
        self.pkg_combo.lineEdit().returnPressed.connect(self._apply_package)
        self.pkg_combo.activated.connect(lambda _i: self._apply_package())
        tb.addWidget(self.pkg_combo)
        fg = QPushButton("Foreground")
        fg.setToolTip("Use the app currently on screen of the first device")
        fg.clicked.connect(self._use_foreground)
        tb.addWidget(fg)
        tb.addSeparator()
        tb.addWidget(QLabel(" Every "))
        self.int_combo = QComboBox()
        self.int_combo.addItems(["%s s" % i for i in INTERVALS])
        self.int_combo.setCurrentIndex(max(0, INTERVALS.index(monitor.interval))
                                       if monitor.interval in INTERVALS else 1)
        self.int_combo.currentIndexChanged.connect(
            lambda i: self.monitor.configure(interval=INTERVALS[i]))
        tb.addWidget(self.int_combo)
        tb.addWidget(QLabel("  PSS "))
        self.pss_combo = QComboBox()
        self.pss_combo.addItems([label for label, _ in PSS_INTERVALS])
        secs = [s for _, s in PSS_INTERVALS]
        self.pss_combo.setCurrentIndex(secs.index(monitor.pss_secs)
                                       if monitor.pss_secs in secs else 0)
        self.pss_combo.setToolTip("dumpsys meminfo interval (costs ~100-500 ms on device)")
        self.pss_combo.currentIndexChanged.connect(
            lambda i: self.monitor.configure(pss_secs=PSS_INTERVALS[i][1]))
        tb.addWidget(self.pss_combo)
        tb.addSeparator()
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._toggle_pause)
        tb.addWidget(self.pause_btn)

        # ---- body -----------------------------------------------------
        self.body = QWidget()
        self.body_lay = QVBoxLayout(self.body)
        self.body_lay.setContentsMargins(6, 6, 6, 6)
        self.body_lay.setSpacing(6)
        self.empty_lbl = QLabel("Waiting for devices…  (adb devices)")
        self.empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.body_lay.addWidget(self.empty_lbl)
        self.body_lay.addStretch(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.body)
        self.setCentralWidget(scroll)

        self._build_menus()
        self._build_tray()

        monitor.device_added.connect(self._on_added)
        monitor.device_state.connect(self._on_state)
        monitor.device_info.connect(self._on_info)
        monitor.device_removed.connect(self._on_removed)
        monitor.sample.connect(self._on_sample)
        monitor.status.connect(self._on_status)
        monitor.adb_error.connect(lambda m: self.statusBar().showMessage(m, 5000))

        geo = settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(640, 420)
        if self.on_top_act.isChecked():
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.statusBar().showMessage("adb: %s" % monitor.adb, 5000)

    # ---- menus / tray ---------------------------------------------------
    def _build_menus(self):
        mb = self.menuBar()
        fm = mb.addMenu("&File")
        exp = QAction("Export CSV…", self)
        exp.setShortcut("Ctrl+E")
        exp.triggered.connect(self._export_csv)
        fm.addAction(exp)
        fm.addSeparator()
        quit_act = QAction("Quit", self)
        quit_act.setShortcut("Ctrl+Q")
        quit_act.setMenuRole(QAction.MenuRole.QuitRole)
        quit_act.triggered.connect(self.quit)
        fm.addAction(quit_act)

        vm = mb.addMenu("&View")
        self.on_top_act = QAction("Stay on Top", self, checkable=True)
        self.on_top_act.setChecked(self.settings.value("on_top", False, type=bool))
        self.on_top_act.toggled.connect(self._set_on_top)
        vm.addAction(self.on_top_act)
        self.to_tray_act = QAction("Close to Tray", self, checkable=True)
        self.to_tray_act.setChecked(self.settings.value("close_to_tray", False, type=bool))
        self.to_tray_act.setEnabled(QSystemTrayIcon.isSystemTrayAvailable())
        vm.addAction(self.to_tray_act)
        vm.addSeparator()
        clear = QAction("Clear Graphs", self)
        clear.triggered.connect(self._clear_graphs)
        vm.addAction(clear)

        hm = mb.addMenu("&Help")
        help_act = QAction("%s Help" % APP_NAME, self)
        help_act.triggered.connect(self._show_help)
        hm.addAction(help_act)
        about = QAction("About %s" % APP_NAME, self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(self._show_about)
        hm.addAction(about)

    def _build_tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(app_icon(), self)
        menu = QMenu()
        show = menu.addAction("Show Window")
        show.triggered.connect(self._show_window)
        self.tray_pause = menu.addAction("Pause")
        self.tray_pause.triggered.connect(lambda: self.pause_btn.toggle())
        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self.quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: self._show_window()
            if reason == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self.tray.setToolTip(APP_NAME)
        self.tray.show()
        self._tray_menu = menu

    def _show_window(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def _set_on_top(self, on: bool):
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, on)
        self.show()

    # ---- actions --------------------------------------------------------
    def _apply_package(self):
        pkg = self.pkg_combo.currentText().strip()
        if pkg and not valid_package(pkg):
            self.statusBar().showMessage("Invalid package name: %s" % pkg, 5000)
            return
        if pkg == self.monitor.package:
            return
        self.monitor.configure(package=pkg)
        for card in self.cards.values():
            card.reset_app()
        if pkg:
            items = [self.pkg_combo.itemText(i) for i in range(self.pkg_combo.count())]
            items = [pkg] + [i for i in items if i != pkg][:14]
            self.pkg_combo.blockSignals(True)
            self.pkg_combo.clear()
            self.pkg_combo.addItems(items)
            self.pkg_combo.setEditText(pkg)
            self.pkg_combo.blockSignals(False)
        self.statusBar().showMessage("Tracking %s" % (pkg or "system only"), 3000)

    def _use_foreground(self):
        online = [s for s, st in self.monitor.devices.items() if st == "device"]
        if not online:
            self.statusBar().showMessage("No device online", 3000)
            return

        def got(pkg):
            if not pkg:
                self.statusBar().showMessage("Could not determine foreground app", 4000)
                return
            self.pkg_combo.setEditText(pkg)
            self._apply_package()
        self.monitor.foreground_package(online[0], got)

    def _toggle_pause(self, paused: bool):
        self.pause_btn.setText("Resume" if paused else "Pause")
        if self.tray is not None:
            self.tray_pause.setText("Resume" if paused else "Pause")
        self.monitor.set_paused(paused)

    def _clear_graphs(self):
        for card in self.cards.values():
            for sp in card.findChildren(Sparkline):
                sp.clear()

    def _export_csv(self):
        rows = sum(len(c.samples) for c in self.cards.values())
        if not rows:
            QMessageBox.information(self, "Export CSV", "No samples collected yet.")
            return
        default = "adb-monitor-%s.csv" % datetime.now().strftime("%Y%m%d-%H%M%S")
        path, _ = QFileDialog.getSaveFileName(
            self, "Export CSV", str(Path.home() / default), "CSV files (*.csv)")
        if not path:
            return
        try:
            write_csv(path, {s: c.samples for s, c in self.cards.items()}, self.monitor.package)
        except OSError as exc:
            QMessageBox.warning(self, "Export CSV", "Could not write file:\n%s" % exc)
            return
        self.statusBar().showMessage("Wrote %d rows to %s" % (rows, path), 5000)

    def _show_about(self):
        AboutDialog(self.monitor.adb, self).exec()

    def _show_help(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("%s Help" % APP_NAME)
        dlg.resize(560, 520)
        lay = QVBoxLayout(dlg)
        tb = QTextBrowser()
        tb.setHtml(_HELP_HTML)
        lay.addWidget(tb)
        close = QPushButton("Close")
        close.clicked.connect(dlg.accept)
        lay.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        dlg.exec()

    # ---- monitor signals ------------------------------------------------
    def _on_added(self, serial, state, transport):
        card = DeviceCard(serial, state, transport)
        self.cards[serial] = card
        self.body_lay.insertWidget(self.body_lay.count() - 1, card, 1)
        self.empty_lbl.hide()

    def _on_state(self, serial, state):
        if serial in self.cards:
            self.cards[serial].set_state(state)

    def _on_info(self, serial, info):
        if serial in self.cards:
            self.cards[serial].set_info(info)

    def _on_removed(self, serial):
        card = self.cards.pop(serial, None)
        if card is not None:
            card.setParent(None)
            card.deleteLater()
        self.empty_lbl.setVisible(not self.cards)
        self._update_tray()

    def _on_status(self, serial, text):
        if serial in self.cards:
            self.cards[serial].set_status(text)

    def _on_sample(self, serial, s):
        card = self.cards.get(serial)
        if card is None:
            return
        card.add_sample(s, self.monitor.package)
        self._update_tray()

    def _update_tray(self):
        if self.tray is None:
            return
        lines = [APP_NAME]
        for serial, card in self.cards.items():
            s = card.last
            if s is None:
                continue
            name = card.info_text.split("  ·  ")[0] if card.info_text else serial
            line = "%s: CPU %s  Mem %s" % (name, fmt_pct(s.cpu_sys), fmt_kb(s.mem_used_kb))
            if self.monitor.package and s.pid:
                line += "  App %s / %s" % (fmt_pct(s.cpu_app), fmt_kb(s.rss_kb))
            lines.append(line)
        self.tray.setToolTip("\n".join(lines))

    # ---- lifecycle ------------------------------------------------------
    def closeEvent(self, event):
        if (not self._quitting and self.tray is not None
                and self.to_tray_act.isChecked()):
            event.ignore()
            self.hide()
            return
        self._shutdown()
        event.accept()
        QApplication.instance().quit()

    def _shutdown(self):
        """Save settings and stop streams; safe to call more than once
        (window close and app.aboutToQuit, e.g. Dock -> Quit on macOS)."""
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        self._save_settings()
        self.monitor.shutdown()
        if self.tray is not None:
            self.tray.hide()

    def quit(self):
        self._quitting = True
        self.close()

    def _save_settings(self):
        st = self.settings
        st.setValue("geometry", self.saveGeometry())
        st.setValue("package", self.monitor.package)
        st.setValue("interval", self.monitor.interval)
        st.setValue("pss_secs", self.monitor.pss_secs)
        st.setValue("on_top", self.on_top_act.isChecked())
        st.setValue("close_to_tray", self.to_tray_act.isChecked())
        st.setValue("package_history",
                    [self.pkg_combo.itemText(i) for i in range(self.pkg_combo.count())])


# ======================================================================
# CSV + console mode
# ======================================================================

def write_csv(path: str, samples_by_serial: dict, package: str):
    cols = [f.name for f in fields(Sample)]
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["serial", "time", "package"] + cols[1:] + ["mem_used_kb"])
        for serial, samples in samples_by_serial.items():
            for s in samples:
                stamp = datetime.fromtimestamp(s.t).isoformat(timespec="milliseconds")
                vals = [getattr(s, c) for c in cols[1:]]
                vals = ["" if v is None else (round(v, 2) if isinstance(v, float) else v)
                        for v in vals]
                w.writerow([serial, stamp, package] + vals + [s.mem_used_kb or ""])


def run_console(monitor: Monitor, duration: float, csv_path: str) -> int:
    """Headless mode: print one line per sample, optionally write CSV."""
    app = QCoreApplication.instance()
    collected = {}
    info = {}

    def on_sample(serial, s: Sample):
        collected.setdefault(serial, []).append(s)
        cores = (info.get(serial) or {}).get("cores")
        line = "%s %-16s cpu %6s  mem %9s / %-9s load %5s" % (
            datetime.fromtimestamp(s.t).strftime("%H:%M:%S"), serial[:16],
            fmt_pct(s.cpu_sys), fmt_kb(s.mem_used_kb), fmt_kb(s.mem_total_kb),
            "-" if s.load1 is None else "%.2f" % s.load1)
        if monitor.package:
            if s.pid:
                line += "  | pid %d cpu %s" % (s.pid, fmt_pct(s.cpu_app))
                if s.cpu_app is not None and cores:
                    line += " (%.2f cores)" % (s.cpu_app * cores / 100)
                line += " rss %s thr %s" % (fmt_kb(s.rss_kb), s.threads or "-")
                if s.pss_kb is not None:
                    line += " pss %s" % fmt_kb(s.pss_kb)
            else:
                line += "  | %s not running" % monitor.package
        print(line, flush=True)

    monitor.sample.connect(on_sample)
    monitor.device_added.connect(
        lambda s, st, tr: print("# device %s  %s  %s" % (s, st, tr), flush=True))
    monitor.device_removed.connect(lambda s: print("# removed %s" % s, flush=True))
    monitor.device_info.connect(lambda s, i: info.__setitem__(s, i))
    monitor.status.connect(
        lambda s, t: None if t == "streaming" else print("# %s: %s" % (s, t), flush=True))
    monitor.adb_error.connect(lambda m: print("# " + m, file=sys.stderr, flush=True))
    monitor.start()
    if duration > 0:
        QTimer.singleShot(int(duration * 1000), app.quit)
    # Let Ctrl-C through the Qt event loop.
    signal.signal(signal.SIGINT, lambda *_: app.quit())
    tick = QTimer()
    tick.start(250)
    tick.timeout.connect(lambda: None)
    app.exec()
    monitor.shutdown()
    if csv_path:
        write_csv(csv_path, collected, monitor.package)
        print("# wrote %s" % csv_path)
    return 0


# ======================================================================
# main
# ======================================================================

def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="adb-monitor",
        description="Live CPU and memory monitor for Android devices over adb "
                    "(USB, Wi-Fi, emulator).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""examples:
  adb-monitor.py                                  # GUI, remembers last settings
  adb-monitor.py -p com.example.app               # GUI tracking one app
  adb-monitor.py -p com.example.app --pss 30      # add PSS every 30 s
  adb-monitor.py --console -p com.example.app -i 0.5 -d 60 --csv run.csv
""")
    parser.add_argument("--package", "-p", default=None,
                        help="app package to track (blank = system only)")
    parser.add_argument("--interval", "-i", default=None, choices=INTERVALS,
                        help="sample interval in seconds (default 1)")
    parser.add_argument("--pss", type=int, default=None, metavar="SECS",
                        help="dumpsys meminfo interval in seconds, 0 = off (default)")
    parser.add_argument("--adb", default="", help="path to adb (default: auto-detect)")
    parser.add_argument("--console", "-c", action="store_true",
                        help="no GUI; print one line per sample")
    parser.add_argument("--duration", "-d", type=float, default=0,
                        help="console mode: stop after SECS (default: until Ctrl-C)")
    parser.add_argument("--csv", default="", help="console mode: write samples to CSV on exit")
    parser.add_argument("--version", "-V", action="version",
                        version="adb-monitor %s" % VERSION)
    args = parser.parse_args(argv)
    if args.package and not valid_package(args.package):
        parser.error("invalid package name: %s" % args.package)
    if args.pss is not None and args.pss < 0:
        parser.error("--pss must be >= 0")
    return args


def main(argv=None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    adb = find_adb(args.adb)

    if args.console:
        app = QCoreApplication(sys.argv)
        monitor = Monitor(adb, args.package or "", args.interval or "1", args.pss or 0)
        return run_console(monitor, args.duration, args.csv)

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(SETTINGS_ORG)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)   # tray mode keeps running
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)

    package = args.package if args.package is not None else settings.value("package", "", type=str)
    interval = args.interval or settings.value("interval", "1", type=str)
    if interval not in INTERVALS:
        interval = "1"
    pss = args.pss if args.pss is not None else settings.value("pss_secs", 0, type=int)
    monitor = Monitor(adb, package or "", interval, pss)
    win = MainWindow(monitor, settings)
    app.aboutToQuit.connect(win._shutdown)
    win.show()
    monitor.start()
    signal.signal(signal.SIGINT, lambda *_: win.quit())
    tick = QTimer()
    tick.start(250)
    tick.timeout.connect(lambda: None)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
