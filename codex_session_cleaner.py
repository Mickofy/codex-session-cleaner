#!/usr/bin/env python3
"""Local Codex session cleaner (Windows-friendly, stdlib only).

Reads local rollout JSONL and (when available) read-only SQLite metadata.
Never directly mutates Codex storage. Archive/delete only through Codex CLI.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from uuid import UUID
from zipfile import ZIP_DEFLATED, ZipFile

APP_NAME = "Codex Session Cleaner v3"
SORT_ORDERS = {
    "Date": ("Newest first", "Oldest first"),
    "Project": ("A-Z", "Z-A"),
    "Status": ("Active first", "Archived first"),
}
UUID_RE = re.compile(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", re.I)
RECENT_HOURS = 24
MAX_HEADER_LINES = 120
MAX_JSONL_LINE_BYTES = 2_000_000  # Avoid reading enormous embedded tool results
CODEX_PROCESS_NAMES = frozenset({"codex.exe", "codex-app-server.exe"})
CODEX_DESKTOP_SHELL_NAME = "chatgpt.exe"
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_QUERY_LIMITED_INFORMATION = 0x00001000
ERROR_NO_MORE_FILES = 18
ERROR_INVALID_PARAMETER = 87
ERROR_NOT_FOUND = 1168


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")).expanduser()


def parse_dt(value) -> datetime | None:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone()
        except ValueError:
            return None
    return None


def friendly_dt(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def extract_first_prompt(path: Path) -> tuple[dict, str]:
    """Read bounded early rollout lines; no content sent off the machine."""
    meta = {}
    prompt = ""
    try:
        with path.open("rb") as fp:
            for _ in range(MAX_HEADER_LINES):
                raw = fp.readline(MAX_JSONL_LINE_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_JSONL_LINE_BYTES:
                    continue
                try:
                    obj = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                p = obj.get("payload") or {}
                typ = obj.get("type")
                if typ == "session_meta" and isinstance(p, dict):
                    meta = p
                elif typ == "event_msg" and p.get("type") == "user_message":
                    prompt = p.get("message") or ""
                elif typ == "response_item" and p.get("role") == "user":
                    content = p.get("content") or []
                    prompt = "\n".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "input_text")
                if meta and prompt:
                    break
    except (OSError, UnicodeError):
        return {}, ""
    return meta, str(prompt).strip()


def read_index(home: Path) -> tuple[dict[str, dict], bool, str]:
    """Metadata only; SQLite opened read-only. Never patch DB."""
    db = home / "state_5.sqlite"
    if not db.is_file():
        return {}, False, "No Codex SQLite index found; pinned-session status cannot be verified."
    try:
        conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            columns = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
            needed = [s for s in ("id", "is_pinned", "archived", "title", "cwd") if s in columns]
            if "id" not in needed or "is_pinned" not in needed:
                return {}, False, "Codex index schema lacks an ID/pin field; deletes are disabled."
            rows = {}
            for data in conn.execute("SELECT " + ", ".join(needed) + " FROM threads"):
                d = dict(zip(needed, data))
                rows[str(d["id"]).lower()] = d
            return rows, True, ""
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        return {}, False, f"Could not read Codex index: {exc}. Deletes are disabled."


@dataclass
class Session:
    uid: str
    path: Path
    project: str
    cwd: str
    title: str
    preview: str
    modified: datetime
    archived: bool
    pinned: bool | None
    indexed: bool
    protected: bool

    def reason_blocked(self) -> str:
        reasons = []
        if self.pinned is not False:
            reasons.append("pinned or pin status unknown")
        if self.protected:
            reasons.append("manually protected")
        if self.modified > datetime.now(timezone.utc) - timedelta(hours=RECENT_HOURS):
            reasons.append(f"modified in the last {RECENT_HOURS} hours")
        if not self.indexed:
            reasons.append("not verified in Codex index")
        if not self.path.is_file():
            reasons.append("rollout file missing")
        return "; ".join(reasons)


def discover(home: Path, protected_ids: set[str]) -> tuple[list[Session], str]:
    index, safe_index, message = read_index(home)
    records = []
    for section in (home / "sessions", home / "archived_sessions"):
        if not section.is_dir():
            continue
        for root, _, names in os.walk(section):
            for name in names:
                if not (name.startswith("rollout-") and name.endswith(".jsonl")):
                    continue
                match = UUID_RE.search(name)
                if match is None:
                    continue
                uid = match.group(1).lower()
                try:
                    uid = str(UUID(uid))
                except ValueError:
                    continue
                path = Path(root) / name
                try:
                    modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                except OSError:
                    continue
                meta, prompt = extract_first_prompt(path)
                if meta.get("id") and str(meta["id"]).lower() != uid:
                    continue  # Mismatched filename and metadata: don't offer it for deletion
                row = index.get(uid, {})
                cwd = str(row.get("cwd") or meta.get("cwd") or "(unknown project)")
                project = Path(cwd.rstrip("\\/")).name if cwd != "(unknown project)" else cwd
                title = str(row.get("title") or prompt.splitlines()[0] if prompt else row.get("title") or "(untitled)")
                if len(title) > 100:
                    title = title[:97] + "..."
                pin = bool(row["is_pinned"]) if safe_index and "is_pinned" in row else None
                archived = ("archived_sessions" in path.parts) or bool(row.get("archived", False))
                records.append(Session(uid, path, project, cwd, title, prompt, modified,
                                       archived, pin, uid in index, uid in protected_ids))
    records.sort(key=lambda x: x.modified, reverse=True)
    return records, message


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    image_name: str


def _windows_processes() -> list[ProcessInfo]:
    """List process IDs and image names through Toolhelp, without tasklist/WMI."""
    import ctypes

    if sys.platform != "win32":
        raise OSError("Windows process enumeration is unavailable on this platform")

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == wintypes.HANDLE(-1).value:
        error = ctypes.get_last_error()
        raise OSError(error, "Could not enumerate Windows processes")

    processes = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            error = ctypes.get_last_error()
            raise OSError(error, "Could not read the Windows process list")
        while True:
            processes.append(ProcessInfo(entry.th32ProcessID, entry.szExeFile))
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                error = ctypes.get_last_error()
                if error == ERROR_NO_MORE_FILES:
                    break
                raise OSError(error, "Could not read the Windows process list")
        return processes
    finally:
        kernel32.CloseHandle(snapshot)


def _process_image_path(pid: int) -> str | None:
    """Read one candidate process path; None means it exited during inspection."""
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error in {ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND}:
            return None
        raise OSError(error, f"Could not inspect process {pid}")

    try:
        buffer = ctypes.create_unicode_buffer(32768)
        length = wintypes.DWORD(len(buffer))
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(length)):
            error = ctypes.get_last_error()
            if error in {ERROR_INVALID_PARAMETER, ERROR_NOT_FOUND}:
                return None
            raise OSError(error, f"Could not inspect process {pid}")
        return buffer.value
    finally:
        kernel32.CloseHandle(handle)


def _is_cleaner_process(process: ProcessInfo) -> bool:
    """Ignore this process and same-image helper processes from the one-file app."""
    if process.pid == os.getpid():
        return True
    cleaner_names = {"codexsessioncleaner.exe", Path(sys.executable).name.casefold()}
    return process.image_name.casefold() in cleaner_names


def _is_codex_desktop_path(image_path: str) -> bool:
    """Only treat the ChatGPT shell as Codex when it belongs to the Codex MSIX."""
    components = image_path.replace("/", "\\").casefold().split("\\")
    return any(component.startswith("openai.codex_") for component in components)


def codex_running() -> bool | None:
    """Return True/False for Codex activity, or None if inspection is unavailable."""
    if sys.platform != "win32":
        return None
    try:
        for process in _windows_processes():
            if _is_cleaner_process(process):
                continue
            image_name = process.image_name.casefold()
            if image_name in CODEX_PROCESS_NAMES:
                return True
            if image_name == CODEX_DESKTOP_SHELL_NAME:
                image_path = _process_image_path(process.pid)
                if image_path and _is_codex_desktop_path(image_path):
                    return True
        return False
    except OSError:
        return None


def process_guard_message(running: bool | None) -> str | None:
    """Keep an active process separate from an unavailable process check."""
    if running is True:
        return "Codex CLI or Desktop is running. Close it before continuing; no changes made."
    if running is None:
        return "Could not verify whether Codex CLI or Desktop is running. Process detection is unavailable; no changes made."
    return None


def filter_sessions(
    sessions: list[Session], *, query: str = "", project: str = "All projects",
    status: str = "All", age: str = "All ages", protection: str = "All",
    checked_only: bool = False, checked_ids: set[str] | None = None,
    now: datetime | None = None,
) -> list[Session]:
    """Pure, testable filter; filtering never implicitly changes checked IDs."""
    checked_ids = checked_ids or set()
    query = query.casefold().strip()
    days = {
        "Older than 1 day": 1,
        "Older than 7 days": 7,
        "Older than 30 days": 30,
        "Older than 90 days": 90,
    }.get(age)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days) if days else None
    shown = []
    for s in sessions:
        if query and query not in (s.cwd + " " + s.title + " " + s.uid).casefold():
            continue
        if project != "All projects" and s.project != project:
            continue
        if status == "Archived" and not s.archived:
            continue
        if status == "Active" and s.archived:
            continue
        if cutoff and s.modified > cutoff:
            continue
        blocked = s.reason_blocked()
        if protection == "Eligible to delete" and blocked:
            continue
        if protection == "Protected / pinned" and not (s.protected or s.pinned is not False):
            continue
        if checked_only and s.uid not in checked_ids:
            continue
        shown.append(s)
    return shown


def sort_sessions(sessions: list[Session], *, sort_by: str = "Date", order: str = "Newest first") -> list[Session]:
    """Order visible rows without changing checked sessions.

    For project/status, keep newest sessions first within each group.
    """
    if sort_by not in SORT_ORDERS or order not in SORT_ORDERS[sort_by]:
        raise ValueError("Unsupported sort choice")
    # A stable, predictable secondary ordering within project/status groups.
    ordered = sorted(sessions, key=lambda s: (s.modified, s.uid), reverse=True)
    if sort_by == "Date":
        if order == "Oldest first":
            ordered.reverse()
    elif sort_by == "Project":
        ordered.sort(key=lambda s: s.project.casefold(), reverse=order == "Z-A")
    else:
        ordered.sort(key=lambda s: s.archived, reverse=order == "Archived first")
    return ordered


class CleanerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_NAME)
        self.root.geometry("1250x800")
        self.root.minsize(1000, 620)
        self.home = codex_home()
        self.state_dir = Path.home() / "CodexSessionCleaner"
        self.protection_file = self.state_dir / "protected.json"
        self.protected_ids = self.load_protection()
        self.sessions: dict[str, Session] = {}
        self.checked_ids: set[str] = set()
        self.status = tk.StringVar(value="Review sessions before making any changes.")
        self.count_var = tk.StringVar(value="0 checked")
        self.search_var = tk.StringVar()
        self.project_var = tk.StringVar(value="All projects")
        self.age_var = tk.StringVar(value="All ages")
        self.state_var = tk.StringVar(value="All")
        self.protection_var = tk.StringVar(value="All")
        self.checked_only_var = tk.BooleanVar(value=False)
        self.sort_var = tk.StringVar(value="Date")
        self.order_var = tk.StringVar(value="Newest first")
        self.build_ui()
        self.refresh()

    def load_protection(self) -> set[str]:
        try:
            obj = json.loads(self.protection_file.read_text(encoding="utf-8"))
            return set(obj) if isinstance(obj, list) else set()
        except (OSError, ValueError, TypeError):
            return set()

    def save_protection(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.protection_file.write_text(json.dumps(sorted(self.protected_ids), indent=2), encoding="utf-8")

    def build_ui(self):
        top = ttk.Frame(self.root, padding=12)
        top.pack(fill="x")
        ttk.Label(top, text="Codex Session Cleaner", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(top, text=f"Codex folder: {self.home}", foreground="#555555").pack(anchor="w", pady=(3, 3))
        ttk.Label(top, text="Check sessions to act on them. Selections stay checked when you change filters.",
                  foreground="#555555").pack(anchor="w", pady=(0, 10))

        filt1 = ttk.Frame(top)
        filt1.pack(fill="x", pady=(0, 5))
        ttk.Label(filt1, text="Search:").pack(side="left")
        ttk.Entry(filt1, textvariable=self.search_var, width=40).pack(side="left", padx=(6, 15))
        ttk.Label(filt1, text="Project:").pack(side="left")
        self.projects_combo = ttk.Combobox(filt1, state="readonly", textvariable=self.project_var, width=25)
        self.projects_combo.pack(side="left", padx=(6, 15))
        ttk.Label(filt1, text="Status:").pack(side="left")
        ttk.Combobox(filt1, state="readonly", textvariable=self.state_var,
                     values=["All", "Active", "Archived"], width=12).pack(side="left", padx=(6, 12))
        ttk.Button(filt1, text="Refresh", command=self.refresh).pack(side="right")

        filt2 = ttk.Frame(top)
        filt2.pack(fill="x")
        ttk.Label(filt2, text="Age:").pack(side="left")
        ttk.Combobox(filt2, state="readonly", textvariable=self.age_var,
                     values=["All ages", "Older than 1 day", "Older than 7 days",
                             "Older than 30 days", "Older than 90 days"],
                     width=21).pack(side="left", padx=(6, 15))
        ttk.Label(filt2, text="Protection:").pack(side="left")
        ttk.Combobox(filt2, state="readonly", textvariable=self.protection_var,
                     values=["All", "Eligible to delete", "Protected / pinned"],
                     width=20).pack(side="left", padx=(6, 15))
        ttk.Checkbutton(filt2, text="Selected only", variable=self.checked_only_var,
                        command=self.update_view).pack(side="left")
        ttk.Label(filt2, textvariable=self.count_var, font=("Segoe UI", 10, "bold")).pack(side="right")
        sort_bar = ttk.Frame(top)
        sort_bar.pack(fill="x", pady=(7, 0))
        ttk.Label(sort_bar, text="Sort by:").pack(side="left")
        ttk.Combobox(sort_bar, state="readonly", textvariable=self.sort_var,
                     values=list(SORT_ORDERS), width=13).pack(side="left", padx=(6, 14))
        ttk.Label(sort_bar, text="Order:").pack(side="left")
        self.order_combo = ttk.Combobox(sort_bar, state="readonly", textvariable=self.order_var,
                                        values=SORT_ORDERS["Date"], width=16)
        self.order_combo.pack(side="left", padx=(6, 0))
        for var in (self.search_var, self.project_var, self.age_var, self.state_var, self.protection_var,
                    self.order_var):
            var.trace_add("write", lambda *_: self.update_view())
        self.sort_var.trace_add("write", self.on_sort_change)

        main = ttk.Frame(self.root, padding=(12, 8, 12, 8))
        main.pack(expand=True, fill="both")
        columns = ("check", "project", "title", "modified", "status", "pin", "id")
        # 'browse' is for a preview cursor only. Bulk choices are always explicit checkmarks.
        self.tree = ttk.Treeview(main, columns=columns, show="headings", selectmode="browse", height=16)
        for name, label, width in [
            ("check", "Select", 65), ("project", "Project", 145),
            ("title", "Session", 350), ("modified", "Date", 132),
            ("status", "Status", 85), ("pin", "Protection", 135),
            ("id", "Session UUID", 285),
        ]:
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, stretch=name in {"project", "title"}, anchor="center" if name == "check" else "w")
        scroll = ttk.Scrollbar(main, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self.on_tree_click)
        self.tree.bind("<space>", self.on_tree_space)
        self.tree.bind("<<TreeviewSelect>>", self.show_preview)
        self.tree.bind("<Double-Button-1>", self.on_tree_double_click)

        ttk.Label(self.root, text="Session preview (first prompt)",
                  font=("Segoe UI", 10, "bold"), padding=(12, 3)).pack(anchor="w")
        preview_wrap = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        preview_wrap.pack(fill="both")
        self.preview = tk.Text(preview_wrap, height=5, wrap="word", state="disabled", padx=8, pady=8)
        self.preview.pack(fill="both", expand=True)

        selection_bar = ttk.Frame(self.root, padding=(12, 8, 12, 2))
        selection_bar.pack(fill="x")
        ttk.Button(selection_bar, text="Select visible", command=self.check_visible).pack(side="left", padx=(0, 8))
        ttk.Button(selection_bar, text="Clear visible", command=self.uncheck_visible).pack(side="left", padx=(0, 8))
        ttk.Button(selection_bar, text="Clear all", command=self.clear_checked).pack(side="left", padx=(0, 8))
        ttk.Button(selection_bar, text="Unprotect", command=lambda: self.set_protection(False)).pack(side="right")
        ttk.Button(selection_bar, text="Protect", command=lambda: self.set_protection(True)).pack(side="right", padx=(0, 8))

        actions = ttk.Frame(self.root, padding=(12, 5, 12, 7))
        actions.pack(fill="x")
        ttk.Button(actions, text="Archive", command=lambda: self.run_actions("archive")).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Delete + backup",
                   command=lambda: self.run_actions("delete_backup")).pack(side="right", padx=(8, 0))
        ttk.Button(actions, text="Delete permanently",
                   command=lambda: self.run_actions("delete_permanent")).pack(side="right")
        ttk.Label(self.root, textvariable=self.status, padding=(12, 0, 12, 10),
                  wraplength=1180).pack(anchor="w")

    def refresh(self):
        records, warning = discover(self.home, self.protected_ids)
        self.sessions = {x.uid: x for x in records}
        # Discard checkboxes for sessions that no longer exist after a CLI operation.
        self.checked_ids.intersection_update(self.sessions)
        projects = ["All projects", *sorted({s.project for s in records}, key=str.casefold)]
        self.projects_combo["values"] = projects
        if self.project_var.get() not in projects:
            self.project_var.set("All projects")
        self.update_view()
        self.status.set(f"Found {len(records)} local rollout-backed sessions. " +
                        (warning or "Pinned state read from Codex index."))

    def visible(self) -> list[Session]:
        filtered = filter_sessions(
            list(self.sessions.values()), query=self.search_var.get(),
            project=self.project_var.get(), status=self.state_var.get(),
            age=self.age_var.get(), protection=self.protection_var.get(),
            checked_only=self.checked_only_var.get(), checked_ids=self.checked_ids,
        )
        return sort_sessions(filtered, sort_by=self.sort_var.get(), order=self.order_var.get())

    def on_sort_change(self, *_):
        # Each sort has labels that explain its direction (date, alphabet, status).
        choice = self.sort_var.get()
        if choice not in SORT_ORDERS:
            return
        self.order_combo["values"] = SORT_ORDERS[choice]
        self.order_var.set(SORT_ORDERS[choice][0])  # Triggers a single view refresh.

    def update_view(self):
        if not hasattr(self, "tree"):
            return
        selected_preview = self.tree.focus()
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        for s in self.visible():
            protection = "Protected" if s.protected else ("Pinned" if s.pinned else ("Unknown pin" if s.pinned is None else "—"))
            self.tree.insert("", "end", iid=s.uid,
                             values=("☑" if s.uid in self.checked_ids else "☐", s.project, s.title,
                                     friendly_dt(s.modified), "Archived" if s.archived else "Active",
                                     protection, s.uid))
        if selected_preview and self.tree.exists(selected_preview):
            self.tree.selection_set(selected_preview)
            self.tree.focus(selected_preview)
        self.update_count()
        self.show_preview()

    def update_count(self):
        visible_ids = set(self.tree.get_children())
        shown_checked = len(self.checked_ids & visible_ids)
        self.count_var.set(f"{len(visible_ids)} shown  |  {len(self.checked_ids)} selected ({shown_checked} visible)")

    def checked(self) -> list[Session]:
        """Always act on the full, explicitly checked set, even across filters."""
        return [s for s in self.sessions.values() if s.uid in self.checked_ids]

    def toggle_check(self, uid: str):
        if uid not in self.sessions:
            return
        if uid in self.checked_ids:
            self.checked_ids.remove(uid)
        else:
            self.checked_ids.add(uid)
        if self.checked_only_var.get():
            self.update_view()
        else:
            self.tree.set(uid, "check", "☑" if uid in self.checked_ids else "☐")
            self.update_count()
        self.show_preview()

    def on_tree_click(self, event):
        row = self.tree.identify_row(event.y)
        col = self.tree.identify_column(event.x)
        if row and col == "#1":
            self.tree.focus(row)
            self.tree.selection_set(row)
            self.toggle_check(row)
            return "break"

    def on_tree_double_click(self, event):
        row = self.tree.identify_row(event.y)
        if row and self.tree.identify_column(event.x) != "#1":
            self.toggle_check(row)
            return "break"

    def on_tree_space(self, _event):
        uid = self.tree.focus()
        if uid:
            self.toggle_check(uid)
            return "break"

    def check_visible(self):
        self.checked_ids.update(self.tree.get_children())
        self.update_view()

    def uncheck_visible(self):
        self.checked_ids.difference_update(self.tree.get_children())
        self.update_view()

    def clear_checked(self):
        self.checked_ids.clear()
        self.update_view()

    def show_preview(self, *_):
        uid = self.tree.focus()
        s = self.sessions.get(uid)
        if s and self.tree.exists(uid):
            lines = [f"Project: {s.cwd}", f"Session ID: {s.uid}",
                     f"Deletion: {s.reason_blocked() or 'Allowed'}", "",
                     s.preview or "[No readable first prompt]"]
        else:
            lines = ["Choose a session to preview. Check its box to select it."]
        self.preview.configure(state="normal")
        self.preview.delete("1.0", "end")
        self.preview.insert("1.0", "\n".join(lines)[:4000])
        self.preview.configure(state="disabled")

    def set_protection(self, protect: bool):
        chosen = self.checked()
        if not chosen:
            messagebox.showinfo(APP_NAME, "Select at least one session first.")
            return
        for s in chosen:
            if protect:
                self.protected_ids.add(s.uid)
            else:
                self.protected_ids.discard(s.uid)
        self.save_protection()
        self.refresh()

    def make_backup(self, chosen: list[Session]) -> Path:
        path = self.state_dir / "backups" / f"codex-sessions-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        manifest = []
        with ZipFile(path, "w", compression=ZIP_DEFLATED, allowZip64=True) as zf:
            for s in chosen:
                zf.write(s.path, arcname=f"rollouts/{s.uid}.jsonl")
                manifest.append({"session_id": s.uid, "original_path": str(s.path),
                                 "project": s.cwd, "modified": s.modified.isoformat()})
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        return path

    def confirmation_text(self, chosen: list[Session], *, with_backup: bool) -> str:
        # Ensure the user can inspect what will be affected before typing confirmation.
        examples = "\n".join(f"• {s.project}: {s.title[:65]} ({s.uid[:8]})" for s in chosen[:8])
        if len(chosen) > 8:
            examples += f"\n… and {len(chosen) - 8} more checked sessions"
        visible_ids = set(self.tree.get_children())
        hidden = len({s.uid for s in chosen} - visible_ids)
        mode = ("A ZIP transcript backup will be created first. It is not a full restore."
                if with_backup else "NO BACKUP WILL BE CREATED. Conversation history may be unrecoverable.")
        keyword = "DELETE" if with_backup else "PERMANENT DELETE"
        return (
            f"Permanently delete {len(chosen)} CHECKED session(s)?\n\n"
            f"{mode}\n"
            f"{hidden} checked session(s) are hidden by the current filters.\n\n"
            f"Selected sessions:\n{examples}\n\n"
            "Codex may also delete related descendant sessions.\n"
            "This is not secure disk erasure or deletion of cloud copies.\n\n"
            f"Type {keyword} {len(chosen)} to confirm:"
        )

    def run_actions(self, action: str):
        if action not in {"archive", "delete_backup", "delete_permanent"}:
            raise ValueError("Unsupported action")
        chosen = self.checked()
        if not chosen:
            messagebox.showinfo(APP_NAME, "Select at least one session first.")
            return
        command = shutil.which("codex")
        if not command:
            messagebox.showerror(APP_NAME, "Codex CLI not found on PATH. Run 'codex --version' in PowerShell first.")
            return
        running = codex_running()
        if sys.platform == "win32":
            guard_message = process_guard_message(running)
            if guard_message:
                messagebox.showerror(APP_NAME, guard_message)
                return

        # Refresh the actual disk+index state rather than trusting the cached table.
        current, warning = discover(self.home, self.protected_ids)
        current_by_id = {s.uid: s for s in current}
        if action.startswith("delete"):
            blocked = []
            for s in chosen:
                fresh = current_by_id.get(s.uid)
                reason = "session changed or missing" if not fresh or fresh.path != s.path else fresh.reason_blocked()
                if reason:
                    blocked.append((s, reason))
            if blocked:
                messagebox.showwarning(APP_NAME,
                                       "Deletion blocked for these checked sessions:\n" +
                                       "\n".join(f"{s.uid[:8]}: {r}" for s, r in blocked[:12]) +
                                       ("\n..." if len(blocked) > 12 else "") +
                                       "\n\nUncheck these sessions, or archive them instead.")
                return
            chosen = [current_by_id[s.uid] for s in chosen]
            with_backup = action == "delete_backup"
            keyword = "DELETE" if with_backup else "PERMANENT DELETE"
            confirm = simpledialog.askstring(APP_NAME, self.confirmation_text(chosen, with_backup=with_backup), parent=self.root)
            if confirm != f"{keyword} {len(chosen)}":
                return
            backup = None
            if with_backup:
                try:
                    backup = self.make_backup(chosen)
                except (OSError, ValueError) as exc:
                    messagebox.showerror(APP_NAME, f"Backup failed; nothing deleted.\n{exc}")
                    return
            cli_action = "delete"
        else:
            blocked = [(s, "already archived or protected/pinned") for s in chosen
                       if (not (fresh := current_by_id.get(s.uid)) or fresh.archived or fresh.protected or fresh.pinned is not False)]
            if blocked:
                messagebox.showwarning(APP_NAME, "Archive blocked for checked sessions:\n" +
                                       "\n".join(f"{s.uid[:8]}: {reason}" for s, reason in blocked[:12]) +
                                       "\nUncheck ineligible sessions.")
                return
            chosen = [current_by_id[s.uid] for s in chosen]
            if not messagebox.askyesno(APP_NAME, f"Archive {len(chosen)} checked session(s)?\n\nThis keeps the conversation history."):
                return
            cli_action, backup = "archive", None

        results = []
        for s in chosen:
            # A later CLI delete may affect descendants and other session records.
            # Re-read the actual state immediately before each command.
            fresh_by_id = {x.uid: x for x in discover(self.home, self.protected_ids)[0]}
            fresh = fresh_by_id.get(s.uid)
            if not fresh or fresh.path != s.path or (cli_action == "delete" and fresh.reason_blocked()):
                results.append(f"STOPPED {s.uid[:8]}: session changed or became ineligible")
                break
            try:
                args = [command, cli_action, s.uid]
                if cli_action == "delete":
                    args.append("--force")
                run = subprocess.run(args, capture_output=True, text=True, timeout=90, check=False)
                results.append(f"{'OK' if run.returncode == 0 else 'ERROR'} {s.uid[:8]}: "
                               f"{(run.stderr or run.stdout or '').strip()[:170]}")
                if run.returncode:
                    break  # Some builds can partially delete a session and leave stale metadata.
            except (OSError, subprocess.TimeoutExpired) as exc:
                results.append(f"ERROR {s.uid[:8]}: {exc}")
                break
        self.refresh()
        messagebox.showinfo(APP_NAME, "\n".join(results[:35]) +
                            (f"\n\nBackup (transcripts only): {backup}" if backup else "") +
                            "\n\nIf deletion failed, inspect Codex state manually before trying again.")

def main():
    # Test the packaged Windows GUI without touching any actual Codex sessions.
    if sys.argv[1:] == ["--smoke-test"]:
        import tempfile

        previous_home = os.environ.get("CODEX_HOME")
        root = None
        try:
            with tempfile.TemporaryDirectory(prefix="codex-cleaner-smoke-") as tmp:
                os.environ["CODEX_HOME"] = tmp
                root = tk.Tk()
                root.withdraw()
                CleanerApp(root)
                root.update_idletasks()
        finally:
            if root is not None:
                root.destroy()
            if previous_home is None:
                os.environ.pop("CODEX_HOME", None)
            else:
                os.environ["CODEX_HOME"] = previous_home
        return

    if len(sys.argv) > 1:
        raise SystemExit("Unsupported command-line arguments")

    root = tk.Tk()
    CleanerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
