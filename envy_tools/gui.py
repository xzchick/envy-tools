"""The ENVY Tools window: browse every ENVY app, install, open, update and remove.

It opens instantly from the built-in app list and what's installed, then checks
GitHub in the background for new apps and new versions. Every request and every
step is shown in the log at the bottom.
"""

from __future__ import annotations

import math
import os
import sys

from . import __version__
from .catalog import ToolsError, find
from .installer import Installer

BG = "#050713"
PANEL = "#0a0e22"
PANEL_HI = "#121735"
INK = "#f2f4ff"
MUTED = "#b9c3df"
DIM = "#6f7898"
PINK = "#ff3db4"
CYAN = "#35e8ff"
LAVENDER = "#ac8cff"
VIOLET = "#8547e8"
AMBER = "#ffb347"
GREEN = "#49e3a6"
LINE = "#1f2547"

COLUMNS = ["App", "Status", "What it does"]


def status_text(row) -> tuple[str, str]:
    """(label, colour) for a row: the one place the GUI decides what to call a state."""
    if row.installed:
        if row.update_available:
            return f"Update {row.installed.version} → {row.latest}", AMBER
        return f"Installed {row.installed.version}", GREEN
    if row.elsewhere:
        return "On this computer", LAVENDER
    if row.latest:
        return f"Available {row.latest}", MUTED
    return "Available", MUTED


def _build():
    from PyQt6.QtCore import Qt, QTimer, QThread, pyqtSignal, QRectF, QPointF
    from PyQt6.QtGui import QColor, QPainter, QLinearGradient, QRadialGradient, QFont, QPen, QBrush
    from PyQt6.QtWidgets import (QApplication, QWidget, QLabel, QLineEdit, QPushButton, QVBoxLayout,
                                 QHBoxLayout, QFrame, QTableWidget, QTableWidgetItem, QHeaderView,
                                 QAbstractItemView, QPlainTextEdit, QMessageBox, QProgressBar)

    class Header(QWidget):
        def __init__(self):
            super().__init__()
            self.setFixedHeight(140)
            self.phase = 0.0
            self.stars = [(((i * 139 + 37) % 1000) / 1000.0, ((i * 83 + 19) % 140) / 140.0,
                           0.6 + (i % 3) * 0.5, (i % 7) / 7.0) for i in range(56)]

        def tick(self, phase):
            self.phase = phase
            self.update()

        def paintEvent(self, _event):
            p = QPainter(self)
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            w, h = self.width(), self.height()
            p.fillRect(self.rect(), QColor(BG))
            for cx, cy, rad, col, a in ((w - 120, 60, 240, CYAN, 70), (60, h, 280, PINK, 50),
                                        (w * 0.5, -40, 240, VIOLET, 45)):
                g = QRadialGradient(cx, cy, rad)
                c = QColor(col); c.setAlpha(a); g.setColorAt(0, c)
                c2 = QColor(col); c2.setAlpha(0); g.setColorAt(1, c2)
                p.setBrush(QBrush(g)); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(cx, cy), rad, rad)
            for sx, sy, sr, ph in self.stars:
                tw = 0.5 + 0.5 * math.sin(self.phase / 0.5 + ph * 6.28)
                c = QColor(205, 217, 255); c.setAlphaF(0.15 + 0.6 * tw)
                p.setBrush(c); p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QPointF(sx * w, sy * h), sr, sr)
            pulse = 0.5 + 0.5 * math.sin(self.phase)
            p.setFont(QFont("Helvetica", 38, QFont.Weight.Bold))
            grad = QLinearGradient(28, 0, 360, 0)
            grad.setColorAt(0, QColor(CYAN)); grad.setColorAt(1, QColor(PINK))
            glow = QColor(CYAN); glow.setAlpha(int(60 + 60 * pulse))
            p.setPen(QPen(glow, 1))
            for dx, dy in ((1.4, 1.4), (-1.4, 1.4)):
                p.drawText(QRectF(28 + dx, 26 + dy, 460, 52), Qt.AlignmentFlag.AlignVCenter, "ENVY TOOLS")
            p.setPen(QPen(QBrush(grad), 1))
            p.drawText(QRectF(28, 26, 460, 52), Qt.AlignmentFlag.AlignVCenter, "ENVY TOOLS")
            p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
            p.setPen(QColor(LAVENDER))
            p.drawText(QRectF(32, 84, 640, 20), Qt.AlignmentFlag.AlignVCenter,
                       "EVERY ENVY APP  ·  SIGNED RELEASES  ·  ANY OS")
            # A row of app "tiles" lighting up one after another.
            n = 13
            gap, size, y = 8, 10, 116
            lit = int(self.phase * 1.5) % n
            for i in range(n):
                x = 32 + i * (size + gap)
                col = QColor(PINK if i == lit else CYAN if i < lit else LINE)
                p.setBrush(col); p.setPen(Qt.PenStyle.NoPen)
                p.drawRoundedRect(QRectF(x, y, size, size), 3, 3)
            p.end()

    class Worker(QThread):
        done = pyqtSignal(str, object)
        line = pyqtSignal(str)
        progress = pyqtSignal(int, int)

        def __init__(self, fn):
            super().__init__()
            self.fn = fn

        def run(self):
            try:
                self.done.emit("ok", self.fn())
            except ToolsError as exc:
                self.done.emit("error", str(exc))
            except Exception as exc:  # pragma: no cover
                self.done.emit("error", f"Unexpected error: {exc}")

    class ToolsWindow(QWidget):
        def __init__(self, installer: Installer | None = None, check_on_start: bool = True):
            super().__init__()
            self.inst = installer or Installer()
            self.inst.log = self._log_threadsafe
            self.worker = None
            self.rows = []
            self.latest = {}
            self.setWindowTitle("ENVY Tools")
            self.resize(940, 860)
            self.setMinimumSize(780, 720)
            self.setStyleSheet(STYLE)

            root = QVBoxLayout(self); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
            self.header = Header(); root.addWidget(self.header)
            body = QWidget(); body.setObjectName("body")
            bl = QVBoxLayout(body); bl.setContentsMargins(24, 16, 24, 18); bl.setSpacing(12)
            root.addWidget(body, 1)

            card = QFrame(); card.setObjectName("card")
            cl = QVBoxLayout(card); cl.setContentsMargins(18, 14, 18, 14); cl.setSpacing(10)
            top = QHBoxLayout(); top.setSpacing(8)
            self.search = QLineEdit(); self.search.setPlaceholderText("Find an app: encryption, notes, vpn, osint…")
            self.search.setAccessibleName("Find an app")
            self.search.textChanged.connect(lambda _t: self.fill())
            self.b_check = self._btn("Check for updates", self.check)
            top.addWidget(self.search, 1); top.addWidget(self.b_check)
            cl.addLayout(top)
            self.table = QTableWidget(0, len(COLUMNS))
            self.table.setHorizontalHeaderLabels(COLUMNS)
            self.table.verticalHeader().setVisible(False)
            self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            hdr = self.table.horizontalHeader()
            hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
            hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
            self.table.setAccessibleName("ENVY apps")
            self.table.verticalHeader().setDefaultSectionSize(32)
            self.table.setMinimumHeight(300)
            self.table.itemSelectionChanged.connect(self.update_buttons)
            self.table.doubleClicked.connect(lambda _i: self.primary())
            cl.addWidget(self.table, 1)
            acts = QHBoxLayout(); acts.setSpacing(8)
            self.b_install = self._btn("Install", self.install, "go")
            self.b_open = self._btn("Open", self.open_app, "go")
            self.b_update = self._btn("Update", self.update_one)
            self.b_remove = self._btn("Remove", self.remove)
            self.b_all = self._btn("Update all", self.update_all)
            for b in (self.b_install, self.b_open, self.b_update, self.b_remove):
                acts.addWidget(b)
            acts.addStretch(1); acts.addWidget(self.b_all)
            cl.addLayout(acts)
            self.detail = QLabel(); self.detail.setObjectName("hint"); self.detail.setWordWrap(True)
            cl.addWidget(self.detail)
            self.bar = QProgressBar(); self.bar.setTextVisible(False); self.bar.setFixedHeight(6)
            self.bar.setVisible(False)
            cl.addWidget(self.bar)
            bl.addWidget(card, 1)

            bl.addWidget(self._tag("EVERY REQUEST AND STEP"))
            self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setObjectName("log")
            self.log.setMaximumBlockCount(800); self.log.setFixedHeight(120)
            self.log.setAccessibleName("Activity log")
            bl.addWidget(self.log)
            self.status = QLabel("Ready."); self.status.setObjectName("status"); self.status.setWordWrap(True)
            bl.addWidget(self.status)
            os_label = {"macos": "macOS", "windows": "Windows", "linux": "Linux"}.get(self.inst.os_name,
                                                                                      self.inst.os_name)
            foot = QLabel(f"{os_label} {self.inst.arch} · checksums verified before anything is installed · "
                          f"no admin rights · v{__version__}")
            foot.setObjectName("foot"); bl.addWidget(foot)

            self.phase = 0.0
            self.timer = QTimer(self); self.timer.timeout.connect(self._animate); self.timer.start(33)
            self.reload(online=False)
            if check_on_start:
                QTimer.singleShot(200, self.check)

        # ----- helpers -----
        def _tag(self, text):
            lab = QLabel(text); lab.setObjectName("tag"); return lab

        def _btn(self, text, slot, kind="pick"):
            b = QPushButton(text); b.setObjectName(kind); b.setMinimumHeight(36)
            b.setCursor(Qt.CursorShape.PointingHandCursor); b.clicked.connect(slot); return b

        def _animate(self):
            self.phase += 0.05
            self.header.tick(self.phase)

        def _log_threadsafe(self, message):
            if self.worker is not None and QThread.currentThread() is self.worker:
                self.worker.line.emit(message)
            else:
                self.log.appendPlainText(message)

        def _say(self, message, color=MUTED):
            self.status.setText(message); self.status.setStyleSheet(f"#status{{color:{color};}}")

        def _fail(self, message):
            self._say(message, PINK)

        def _confirm(self, title, text):  # replaced in tests
            return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

        # ----- data → table -----
        def reload(self, online=False):
            self.rows = self.inst.rows(check_latest=False, apps=self.inst.apps(online=online))
            for r in self.rows:
                r.latest = self.latest.get(r.app.slug)
            self.fill()

        def visible_rows(self):
            q = self.search.text().strip()
            if not q:
                return list(self.rows)
            keep = {a.slug for a in find([r.app for r in self.rows], q)}
            return [r for r in self.rows if r.app.slug in keep]

        def fill(self):
            keep = self.selected()
            rows = self.visible_rows()
            self.table.setRowCount(len(rows))
            for i, row in enumerate(rows):
                label, color = status_text(row)
                name = QTableWidgetItem(row.app.name)
                name.setData(Qt.ItemDataRole.UserRole, row.app.slug)
                state = QTableWidgetItem(label); state.setForeground(QColor(color))
                summary = row.app.summary + ("  · uses the network" if row.app.network else "")
                for c, item in enumerate((name, state, QTableWidgetItem(summary))):
                    self.table.setItem(i, c, item)
                if keep and row.app.slug == keep:
                    self.table.selectRow(i)
            n_inst = sum(1 for r in self.rows if r.installed)
            n_upd = sum(1 for r in self.rows if r.update_available)
            self.b_all.setText(f"Update all ({n_upd})" if n_upd else "Update all")
            self.b_all.setEnabled(n_upd > 0 and self.worker is None)
            if not self.status.text() or self.status.text() == "Ready.":
                self._say(f"{len(self.rows)} ENVY apps · {n_inst} installed", MUTED)
            self.update_buttons()

        def selected(self):
            rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
            if not rows:
                return None
            item = self.table.item(rows[0].row(), 0)
            return item.data(Qt.ItemDataRole.UserRole) if item else None

        def row_for(self, slug):
            return next((r for r in self.rows if r.app.slug == slug), None)

        def update_buttons(self):
            row = self.row_for(self.selected()) if self.selected() else None
            busy = self.worker is not None
            self.b_install.setEnabled(bool(row and not row.installed and not busy))
            self.b_open.setEnabled(bool(row and row.installed and not busy))
            self.b_update.setEnabled(bool(row and row.update_available and not busy))
            self.b_remove.setEnabled(bool(row and row.installed and not busy))
            self.b_check.setEnabled(not busy); self.search.setEnabled(True)
            if row is None:
                self.detail.setText("Select an app. Double-click installs it, or opens it once installed.")
            else:
                where = ""
                if row.installed:
                    where = f"Installed at {row.installed.folder}"
                elif row.elsewhere:
                    where = (f"Already on this computer at {row.elsewhere} (installed another way). "
                             "Installing here adds a managed copy you can update from ENVY Tools.")
                else:
                    where = f"From {row.app.repo_url}/releases"
                kind = " Opens in a terminal." if row.app.kind == "terminal" else ""
                self.detail.setText(f"{row.app.name}: {row.app.summary}.{kind}  {where}")

        # ----- background work -----
        def _start(self, label, fn, after=None, progress=False):
            if self.worker:
                return False
            self._say(label + "…", CYAN)
            self.worker = Worker(fn)
            self.worker.line.connect(self.log.appendPlainText)
            self.worker.progress.connect(self._progress)
            self.worker.done.connect(lambda kind, payload: self._finished(kind, payload, after))
            if progress:
                self.bar.setRange(0, 0); self.bar.setVisible(True)
            self.update_buttons()
            self.worker.start()
            return True

        def _progress(self, got, total):
            if total:
                self.bar.setRange(0, 1000); self.bar.setValue(int(got * 1000 / total))

        def _emit_progress(self, got, total):
            if self.worker is not None:
                self.worker.progress.emit(got, total)

        def _finished(self, kind, payload, after):
            self.worker = None
            self.bar.setVisible(False)
            if kind == "ok":
                if after:
                    after(payload)
            else:
                self._fail(payload)
            self.reload(online=False)

        # ----- actions -----
        def check(self):
            inst = self.inst

            def work():
                apps = inst.apps(refresh=True, online=True)
                return {r.app.slug: r.latest for r in inst.rows(check_latest=True, apps=apps)}

            def done(latest):
                self.latest = {k: v for k, v in latest.items() if v}
                n = sum(1 for r in self.inst.rows(apps=self.inst.apps(online=False))
                        if r.installed and self.latest.get(r.app.slug)
                        and self.latest[r.app.slug] != r.installed.version)
                self._say("Up to date." if not n else f"{n} update(s) available.", GREEN if not n else AMBER)
            self._start("Checking GitHub for apps and versions", work, done)

        def primary(self):
            row = self.row_for(self.selected())
            if row is None:
                return
            (self.open_app if row.installed else self.install)()

        def install(self):
            row = self.row_for(self.selected())
            if row is None:
                return self._fail("Select an app first.")
            app = row.app
            self._start(f"Installing {app.name}", lambda: self.inst.install(app, self._emit_progress),
                        lambda rec: self._say(f"{app.name} {rec.version} installed. Press Open to start it.",
                                              GREEN), progress=True)

        def update_one(self):
            row = self.row_for(self.selected())
            if row is None or not row.installed:
                return self._fail("Select an installed app first.")
            app = row.app
            self._start(f"Updating {app.name}", lambda: self.inst.update([app], self._emit_progress),
                        lambda _c: self._say(f"{app.name} is up to date.", GREEN), progress=True)

        def update_all(self):
            apps = [r.app for r in self.rows if r.update_available]
            if not apps:
                return self._say("Everything is up to date.", GREEN)
            self._start(f"Updating {len(apps)} app(s)", lambda: self.inst.update(apps, self._emit_progress),
                        lambda changed: self._say(f"Updated {len(changed)} app(s).", GREEN), progress=True)

        def remove(self):
            row = self.row_for(self.selected())
            if row is None or not row.installed:
                return self._fail("Select an installed app first.")
            if not self._confirm("Remove app", f"Remove {row.app.name}? Its launchers go too. "
                                               "Your own files made with it are not touched."):
                return
            try:
                self.inst.remove(row.app)
            except ToolsError as exc:
                return self._fail(str(exc))
            self._say(f"{row.app.name} removed.", GREEN)
            self.reload(online=False)

        def open_app(self):
            row = self.row_for(self.selected())
            if row is None or not row.installed:
                return self._fail("Select an installed app first.")
            try:
                self.inst.open(row.app)
            except (ToolsError, OSError) as exc:
                return self._fail(str(exc))
            self._say(f"Started {row.app.name}.", GREEN)

    return QApplication, ToolsWindow


STYLE = f"""
#body {{ background: {BG}; }}
QWidget {{ color: {INK}; font-family: Helvetica, Arial, sans-serif; font-size: 13px; }}
#card {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 14px; }}
#tag {{ color: {MUTED}; font-weight: bold; font-size: 11px; letter-spacing: 1px; }}
#hint {{ color: {DIM}; font-size: 11px; }}
#status {{ color: {MUTED}; font-size: 12px; }}
#foot {{ color: {DIM}; font-family: "Courier New", monospace; font-size: 10px; }}
#log {{ background: {PANEL}; border: 1px solid {LINE}; border-radius: 9px; color: {LAVENDER};
        font-family: "Courier New", monospace; font-size: 11px; }}
QLineEdit {{ background: {PANEL_HI}; border: 1px solid {LINE}; border-radius: 9px;
            padding: 7px 10px; color: {INK}; selection-background-color: {VIOLET}; }}
QLineEdit:focus {{ border: 1px solid {CYAN}; }}
QTableWidget {{ background: {PANEL_HI}; border: 1px solid {LINE}; border-radius: 9px;
                gridline-color: {LINE}; selection-background-color: {VIOLET}; }}
QHeaderView::section {{ background: {PANEL}; color: {MUTED}; border: none; padding: 6px;
                        font-weight: bold; font-size: 11px; }}
QProgressBar {{ background: {PANEL_HI}; border: none; border-radius: 3px; }}
QProgressBar::chunk {{ background: {CYAN}; border-radius: 3px; }}
QPushButton#pick {{ background: {PANEL_HI}; border: 1px solid {LAVENDER}; border-radius: 9px;
                    color: {INK}; font-weight: bold; padding: 0 14px; }}
QPushButton#pick:hover {{ background: #1b2150; }}
QPushButton#go {{ background: {CYAN}; color: {BG}; border: none; border-radius: 9px;
                  font-weight: bold; padding: 0 16px; }}
QPushButton#go:disabled, QPushButton#pick:disabled {{ background: {PANEL_HI}; color: {DIM};
                                                      border: 1px solid {LINE}; }}
QScrollBar:vertical {{ background: {PANEL}; width: 10px; margin: 0; border: none; }}
QScrollBar::handle:vertical {{ background: {LINE}; border-radius: 5px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {VIOLET}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
"""


def launch() -> int:
    try:
        from PyQt6.QtWidgets import QApplication  # noqa: F401
    except Exception:
        print("PyQt6 is required for the ENVY Tools window. Run 'envy-tools --help' for the "
              "command-line version.")
        return 2
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("No display found. Run 'envy-tools --help' for the command-line version.")
        return 2
    QApplication, ToolsWindow = _build()
    app = QApplication.instance() or QApplication(sys.argv)
    window = ToolsWindow()
    window.show()
    return app.exec()
