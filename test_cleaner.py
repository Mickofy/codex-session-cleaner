"""Synthetic regression tests: no real Codex CLI, sessions, or database modified."""
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

spec = importlib.util.spec_from_file_location("cleaner", Path(__file__).parent / "codex_session_cleaner.py")
mod = importlib.util.module_from_spec(spec)
sys.modules["cleaner"] = mod
spec.loader.exec_module(mod)


def fixture(home: Path, *, name="demo", prompt="Improve storefront filters", age_days=30, archived=False, pinned=False):
    sid = str(uuid4())
    folder = "archived_sessions" if archived else "sessions"
    path = home / folder / "2026" / "09" / "01" / f"rollout-2026-09-01T12-00-00-{sid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    cwd = str(home / name)
    meta = {"timestamp": "2026-09-01T12:00:00Z", "type": "session_meta",
            "payload": {"id": sid, "cwd": cwd, "timestamp": "2026-09-01T12:00:00Z"}}
    event = {"timestamp": "2026-09-01T12:00:01Z", "type": "event_msg",
             "payload": {"type": "user_message", "message": prompt}}
    path.write_text(json.dumps(meta) + "\n" + json.dumps(event) + "\n", encoding="utf-8")
    old = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
    os.utime(path, (old, old))
    db = home / "state_5.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS threads (id TEXT, is_pinned INTEGER, archived INTEGER, title TEXT, cwd TEXT)")
    conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?)",
                 (sid, int(pinned), int(archived), prompt, cwd))
    conn.commit()
    conn.close()
    return sid, path


class CleanerTests(unittest.TestCase):
    def test_discover_and_restrictions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sid, _ = fixture(home)
            sessions, err = mod.discover(home, set())
            self.assertEqual(len(sessions), 1)
            self.assertEqual(sessions[0].title, "Improve storefront filters")
            self.assertEqual(sessions[0].preview, "Improve storefront filters")
            self.assertFalse(sessions[0].pinned)
            self.assertEqual(sessions[0].reason_blocked(), "")
            protected, _ = mod.discover(home, {sid})
            self.assertIn("manually protected", protected[0].reason_blocked())
            conn = sqlite3.connect(home / "state_5.sqlite")
            conn.execute("UPDATE threads SET is_pinned=1")
            conn.commit()
            conn.close()
            pinned, _ = mod.discover(home, set())
            self.assertIn("pinned", pinned[0].reason_blocked())

    def test_missing_db_blocks_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            sid = str(uuid4())
            path = home / "archived_sessions" / f"rollout-2026-01-01T00-00-00-{sid}.jsonl"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "cwd": "/tmp/example"}}) + "\n")
            sessions, warn = mod.discover(home, set())
            self.assertEqual(len(sessions), 1)
            self.assertTrue(sessions[0].archived)
            self.assertIn("pin status unknown", sessions[0].reason_blocked())

    def test_sort_date_project_and_status_both_directions(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            old_active, _ = fixture(home, name="Zeta", prompt="Old active", age_days=45)
            new_archived, _ = fixture(home, name="Alpha", prompt="New archived", age_days=8, archived=True)
            newest_active, _ = fixture(home, name="Alpha", prompt="Newest active", age_days=3)
            sessions, _ = mod.discover(home, set())

            def sorted_ids(by, order):
                return [s.uid for s in mod.sort_sessions(sessions, sort_by=by, order=order)]

            self.assertEqual(sorted_ids("Date", "Newest first"), [newest_active, new_archived, old_active])
            self.assertEqual(sorted_ids("Date", "Oldest first"), [old_active, new_archived, newest_active])
            self.assertEqual(sorted_ids("Project", "A-Z"), [newest_active, new_archived, old_active])
            self.assertEqual(sorted_ids("Project", "Z-A"), [old_active, newest_active, new_archived])
            self.assertEqual(sorted_ids("Status", "Active first"), [newest_active, old_active, new_archived])
            self.assertEqual(sorted_ids("Status", "Archived first"), [new_archived, newest_active, old_active])
            with self.assertRaises(ValueError):
                mod.sort_sessions(sessions, sort_by="Not a field")

    def test_project_status_age_protection_and_checked_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            a, _ = fixture(home, name="Alpha", prompt="Fix mobile nav", age_days=40)
            b, _ = fixture(home, name="Beta", prompt="Implement SEO", age_days=10, archived=True)
            c, _ = fixture(home, name="Alpha", prompt="Urgent issue", age_days=0, pinned=True)
            all_sessions, _ = mod.discover(home, set())
            self.assertEqual({s.uid for s in mod.filter_sessions(all_sessions, project="Alpha")}, {a, c})
            self.assertEqual({s.uid for s in mod.filter_sessions(all_sessions, status="Archived")}, {b})
            self.assertEqual({s.uid for s in mod.filter_sessions(all_sessions, age="Older than 30 days")}, {a})
            self.assertEqual({s.uid for s in mod.filter_sessions(all_sessions, protection="Eligible to delete")}, {a, b})
            self.assertEqual({s.uid for s in mod.filter_sessions(all_sessions, checked_only=True, checked_ids={b})}, {b})
            self.assertEqual({s.uid for s in mod.filter_sessions(all_sessions, query="SEO")}, {b})


@unittest.skipUnless(os.environ.get("DISPLAY") or sys.platform == "win32", "GUI tests need a display / Xvfb")
class CleanerGuiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.a, _ = fixture(self.home, name="Alpha", prompt="Fix mobile nav")
        self.b, _ = fixture(self.home, name="Beta", prompt="Implement SEO")
        self.home_patch = patch.object(mod, "codex_home", return_value=self.home)
        self.home_patch.start()
        self.root = mod.tk.Tk()
        self.root.withdraw()
        self.app = mod.CleanerApp(self.root)
        self.app.state_dir = self.home / "cleaner-config"
        self.app.protection_file = self.app.state_dir / "protected.json"
        self.root.update()

    def tearDown(self):
        self.root.destroy()
        self.home_patch.stop()
        self.tmp.cleanup()

    def test_check_multiple_and_preserve_when_filter_changes(self):
        self.assertEqual(len(self.app.tree.get_children()), 2)
        self.app.check_visible()
        self.assertEqual(self.app.checked_ids, {self.a, self.b})
        self.app.project_var.set("Alpha")
        self.assertEqual(set(self.app.tree.get_children()), {self.a})
        self.assertEqual(len(self.app.checked()), 2)  # Hidden check deliberately persists
        self.assertIn("2 selected (1 visible)", self.app.count_var.get())
        self.app.uncheck_visible()
        self.assertEqual(self.app.checked_ids, {self.b})
        self.app.checked_only_var.set(True)
        self.app.update_view()
        self.assertEqual(len(self.app.tree.get_children()), 0)
        self.app.clear_checked()
        self.assertEqual(self.app.checked_ids, set())

    def test_sort_controls_and_selection_survive_filter_changes(self):
        self.app.check_visible()
        self.assertEqual(self.app.checked_ids, {self.a, self.b})
        self.assertEqual(self.app.sort_var.get(), "Date")
        self.assertEqual(tuple(self.app.order_combo["values"]), ("Newest first", "Oldest first"))
        self.app.sort_var.set("Project")
        self.assertEqual(self.app.order_var.get(), "A-Z")
        self.assertEqual(tuple(self.app.order_combo["values"]), ("A-Z", "Z-A"))
        self.assertEqual(self.app.tree.get_children(), (self.a, self.b))
        self.app.order_var.set("Z-A")
        self.assertEqual(self.app.tree.get_children(), (self.b, self.a))
        self.app.sort_var.set("Status")
        self.assertEqual(self.app.order_var.get(), "Active first")
        self.assertEqual(tuple(self.app.order_combo["values"]), ("Active first", "Archived first"))
        self.app.state_var.set("Archived")
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertEqual(self.app.checked_ids, {self.a, self.b})

    def test_protect_and_unprotect_are_explicit(self):
        self.app.checked_ids = {self.a, self.b}
        self.app.set_protection(True)
        self.assertEqual(self.app.protected_ids, {self.a, self.b})
        self.app.set_protection(False)
        self.assertEqual(self.app.protected_ids, set())

    def test_permanent_delete_uses_cli_and_creates_no_backup(self):
        self.app.checked_ids = {self.a, self.b}
        with (patch.object(mod.shutil, "which", return_value="/mock/codex"),
              patch.object(mod, "codex_running", return_value=False),
              patch.object(mod.simpledialog, "askstring", return_value="PERMANENT DELETE 2") as prompt,
              patch.object(mod.messagebox, "showinfo"),
              patch.object(mod.messagebox, "showerror") as error,
              patch.object(mod.messagebox, "showwarning") as warning,
              patch.object(mod.subprocess, "run", return_value=mod.subprocess.CompletedProcess([], 0, "ok", "")) as run):
            self.app.run_actions("delete_permanent")
        self.assertFalse(error.called)
        self.assertFalse(warning.called)
        self.assertEqual(run.call_count, 2)
        self.assertEqual({x.args[0][2] for x in run.call_args_list}, {self.a, self.b})
        self.assertTrue(all(x.args[0][1] == "delete" and x.args[0][3] == "--force" for x in run.call_args_list))
        self.assertIn("NO BACKUP", prompt.call_args.args[1])
        self.assertFalse((self.app.state_dir / "backups").exists())

    def test_backup_delete_creates_archive(self):
        self.app.checked_ids = {self.a}
        with (patch.object(mod.shutil, "which", return_value="/mock/codex"),
              patch.object(mod, "codex_running", return_value=False),
              patch.object(mod.simpledialog, "askstring", return_value="DELETE 1"),
              patch.object(mod.messagebox, "showinfo"),
              patch.object(mod.messagebox, "showwarning"),
              patch.object(mod.subprocess, "run", return_value=mod.subprocess.CompletedProcess([], 0, "ok", "")) as run):
            self.app.run_actions("delete_backup")
        self.assertEqual(run.call_count, 1)
        backups = list((self.app.state_dir / "backups").glob("*.zip"))
        self.assertEqual(len(backups), 1)
        with mod.ZipFile(backups[0]) as archive:
            self.assertIn(f"rollouts/{self.a}.jsonl", archive.namelist())

    def test_cancel_and_blocked_delete_do_nothing(self):
        self.app.checked_ids = {self.a}
        with (patch.object(mod.shutil, "which", return_value="/mock/codex"),
              patch.object(mod, "codex_running", return_value=False),
              patch.object(mod.simpledialog, "askstring", return_value="WRONG"),
              patch.object(mod.subprocess, "run") as run):
            self.app.run_actions("delete_permanent")
            run.assert_not_called()
        self.app.protected_ids.add(self.a)
        with (patch.object(mod.shutil, "which", return_value="/mock/codex"),
              patch.object(mod, "codex_running", return_value=False),
              patch.object(mod.messagebox, "showwarning") as warn,
              patch.object(mod.subprocess, "run") as run):
            self.app.run_actions("delete_permanent")
            warn.assert_called_once()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
