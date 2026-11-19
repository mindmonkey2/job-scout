#!/usr/bin/env python3
"""Job Scout desktop app: review matches, edit and copy proposals, and run the scanner from a window and tray icon.

The scanner itself keeps running as the background service (`scout.py loop`), so alerts still arrive
when this window is closed. The app reads the same scout.db and draft files, and never submits a bid.

  scout_gui.py                 open the window (or bring the running one forward)
  scout_gui.py --job <id>      open on one job
  scout_gui.py --hidden        start in the tray (used at login)
  scout_gui.py --install       add Job Scout to the app menu
Needs PySide6 (Fedora: sudo dnf install python3-pyside6; elsewhere: pip install PySide6).
"""
import argparse
import importlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

try:
    from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
    from PySide6.QtGui import (QAction, QBrush, QColor, QDesktopServices, QFont, QGuiApplication, QIcon,
                               QKeySequence, QPainter, QPen, QPixmap)
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import (QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
                                   QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
                                   QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox,
                                   QSplitter, QSystemTrayIcon, QTabBar, QTableWidget, QTableWidgetItem, QTabWidget,
                                   QTextBrowser, QVBoxLayout, QWidget)
except ImportError:
    sys.exit("The desktop app needs PySide6. Fedora: sudo dnf install python3-pyside6; "
             "elsewhere: pip install PySide6. The terminal tool (scout.py) works without it.")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scout  # noqa: E402

APP_ID = "job-scout"
SERVICE = "job-scout"
LIMIT = scout.FREELANCER_LIMIT
STATUS_LABEL = {"new": "No draft", "drafted": "Draft ready", "opened": "Opened", "submitted": "Submitted",
                "sent": "Submitted", "skipped": "Skipped", "locked": "Locked"}
FILTERS = [("To review", {"new", "drafted"}), ("Opened", {"opened"}), ("Submitted", {"submitted", "sent"}),
           ("Skipped", {"skipped", "locked"}), ("All", None)]
SCAN_LINE = re.compile(r"^(\d\d:\d\d) scanned (\d+) fresh jobs: (\d+) match, (\d+) new")
RED, GREEN, AMBER = QColor("#d1242f"), QColor("#1a7f37"), QColor("#bf8700")


# ---------- background work ----------

class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn, signals):
        super().__init__()
        self.fn, self.signals = fn, signals

    def run(self):
        try:
            result = self.fn()
        except BaseException as e:  # scout raises SystemExit for setup problems; show them, don't die
            result, error = None, str(e) or type(e).__name__
        else:
            error = None
        try:
            self.signals.failed.emit(error) if error is not None else self.signals.done.emit(result)
        except RuntimeError:  # the app quit while this ran
            pass


_RUNNING = set()  # keeps signal objects alive until their task reports back


def run_bg(fn, on_done=None, on_fail=None):
    """Run fn on a worker thread; on_done/on_fail run back on the UI thread."""
    sig = _Signals()
    _RUNNING.add(sig)
    if on_done:
        sig.done.connect(on_done, Qt.QueuedConnection)
    if on_fail:
        sig.failed.connect(on_fail, Qt.QueuedConnection)
    for s in (sig.done, sig.failed):
        s.connect(lambda *_: _RUNNING.discard(sig), Qt.QueuedConnection)
    QThreadPool.globalInstance().start(_Task(fn, sig))


# ---------- helpers ----------

def make_icon(size=256, count=0):
    """The radar icon, drawn in code so no image files or SVG plugin are needed. Optional count badge."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = float(size)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#1f6feb"))
    p.drawEllipse(QRectF(s * .04, s * .04, s * .92, s * .92))
    p.setBrush(Qt.NoBrush)
    p.setPen(QPen(QColor(255, 255, 255, 150), s * .035))
    for r in (.32, .19):
        p.drawEllipse(QPointF(s * .5, s * .5), s * r, s * r)
    pen = QPen(QColor("white"), s * .055)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(s * .5, s * .5), QPointF(s * .77, s * .27))
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#ffd33d"))
    p.drawEllipse(QPointF(s * .64, s * .64), s * .07, s * .07)
    if count:
        p.setBrush(RED)
        p.drawEllipse(QRectF(s * .5, 0, s * .5, s * .5))
        p.setPen(QColor("white"))
        font = QFont()
        font.setBold(True)
        font.setPixelSize(int(s * (.32 if count < 10 else .24)))
        p.setFont(font)
        p.drawText(QRectF(s * .5, 0, s * .5, s * .5), Qt.AlignCenter, str(count) if count < 100 else "99+")
    p.end()
    return QIcon(pm)


def ago(minutes):
    if minutes is None:
        return "?"
    minutes = max(0, int(minutes))
    if minutes < 60:
        return f"{minutes} min"
    if minutes < 48 * 60:
        return f"{minutes // 60} h"
    return f"{minutes // 1440} d"


def minutes_since(iso):
    try:
        return (datetime.now() - datetime.fromisoformat(iso)).total_seconds() / 60
    except (TypeError, ValueError):
        return None


def split_draft(text):
    """(header, {section name: body}) for a draft file."""
    parts = re.split(r"^## ", text, flags=re.M)
    sections = {}
    for chunk in parts[1:]:
        name, _, body = chunk.partition("\n")
        sections.setdefault(name.strip(), body.strip())
    return parts[0].strip(), sections


def draft_details_md(text):
    """Everything in a draft except the proposal and the pasted original, as Markdown for the side panel."""
    head, sections = split_draft(text)
    head = "\n".join(ln for ln in head.splitlines() if not ln.startswith("# "))
    out = [head]
    for name, body in sections.items():
        if name == "Proposal" or name.startswith("Original job text"):
            continue
        out.append(f"#### {name}\n\n{body}")
    return "\n\n".join(out)


def systemctl(*args):
    try:
        return subprocess.run(["systemctl", "--user", *args, SERVICE], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None


def service_installed():
    res = systemctl("cat")
    return bool(res and res.returncode == 0)


def service_status():
    """(state, last scan line or None) for the background scanner."""
    res = systemctl("is-active")
    state = res.stdout.strip() if res else "unknown"
    last = None
    try:
        log = subprocess.run(["journalctl", "--user", "-u", SERVICE, "-n", "200", "--output=cat", "--no-pager"],
                             capture_output=True, text=True, timeout=10).stdout
        last = next((ln for ln in reversed(log.splitlines()) if SCAN_LINE.match(ln)), None)
    except (OSError, subprocess.SubprocessError):
        pass
    return state, last


class Item(QTableWidgetItem):
    """Table cell that sorts by a key (numbers sort as numbers)."""

    def __init__(self, text, key=None):
        super().__init__(text)
        self.key = text.lower() if key is None else key

    def __lt__(self, other):
        return self.key < other.key


class ProposalEdit(QWidget):
    """Proposal text box with a live character count against Freelancer's limit."""
    changed = Signal()

    def __init__(self, limit=LIMIT):
        super().__init__()
        self.limit = limit
        self.edit = QPlainTextEdit()
        self.edit.setPlaceholderText("No proposal yet.")
        self.count = QLabel()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        title = QLabel("<b>Proposal</b> (edit freely; it saves to the draft file)")
        top.addWidget(title)
        top.addStretch()
        top.addWidget(self.count)
        lay.addLayout(top)
        lay.addWidget(self.edit)
        self.edit.textChanged.connect(self._update)
        self._update()

    def _update(self):
        n = len(self.text())
        if self.limit:
            over = n > self.limit
            self.count.setText(f"{n:,} / {self.limit:,} characters" + ("  — too long for the bid box" if over else ""))
            self.count.setStyleSheet(f"color: {RED.name()}; font-weight: bold;" if over else "")
        else:
            self.count.setText(f"{n:,} characters")
        self.changed.emit()

    def text(self):
        return self.edit.toPlainText().strip()

    def set_text(self, text):
        self.edit.blockSignals(True)
        self.edit.setPlainText(text or "")
        self.edit.blockSignals(False)
        self._update()


def copy_text(text):
    QGuiApplication.clipboard().setText(text)


# ---------- job detail panel ----------

class JobPanel(QWidget):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.row = None
        self.path = None
        self.saved = ""
        self.live = {}

        self.title = QLabel()
        self.title.setWordWrap(True)
        f = self.title.font()
        f.setPointSizeF(f.pointSizeF() * 1.35)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.meta = QLabel()
        self.meta.setWordWrap(True)
        self.meta.setTextFormat(Qt.RichText)
        self.livebox = QLabel()
        self.livebox.setTextFormat(Qt.RichText)

        self.b_copy_open = QPushButton(QIcon.fromTheme("internet-web-browser"), "Copy proposal && open job")
        self.b_copy_open.setDefault(True)
        self.b_copy_open.setToolTip("Saves your edits, copies the proposal and opens the job page (Ctrl+Return)")
        self.b_copy = QPushButton(QIcon.fromTheme("edit-copy"), "Copy proposal")
        self.b_open = QPushButton(QIcon.fromTheme("document-open-remote"), "Open job page")
        self.b_submitted = QPushButton(QIcon.fromTheme("dialog-ok-apply"), "Mark submitted")
        self.b_skip = QPushButton(QIcon.fromTheme("edit-delete-remove"), "Skip")
        self.b_back = QPushButton(QIcon.fromTheme("edit-undo"), "Back to review")
        self.b_redraft = QPushButton(QIcon.fromTheme("view-refresh"), "Redraft…")
        self.b_file = QPushButton(QIcon.fromTheme("document-edit"), "Open draft file")
        self.b_save = QPushButton(QIcon.fromTheme("document-save"), "Save")
        self.b_revert = QPushButton("Undo edits")

        self.proposal = ProposalEdit()
        self.details = QTextBrowser()
        self.details.setOpenExternalLinks(True)

        row1 = QHBoxLayout()
        for b in (self.b_copy_open, self.b_copy, self.b_open):
            row1.addWidget(b)
        row1.addStretch()
        row2 = QHBoxLayout()
        for b in (self.b_submitted, self.b_skip, self.b_back, self.b_redraft, self.b_file):
            row2.addWidget(b)
        row2.addStretch()
        saverow = QHBoxLayout()
        saverow.addStretch()
        saverow.addWidget(self.b_revert)
        saverow.addWidget(self.b_save)

        prop = QWidget()
        pl = QVBoxLayout(prop)
        pl.setContentsMargins(0, 0, 0, 0)
        pl.addWidget(self.proposal)
        pl.addLayout(saverow)
        split = QSplitter(Qt.Vertical)
        split.addWidget(prop)
        split.addWidget(self.details)
        split.setSizes([380, 300])

        self.empty = QLabel("Select a job on the left.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.body = QWidget()
        bl = QVBoxLayout(self.body)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.addWidget(self.title)
        bl.addWidget(self.meta)
        bl.addWidget(self.livebox)
        bl.addLayout(row1)
        bl.addLayout(row2)
        bl.addWidget(split, 1)
        lay = QVBoxLayout(self)
        lay.addWidget(self.empty)
        lay.addWidget(self.body)
        self.body.hide()

        self.b_copy_open.clicked.connect(self.copy_and_open)
        self.b_copy.clicked.connect(self.copy_only)
        self.b_open.clicked.connect(self.open_page)
        self.b_submitted.clicked.connect(lambda: self.set_status("submitted"))
        self.b_skip.clicked.connect(lambda: self.set_status("skipped"))
        self.b_back.clicked.connect(lambda: self.set_status("drafted" if self.path else "new"))
        self.b_redraft.clicked.connect(self.redraft)
        self.b_file.clicked.connect(lambda: self.path and QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path))))
        self.b_save.clicked.connect(self.save)
        self.b_revert.clicked.connect(lambda: self.proposal.set_text(self.saved))
        self.proposal.changed.connect(self._dirty_changed)
        act = QAction(self)
        act.setShortcut(QKeySequence("Ctrl+Return"))
        act.triggered.connect(self.copy_and_open)
        self.addAction(act)

    @property
    def pid(self):
        return self.row["id"] if self.row else None

    def dirty(self):
        return bool(self.row) and self.proposal.edit.isEnabled() and self.proposal.text() != self.saved

    def _dirty_changed(self):
        d = self.dirty()
        self.b_save.setEnabled(d)
        self.b_revert.setEnabled(d)

    def confirm_leave(self):
        """Before switching away: save unsaved edits (asks). Returns False to stay."""
        if not self.dirty():
            return True
        r = QMessageBox.question(self, "Unsaved proposal", "Save your edits to this proposal?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def show_job(self, row):
        """Load a job. Same job again: refresh the labels only, keeping any edits."""
        same = self.row and row and row["id"] == self.row["id"]
        self.row = row
        if not row:
            self.body.hide()
            self.empty.show()
            return
        self.empty.hide()
        self.body.show()
        self._labels()
        if same:
            return
        self.path = Path(row["draft_path"]) if row["draft_path"] else None
        if self.path and self.path.exists():
            text = self.path.read_text()
            self.saved = scout.proposal_text(self.path) or ""
            self.proposal.edit.setEnabled(True)
            self.proposal.set_text(self.saved)
            self.details.setMarkdown(draft_details_md(text))
        else:
            self.path = None
            self.saved = ""
            self.proposal.set_text("")
            self.proposal.edit.setEnabled(False)
            self.proposal.edit.setPlaceholderText("No draft yet (drafting failed or is still running). "
                                                  "Click Redraft to write one now.")
            self.details.setMarkdown("")
        self._dirty_changed()
        self._buttons()
        self.check_live()

    def _labels(self):
        r = self.row
        self.title.setText(r["title"])
        posted = r["age_when_found"] + (minutes_since(r["found_at"]) or 0)
        tags = [t for t in ([("crowded" if r["crowd_warned"] else "")] + (r["tags"] or "").split(", ")) if t]
        tag_html = " ".join(f"<span style='color:{(RED if t.endswith('crowded') else GREEN).name()}'>[{t}]</span>"
                            for t in tags)
        bids = f"{r['bids_when_found']} bids when found"
        if r["bids_now"] is not None and r["bids_now"] != r["bids_when_found"]:
            bids += f", {r['bids_now']} at the last scan"
        state = STATUS_LABEL.get(r["status"], r["status"])
        if r["id"] in self.win.drafting:
            state = "Drafting… (about 30 s)"
        self.meta.setText(f"<b>Score {r['score']}</b> · {r['budget']} · {bids} · posted {ago(posted)} ago · "
                          f"<b>{state}</b> {tag_html}<br><a href='{r['url']}'>{r['url']}</a>")
        self.meta.setOpenExternalLinks(True)
        self.livebox.setText(self.live.get(r["id"], "<i>Checking the live bid count…</i>"))
        self._buttons()

    def _buttons(self):
        st = self.row["status"]
        has = bool(self.path)
        busy = self.row["id"] in self.win.drafting
        self.b_copy.setEnabled(has)
        self.b_file.setEnabled(has)
        self.b_copy_open.setText("Copy proposal && open job" if has else "Open job page")
        self.b_submitted.setVisible(st not in ("submitted", "sent"))
        self.b_skip.setVisible(st not in ("skipped", "locked"))
        self.b_back.setVisible(st not in ("new", "drafted"))
        self.b_redraft.setEnabled(not busy)
        self.b_redraft.setText("Draft now" if not has else "Redraft…")

    def check_live(self):
        pid = self.pid
        self.livebox.setText(self.live.get(pid, "<i>Checking the live bid count…</i>"))

        def done(p):
            if not p:
                msg = "<span style='color:%s'>This job is no longer listed.</span>" % RED.name()
            else:
                bids = p["bid_stats"]["bid_count"]
                st = p.get("status")
                if st != "active":
                    msg = f"<span style='color:{RED.name()}'><b>Closed</b> ({st}), {bids} bids.</span>"
                else:
                    color = RED if bids >= scout.CFG.get("crowd_bids", 30) else GREEN
                    msg = (f"Live now: <b style='color:{color.name()}'>{bids} bids</b>, still open "
                           f"(checked {datetime.now():%H:%M}).")
                con = scout.db()
                con.execute("UPDATE jobs SET bids_now=?, bids_checked_at=? WHERE id=?",
                            (bids, datetime.now().isoformat(timespec="seconds"), pid))
                con.commit()
                con.close()
            self.live[pid] = msg
            if self.pid == pid:
                self.livebox.setText(msg)

        def failed(err):
            self.live[pid] = f"<i>Couldn't check the live job: {err}</i>"
            if self.pid == pid:
                self.livebox.setText(self.live[pid])

        run_bg(lambda: scout.fetch_by_id(pid), done, failed)

    def save(self):
        if not self.path:
            return True
        try:
            scout.save_proposal(self.path, self.proposal.text())
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Couldn't save", str(e))
            return False
        self.saved = self.proposal.text()
        self._dirty_changed()
        self.win.say(f"Saved to {self.path.name}")
        return True

    def _copy(self):
        text = self.proposal.text()
        if not text:
            return False
        if len(text) > LIMIT and QMessageBox.question(
                self, "Proposal too long",
                f"The proposal is {len(text):,} characters; Freelancer's bid box takes {LIMIT:,}. Copy anyway?"
        ) != QMessageBox.Yes:
            return None
        if self.dirty():
            self.save()
        copy_text(text)
        return True

    def copy_only(self):
        if self._copy():
            self.win.say(f"Proposal copied ({len(self.proposal.text()):,} characters). Review it before submitting.")

    def open_page(self):
        QDesktopServices.openUrl(QUrl(self.row["url"]))
        if self.row["status"] in ("new", "drafted"):
            self.set_status("opened")

    def copy_and_open(self):
        if not self.row:
            return
        copied = self._copy() if self.path else False
        if copied is None:
            return
        self.open_page()
        self.win.say(f"Proposal copied ({len(self.proposal.text()):,} characters) and job page opened."
                     if copied else "Job page opened.")

    def set_status(self, status):
        scout.set_status(self.pid, status)
        self.win.refresh(force=True)

    def redraft(self):
        pid, title = self.pid, self.row["title"]
        if self.dirty() and QMessageBox.question(
                self, "Replace your edits?", "Redrafting replaces the proposal, including your unsaved edits. "
                                             "Continue?") != QMessageBox.Yes:
            return
        note, ok = QInputDialog.getMultiLineText(
            self, "Redraft", "Optional note for this draft (for example: shorter, mention my Odoo work, "
                             "hourly rate 15/hr). Leave empty for a fresh draft.")
        if not ok:
            return
        self.win.drafting.add(pid)
        self._labels()
        self.win.say(f"Drafting “{title[:50]}” (about 30 seconds)…")

        def done(path):
            self.win.drafting.discard(pid)
            self.win.say(f"New draft ready for “{title[:50]}”.")
            if self.pid == pid:
                self.row = None  # force a full reload of the panel
            self.win.refresh(force=True)

        def failed(err):
            self.win.drafting.discard(pid)
            if self.pid == pid:
                self._labels()
            QMessageBox.warning(self.win, "Drafting failed", err)

        run_bg(lambda: scout.redraft(pid, note.strip() or None), done, failed)


# ---------- paste mode ----------

class PasteDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.path = None
        self.setWindowTitle("Draft from a pasted job (Upwork and others)")
        self.resize(900, 760)
        self.platform = QComboBox()
        for key, pf in sorted(scout.PLATFORMS.items(), key=lambda kv: kv[0] != "upwork"):
            self.platform.addItem(pf["name"], key)
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://www.upwork.com/jobs/… (optional, saved in the draft)")
        self.title_in = QLineEdit()
        self.title_in.setPlaceholderText("Optional; taken from the first line")
        self.note = QLineEdit()
        self.note.setPlaceholderText("Optional note for the drafter, e.g. keep it short")
        self.text = QPlainTextEdit()
        self.text.setPlaceholderText("Paste the whole job post: title, description, budget, client info "
                                     "and any screening questions.")
        b_paste = QPushButton(QIcon.fromTheme("edit-paste"), "Paste from clipboard")
        b_paste.clicked.connect(lambda: self.text.setPlainText(QGuiApplication.clipboard().text()))
        self.b_draft = QPushButton(QIcon.fromTheme("document-new"), "Draft proposal")
        self.b_draft.clicked.connect(self.start)
        self.state = QLabel()

        self.proposal = ProposalEdit(limit=None)
        self.details = QTextBrowser()
        b_copy = QPushButton(QIcon.fromTheme("edit-copy"), "Copy proposal")
        b_copy.clicked.connect(self.copy)
        b_file = QPushButton(QIcon.fromTheme("document-edit"), "Open draft file")
        b_file.clicked.connect(lambda: self.path and QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path))))
        self.result = QWidget()
        rl = QVBoxLayout(self.result)
        rl.setContentsMargins(0, 0, 0, 0)
        rs = QSplitter(Qt.Vertical)
        rs.addWidget(self.proposal)
        rs.addWidget(self.details)
        rl.addWidget(rs)
        br = QHBoxLayout()
        br.addStretch()
        br.addWidget(b_file)
        br.addWidget(b_copy)
        rl.addLayout(br)
        self.result.hide()

        form = QFormLayout()
        form.addRow("Platform", self.platform)
        form.addRow("Job link", self.url)
        form.addRow("Title", self.title_in)
        form.addRow("Note", self.note)
        top = QHBoxLayout()
        top.addWidget(b_paste)
        top.addStretch()
        top.addWidget(self.state)
        top.addWidget(self.b_draft)
        self.input = QWidget()
        il = QVBoxLayout(self.input)
        il.setContentsMargins(0, 0, 0, 0)
        il.addLayout(form)
        il.addWidget(self.text, 1)
        il.addLayout(top)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.input)
        split.addWidget(self.result)
        lay = QVBoxLayout(self)
        lay.addWidget(split)

    def start(self):
        text = self.text.toPlainText().strip()
        if len(text) < scout.PASTE_MIN:
            QMessageBox.information(self, "Not enough text", f"Only {len(text)} characters. Copy the whole job post "
                                    "(title, description, budget, client info and any questions).")
            return
        platform = self.platform.currentData()
        self.proposal.limit = LIMIT if platform == "freelancer" else None
        self.b_draft.setEnabled(False)
        self.state.setText("Drafting (about 30 seconds)…")
        args = (text, platform, self.url.text().strip() or None, self.title_in.text().strip() or None,
                self.note.text().strip() or None)
        run_bg(lambda: scout.draft_pasted(*args), self.done, self.failed)

    def done(self, res):
        self.path, body = res
        self.b_draft.setEnabled(True)
        self.state.setText(f"Saved as {self.path.name}")
        self.proposal.set_text(scout.section(body, "Proposal") or "")
        self.details.setMarkdown(draft_details_md(body))
        self.result.show()
        self.win.say(f"Paste draft saved: {self.path}")

    def failed(self, err):
        self.b_draft.setEnabled(True)
        self.state.setText("")
        QMessageBox.warning(self, "Drafting failed", err)

    def copy(self):
        text = self.proposal.text()
        if not text:
            return
        if self.path:
            try:
                scout.save_proposal(self.path, text)
            except (OSError, ValueError):
                pass
        copy_text(text)
        self.state.setText(f"Copied ({len(text):,} characters). Review it before pasting.")


# ---------- settings ----------

NUMBERS = [
    ("scan_every_min", "Scan every (minutes)", 1, 120),
    ("score_threshold", "Alert when score is at least", 0, 100),
    ("max_age_min", "Ignore jobs older than (minutes)", 5, 1440),
    ("max_bids", "Ignore jobs with more bids than", 1, 500),
    ("min_fixed_usd", "Minimum fixed budget (USD)", 0, 100000),
    ("min_hourly_usd", "Minimum hourly rate (USD)", 0, 1000),
    ("crowd_bids", "“Crowded” alert at (bids)", 1, 1000),
    ("crowd_rate_per_min", "“Crowded” when bids per minute reach", 1, 100),
    ("hot_budget_usd", "“Likely crowded” from budget (USD)", 0, 1000000),
    ("hot_penalty", "“Likely crowded” penalty (points)", 0, 100),
    ("niche_bonus", "Niche skill bonus (points)", 0, 100),
    ("draft_workers", "Drafts written at the same time", 1, 8),
]
LISTS = [
    ("queries", "Search terms"), ("strong_skills", "Strong skills"), ("weak_skills", "Weak skills"),
    ("niche_skills", "Niche skills (bonus)"), ("hot_skills", "Trending skills (penalty)"),
    ("off_skills", "Non-development skills"),
]
DEFAULTS = {"scan_every_min": 15}
AUTOSTART = Path.home() / ".config/autostart/job-scout.desktop"


class SettingsDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.setWindowTitle("Job Scout settings")
        self.resize(640, 640)
        self.base = json.loads((HERE / "config.json").read_text())
        self.local_path = HERE / "config.local.json"
        self.local = json.loads(self.local_path.read_text()) if self.local_path.exists() else {}
        cur = {**DEFAULTS, **self.base, **self.local}

        tabs = QTabWidget()
        general = QWidget()
        form = QFormLayout(general)
        self.nums = {}
        for key, label, lo, hi in NUMBERS:
            sb = QSpinBox()
            sb.setRange(lo, hi)
            sb.setValue(int(cur.get(key, lo)))
            form.addRow(label, sb)
            self.nums[key] = sb
        self.skip_local = QCheckBox("Skip on-site jobs that need you in a particular city")
        self.skip_local.setChecked(bool(cur.get("skip_local", True)))
        form.addRow(self.skip_local)
        self.autostart = QCheckBox("Start Job Scout in the tray when I log in")
        self.autostart.setChecked(AUTOSTART.exists())
        form.addRow(self.autostart)
        tabs.addTab(general, "General")
        self.lists = {}
        for key, label in LISTS:
            w = QWidget()
            lay = QVBoxLayout(w)
            lay.addWidget(QLabel(f"One per line, lowercase, as Freelancer names them. ({label})"))
            ed = QPlainTextEdit("\n".join(cur.get(key, [])))
            lay.addWidget(ed)
            self.lists[key] = ed
            tabs.addTab(w, label.split(" (")[0])
        note = QLabel("Saved to config.local.json, so config.json stays as shipped. Spam filters (regexes) are "
                      "edited in the config files directly.")
        note.setWordWrap(True)
        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(note)
        lay.addWidget(bb)

    def values(self):
        out = {k: sb.value() for k, sb in self.nums.items()}
        out["skip_local"] = self.skip_local.isChecked()
        for k, ed in self.lists.items():
            items = [ln.strip().lower() for ln in ed.toPlainText().splitlines() if ln.strip()]
            out[k] = list(dict.fromkeys(items))
        return out

    def save(self):
        local = dict(self.local)
        for k, v in self.values().items():
            if v == self.base.get(k, DEFAULTS.get(k)):
                local.pop(k, None)
            else:
                local[k] = v
        self.local_path.write_text(json.dumps(local, indent=2, ensure_ascii=False) + "\n")
        set_autostart(self.autostart.isChecked())


# ---------- install ----------

def desktop_entry(hidden=False):
    icon = Path.home() / ".local/share/icons/hicolor/256x256/apps/job-scout.png"
    return "\n".join([
        "[Desktop Entry]", "Type=Application", "Name=Job Scout",
        "Comment=Fresh freelance jobs with ready-to-review proposals",
        f"Exec={sys.executable} -I {HERE / 'scout_gui.py'}" + (" --hidden" if hidden else ""),
        f"Icon={icon if icon.exists() else 'system-search'}",
        "Terminal=false", "Categories=Office;Utility;", "StartupWMClass=job-scout", "",
    ])


def set_autostart(on):
    if on:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(desktop_entry(hidden=True))
    elif AUTOSTART.exists():
        AUTOSTART.unlink()


def install():
    icon = Path.home() / ".local/share/icons/hicolor/256x256/apps/job-scout.png"
    icon.parent.mkdir(parents=True, exist_ok=True)
    make_icon(256).pixmap(256, 256).save(str(icon), "PNG")
    app_file = Path.home() / ".local/share/applications/job-scout.desktop"
    app_file.parent.mkdir(parents=True, exist_ok=True)
    app_file.write_text(desktop_entry())
    subprocess.run(["update-desktop-database", str(app_file.parent)], capture_output=True)
    print(f"Added Job Scout to the app menu ({app_file}).")


# ---------- main window ----------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Job Scout")
        self.setWindowIcon(make_icon())
        self.resize(1400, 860)
        self.drafting = set()
        self.rows = []
        self._sig = None
        self._quitting = False
        self.has_service = service_installed()

        # toolbar row
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by title…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda: self.refresh(force=True))
        b_scan = QPushButton(QIcon.fromTheme("view-refresh"), "Scan now")
        b_scan.clicked.connect(self.scan_now)
        b_paste = QPushButton(QIcon.fromTheme("edit-paste"), "Paste a job…")
        b_paste.clicked.connect(self.paste_mode)
        b_settings = QPushButton(QIcon.fromTheme("configure"), "Settings")
        b_settings.clicked.connect(self.settings)
        self.recent = QCheckBox("Only last 24 h")
        self.recent.setChecked(True)
        self.recent.setToolTip("Hide jobs posted more than a day ago; they rarely still need bids")
        self.recent.toggled.connect(lambda: self.refresh(force=True))
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.recent)
        top.addWidget(b_scan)
        top.addWidget(b_paste)
        top.addWidget(b_settings)
        self.b_pause = QPushButton()
        self.b_pause.setToolTip("Turn the background scanner off or on. Off means no scans and no alerts.")
        self.b_pause.clicked.connect(self.toggle_service)
        top.addWidget(self.b_pause)  # Shut down lives in the File menu only, so it isn't clicked by accident

        self.tabs = QTabBar()
        for name, _ in FILTERS:
            self.tabs.addTab(name)
        self.tabs.currentChanged.connect(lambda: self.refresh(force=True))

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Score", "Title", "Budget", "Bids", "Posted", "Status", "Tags"])
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.setAlternatingRowColors(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.sortByColumn(4, Qt.AscendingOrder)
        self.table.itemSelectionChanged.connect(self.on_select)
        self.table.cellClicked.connect(lambda *_: self.on_select())  # also loads an already-highlighted row
        self.table.doubleClicked.connect(lambda *_: self.panel.copy_and_open())

        self.panel = JobPanel(self)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(top)
        ll.addWidget(self.tabs)
        ll.addWidget(self.table)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.panel)
        split.setSizes([700, 700])
        self.setCentralWidget(split)

        # status bar: scanner state
        self.scanner = QLabel()
        b_log = QPushButton(QIcon.fromTheme("text-x-log"), "Scanner log")
        b_log.clicked.connect(self.show_log)
        sb = self.statusBar()
        sb.addPermanentWidget(self.scanner)
        sb.addPermanentWidget(b_log)
        self.service_state = "unknown"

        self._menus()
        self._tray()
        self.refresh(force=True)
        self.update_service()
        t = QTimer(self)
        t.timeout.connect(self.refresh)
        t.start(10_000)
        t2 = QTimer(self)
        t2.timeout.connect(self.update_service)
        t2.start(30_000)
        t3 = QTimer(self)
        t3.timeout.connect(lambda: self.refresh(force=True))  # ages in the Posted column move on
        t3.start(60_000)

    # --- menus and tray ---

    def _menus(self):
        m = self.menuBar().addMenu("&File")
        for text, key, fn in (("&Paste a job…", "Ctrl+P", self.paste_mode), ("Scan &now", "Ctrl+R", self.scan_now),
                              ("&Settings…", "Ctrl+,", self.settings), ("&Quit (scanner keeps running)", "Ctrl+Q", self.quit),
                              ("Shut &down (stop scanning and quit)…", "", self.shutdown)):
            a = m.addAction(text)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(fn)
        m = self.menuBar().addMenu("&View")
        a = m.addAction("&Refresh")
        a.setShortcut(QKeySequence("F5"))
        a.triggered.connect(lambda: self.refresh(force=True))
        m.addAction("Scanner &log").triggered.connect(self.show_log)
        m.addAction("Open &drafts folder").triggered.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(scout.BIDS_DIR))))
        m = self.menuBar().addMenu("&Help")
        m.addAction("&About Job Scout").triggered.connect(lambda: QMessageBox.about(
            self, "Job Scout", "Finds fresh Freelancer.com jobs, drafts honest proposals with Claude and alerts you.\n\n"
                               "It never submits a bid or messages a client: you review and submit every proposal."))

    def _tray(self):
        self.tray = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_icon(), self)
        menu = QMenu()
        menu.addAction("Open Job Scout").triggered.connect(self.bring_up)
        menu.addAction("Scan now").triggered.connect(self.scan_now)
        self.tray_pause = menu.addAction("Stop scanning")
        self.tray_pause.triggered.connect(self.toggle_service)
        menu.addAction("Paste a job…").triggered.connect(lambda: (self.bring_up(), self.paste_mode()))
        menu.addSeparator()
        menu.addAction("Quit app (scanner keeps running)").triggered.connect(self.quit)
        self.tray.setContextMenu(menu)
        self._tray_menu = menu
        self.tray.activated.connect(self._tray_click)
        self.tray.show()

    def _tray_click(self, reason):
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            if self.isVisible() and self.isActiveWindow():
                self.hide()
            else:
                self.bring_up()

    def bring_up(self, job_id=None):
        self.show()
        self.setWindowState(self.windowState() & ~Qt.WindowMinimized)
        self.raise_()
        self.activateWindow()
        if job_id:
            self.select_job(int(job_id))

    def closeEvent(self, e):
        if self.tray and not self._quitting:
            if not self.panel.confirm_leave():
                e.ignore()
                return
            self.hide()  # keep running in the tray
            e.ignore()
            return
        e.accept()

    def quit(self):
        if not self.panel.confirm_leave():
            return
        self._quitting = True
        QApplication.quit()

    def say(self, text):
        self.statusBar().showMessage(text, 12_000)

    # --- data ---

    def load_rows(self):
        con = scout.db()
        con.row_factory = __import__("sqlite3").Row
        rows = [dict(r) for r in con.execute("SELECT * FROM jobs ORDER BY found_at DESC")]
        con.close()
        return rows

    def refresh(self, force=False):
        try:
            rows = self.load_rows()
        except Exception as e:  # database busy or missing: try again on the next tick
            self.say(f"Couldn't read the job database: {e}")
            return
        sig = (repr(rows) + self.search.text() + str(self.tabs.currentIndex()) + str(sorted(self.drafting))
               + str(self.recent.isChecked()))
        if not force and sig == self._sig:
            return
        self._sig = sig
        self.rows = rows
        self._update_tabs()
        self._update_tray()

        wanted = FILTERS[self.tabs.currentIndex()][1]
        shown = [r for r in self.visible(rows) if wanted is None or r["status"] in wanted]
        keep = self.panel.pid
        sort_col = self.table.horizontalHeader().sortIndicatorSection()
        sort_order = self.table.horizontalHeader().sortIndicatorOrder()
        bar = self.table.verticalScrollBar().value()
        self.table.blockSignals(True)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(shown))
        for i, r in enumerate(shown):
            self._fill_row(i, r)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(sort_col, sort_order)
        self.table.blockSignals(False)
        self.table.verticalScrollBar().setValue(bar)

        target = next((r for r in shown if r["id"] == keep), None)
        if target:
            self._select_row_id(keep)
            self.panel.show_job(target)
        elif not self.panel.dirty():  # never drop unsaved edits just because the list changed
            # the shown job left this view (tab switch, filter, marked submitted/skipped): show the first row.
            # Selection is set with signals blocked and loaded directly, so a row that was already
            # highlighted still loads (clicking it again would emit no selection change).
            self.table.blockSignals(True)
            self.table.clearSelection()
            if self.table.rowCount():
                self.table.selectRow(0)
            self.table.blockSignals(False)
            first = self.table.item(0, 1).data(Qt.UserRole) if self.table.rowCount() else None
            self.panel.show_job(next((r for r in shown if r["id"] == first), None))

    @staticmethod
    def posted_min(r):
        return (r["age_when_found"] or 0) + (minutes_since(r["found_at"]) or 0)

    def _fill_row(self, i, r):
        posted = self.posted_min(r)
        now = r["bids_now"] if r["bids_now"] is not None else r["bids_when_found"]
        bids = str(now) + (f"  (+{now - r['bids_when_found']})" if now > r["bids_when_found"] else "")
        tags = ", ".join(t for t in (["crowded"] if r["crowd_warned"] else []) + (r["tags"] or "").split(", ") if t)
        status = STATUS_LABEL.get(r["status"], r["status"])
        if r["id"] in self.drafting:
            status = "Drafting…"
        cells = [Item(str(r["score"]), r["score"]), Item(r["title"]), Item(r["budget"] or ""),
                 Item(bids, now), Item(ago(posted), posted), Item(status), Item(tags)]
        cells[1].setData(Qt.UserRole, r["id"])
        cells[1].setToolTip(r["title"])
        sc = r["score"] or 0
        cells[0].setForeground(QBrush(GREEN if sc >= 80 else AMBER if sc >= 65 else QColor("gray")))
        f = cells[0].font()
        f.setBold(True)
        cells[0].setFont(f)
        if now >= scout.CFG.get("crowd_bids", 30) or r["crowd_warned"]:
            cells[3].setForeground(QBrush(RED))
        if "crowded" in tags:
            cells[6].setForeground(QBrush(RED))
        elif tags:
            cells[6].setForeground(QBrush(GREEN))
        if r["status"] in ("new", "drafted"):
            for c in (cells[1],):
                f = c.font()
                f.setBold(True)
                c.setFont(f)
        for col, c in enumerate(cells):
            self.table.setItem(i, col, c)

    def visible(self, rows):
        """Rows passing the title filter and the 24-hour switch; the tabs then split them by status."""
        q = self.search.text().strip().lower()
        recent = self.recent.isChecked()
        return [r for r in rows if (not q or q in r["title"].lower()) and (not recent or self.posted_min(r) <= 1440)]

    def _update_tabs(self):
        """Tab counts use the same filters as the list, so each number matches the rows you see."""
        rows = self.visible(self.rows)
        for i, (name, wanted) in enumerate(FILTERS):
            n = sum(1 for r in rows if wanted is None or r["status"] in wanted)
            self.tabs.setTabText(i, f"{name} ({n})")

    def fresh_count(self):
        limit = scout.CFG["max_age_min"]
        return sum(1 for r in self.rows if r["status"] in ("new", "drafted") and not r["crowd_warned"]
                   and (minutes_since(r["found_at"]) or 1e9) <= limit)

    def _update_tray(self):
        if not self.tray:
            return
        n = self.fresh_count()
        self.tray.setIcon(make_icon(64, n))
        self.tray.setToolTip(f"Job Scout: {n} fresh match{'es' if n != 1 else ''} to review" if n
                             else "Job Scout: nothing new to review")

    def _select_row_id(self, pid):
        for i in range(self.table.rowCount()):
            if self.table.item(i, 1).data(Qt.UserRole) == pid:
                self.table.blockSignals(True)
                self.table.selectRow(i)
                self.table.blockSignals(False)
                return True
        return False

    def on_select(self):
        items = self.table.selectedItems()
        if not items:
            return
        pid = self.table.item(items[0].row(), 1).data(Qt.UserRole)
        if pid == self.panel.pid:
            return
        if not self.panel.confirm_leave():
            self._select_row_id(self.panel.pid)
            return
        self.panel.show_job(next((r for r in self.rows if r["id"] == pid), None))

    def select_job(self, pid):
        if not any(r["id"] == pid for r in self.rows):
            self.refresh(force=True)
        row = next((r for r in self.rows if r["id"] == pid), None)
        if not row:
            self.say(f"Job {pid} isn't in the database.")
            return
        if not self.panel.confirm_leave():
            return
        for i, (_, wanted) in enumerate(FILTERS):
            if wanted is None or row["status"] in wanted:
                if self.tabs.currentIndex() != i:
                    self.tabs.setCurrentIndex(i)
                break
        self.search.clear()
        if self.posted_min(row) > 1440:
            self.recent.setChecked(False)
        self.panel.show_job(row)
        self.refresh(force=True)

    # --- actions ---

    def paste_mode(self):
        PasteDialog(self).show()

    def settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            dlg.save()
        except OSError as e:
            QMessageBox.warning(self, "Couldn't save settings", str(e))
            return
        global scout
        scout = importlib.reload(scout)
        if self.service_state == "active" and QMessageBox.question(
                self, "Restart the scanner?", "Restart the background scanner now so it uses the new settings?"
        ) == QMessageBox.Yes:
            systemctl("restart")
        self.say("Settings saved.")
        self.update_service()

    def scan_now(self):
        """One scan with drafting and alerts. Runs as its own process, like the service does."""
        self.say("Scanning…")
        cmd = [sys.executable, "-I", str(HERE / "scout.py"), "scan", "--draft", "--notify"]

        def run():
            return subprocess.run(cmd, capture_output=True, text=True, timeout=900, cwd=HERE)

        def done(res):
            line = next((ln for ln in res.stdout.splitlines() if SCAN_LINE.match(ln)), None)
            self.say(line or f"Scan finished (exit {res.returncode}). {res.stderr.strip()[-200:]}")
            self.refresh(force=True)

        run_bg(run, done, lambda e: self.say(f"Scan failed: {e}"))

    def update_service(self):
        if not self.has_service:
            self.scanner.setText("Background scanner not installed (see README)")
            self.b_pause.hide()
            if self.tray:
                self.tray_pause.setVisible(False)
            return

        def done(res):
            state, last = res
            self.service_state = state
            running = state == "active"
            text = "Scanner: <b style='color:%s'>%s</b>" % ((GREEN if running else RED).name(),
                                                           "running" if running else state)
            m = SCAN_LINE.match(last or "")
            if m and running:
                text += f" · last scan {m[1]}: {m[2]} jobs, {m[3]} match"
            self.scanner.setText(text)
            self.b_pause.setText("Stop scanning" if running else "Start scanning")
            self.b_pause.setIcon(QIcon.fromTheme("media-playback-stop" if running else "media-playback-start"))
            if self.tray:
                self.tray_pause.setText(self.b_pause.text())

        run_bg(service_status, done)

    def service_cmd(self, *args):
        res = systemctl(*args)
        if res is None or res.returncode:
            QMessageBox.warning(self, "Scanner", f"systemctl --user {' '.join(args)} {SERVICE} failed:\n"
                                                 f"{(res.stderr if res else '').strip()}")
            return False
        return True

    def toggle_service(self):
        if self.service_state == "active":
            ok = self.service_cmd("stop")
            self.say("Scanner stopped: no scans or alerts until you start it again (or log in next time)." if ok else "")
        else:
            ok = self.service_cmd("enable", "--now")  # also undoes "don't start at login"
            self.say("Scanner started; it scans right away." if ok else "")
        QTimer.singleShot(800, self.update_service)

    def shutdown(self):
        """Stop the background scanner and quit the app."""
        if not self.panel.confirm_leave():
            return
        if self.has_service:
            box = QMessageBox(QMessageBox.Question, "Shut down Job Scout",
                              "Stop the background scanner and close Job Scout?\n\n"
                              "No scans or alerts will run until you open Job Scout and press Start scanning.",
                              QMessageBox.Yes | QMessageBox.Cancel, self)
            box.button(QMessageBox.Yes).setText("Shut down")
            at_login = QCheckBox("Also don't start the scanner when I log in")
            box.setCheckBox(at_login)
            if box.exec() != QMessageBox.Yes:
                return
            if not self.service_cmd(*(("disable", "--now") if at_login.isChecked() else ("stop",))):
                return
        self._quitting = True
        QApplication.quit()

    def show_log(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Scanner log")
        dlg.resize(900, 600)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setFont(QFont("monospace"))

        def load():
            try:
                out = subprocess.run(["journalctl", "--user", "-u", SERVICE, "-n", "300", "--no-pager",
                                      "--output=short"], capture_output=True, text=True, timeout=10).stdout
            except (OSError, subprocess.SubprocessError) as e:
                out = str(e)
            view.setPlainText(out or "No log yet.")
            view.verticalScrollBar().setValue(view.verticalScrollBar().maximum())

        b = QPushButton(QIcon.fromTheme("view-refresh"), "Refresh")
        b.clicked.connect(load)
        lay = QVBoxLayout(dlg)
        lay.addWidget(view)
        lay.addWidget(b, 0, Qt.AlignRight)
        load()
        dlg.show()


# ---------- single instance ----------

def server_name():
    return f"{APP_ID}-{os.getuid()}"


def forward_to_running(message):
    """If the app already runs, hand it the message and return True."""
    sock = QLocalSocket()
    sock.connectToServer(server_name())
    if not sock.waitForConnected(500):
        return False
    sock.write(message.encode())
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--job", type=int, help="open on this project ID")
    ap.add_argument("--hidden", action="store_true", help="start in the tray")
    ap.add_argument("--install", action="store_true", help="add Job Scout to the app menu and exit")
    args = ap.parse_args()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Job Scout")
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(make_icon())
    if args.install:
        install()
        return
    if forward_to_running(f"show {args.job or ''}".strip()):
        return
    QLocalServer.removeServer(server_name())  # stale socket from a crash
    server = QLocalServer()
    server.listen(server_name())

    win = MainWindow()
    app.setQuitOnLastWindowClosed(win.tray is None)

    def incoming():
        conn = server.nextPendingConnection()
        conn.waitForReadyRead(1000)
        parts = bytes(conn.readAll()).decode(errors="ignore").split()
        if parts and parts[0] == "show":
            win.bring_up(parts[1] if len(parts) > 1 and parts[1].isdigit() else None)
        conn.disconnectFromServer()

    server.newConnection.connect(incoming)
    if not (args.hidden and win.tray):
        win.bring_up(args.job)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
