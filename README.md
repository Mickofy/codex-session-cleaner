# Codex Session Cleaner v3 — Windows desktop utility

A local Python/Tkinter utility for reviewing, filtering, sorting, multi-selecting, archiving and deleting Codex sessions. It reads Codex session files and SQLite metadata **without editing them**. Archive/delete actions use the installed Codex CLI.

> **Unofficial community tool (beta).** Not affiliated with or endorsed by OpenAI. Session formats and CLI commands may change between Codex versions. Review the **Safety** and **Limitations** sections before using destructive actions.

## What's new in v3

- **Short buttons:** **Select visible**, **Clear visible**, **Clear all**, **Protect**, **Unprotect**, **Archive**, **Delete permanently**, and **Delete + backup**.
- **Sort by** **Date**, **Project**, or **Status**, with explicit order choices:
  - **Date:** Newest first / Oldest first (default: Newest first)
  - **Project:** A-Z / Z-A (case-insensitive)
  - **Status:** Active first / Archived first
- For project and status sorts, **newer sessions appear first within each group**.
- **Protect** and **Unprotect** are separate, explicit actions (previously a single toggle).
- Selection and filters work as before. Sorting **never** changes which sessions are checked.

## Windows desktop app (.exe)

**Recommended for Windows users:** Download the portable app from GitHub Actions. Python is *not* required to run the packaged application; the installed **Codex CLI is still required** to archive or delete sessions.

1. Open the [Windows app workflow](https://github.com/Mickofy/codex-session-cleaner/actions/workflows/build-windows.yml).
2. Choose the most recent **successful run on `main`** and download the **CodexSessionCleaner-Windows-x64** artifact.
3. Extract the downloaded ZIP and double-click **`CodexSessionCleaner.exe`**. You can create a desktop shortcut to the EXE.
4. Before using archive/delete features, make sure the Codex CLI is installed and `codex --version` works in PowerShell. Close active Codex sessions before making changes.

This is a **portable app, not an installer**: there is no setup wizard, automatic update service, or Start Menu entry. New versions are downloaded manually. The public beta EXE is **not code-signed**; Windows SmartScreen or antivirus may warn about an unfamiliar publisher. Verify the download's GitHub origin and do not disable system protections indiscriminately.

**Build it yourself on Windows:**

```powershell
py -3.12 -m pip install pyinstaller==6.22.3
py -3.12 -m PyInstaller --clean --noconfirm --onefile --windowed --name CodexSessionCleaner codex_session_cleaner.py
```

The output is at `dist/CodexSessionCleaner.exe`. The packaging workflow also runs a **non-destructive smoke test** using a temporary, empty Codex home. No real Codex session deletion is tested by CI.

## Run from Python (for development)

1. Extract the ZIP.
2. Open PowerShell in the extracted `codex-session-cleaner` directory.
3. Check the prerequisites:

   ```powershell
   python --version
   python -m tkinter
   codex --version
   codex delete --help
   ```

4. Start the app:

   ```powershell
   python .\codex_session_cleaner.py
   ```

## How to use

1. Filter sessions by **Search**, **Project**, **Status**, **Age**, or **Protection**.
2. Pick **Sort by** (**Date**, **Project**, **Status**) and an **Order**.
3. Use the checkboxes to select individual sessions. **Select visible** checks all currently displayed rows; **Clear visible** unchecks them; **Clear all** unchecks everything. **Selected only** shows all checked sessions, even if another filter hides them.
4. Use **Protect** or **Unprotect** on the selected sessions if needed.
5. Choose **Archive**, **Delete + backup**, or **Delete permanently**.

**Selections persist when filters or sorting change.** Actions apply to **all checked sessions**, including those hidden by a filter. Before deletion, the confirmation displays the number of hidden selected sessions.

### Delete modes

- **Delete + backup:** Save a ZIP of selected JSONL transcripts, then call Codex `delete` for each selected session. Confirmation phrase: `DELETE N`.
- **Delete permanently:** Call the same Codex `delete` command **without creating a ZIP backup**. Confirmation phrase: `PERMANENT DELETE N`.

`N` is the number of checked sessions in the action. A ZIP transcript backup is **not a complete one-click restore**.

## Safety

- Close Codex CLI, Codex Desktop, and background agents before archive/delete operations. The Windows process guard checks some known names but is not exhaustive.
- Delete is blocked for sessions that are pinned, manually protected, modified within the last 24 hours, missing from the local Codex SQLite index, or missing their rollout file.
- Archive is blocked for protected, pinned, or already archived sessions.
- Session eligibility is checked again against local state immediately before each command.
- A batch stops if a CLI command fails. It does not automatically retry partial deletions.
- Locally protected IDs are stored in `~/CodexSessionCleaner/protected.json`.
- Optional backups are stored in `~/CodexSessionCleaner/backups/`.

## Limitations

- This tool lists **local JSONL rollout-backed sessions**, not necessarily every session displayed by the Codex app.
- **Permanent deletion is not secure disk erasure** and does not guarantee deletion of cloud copies or backups made elsewhere.
- Deleting a parent Codex session may affect descendant sessions that were not individually selected; check related sessions before deleting.
- A session's age or status does not prove that its task has been completed.
- Codex CLI behavior/storage may change with versions. Confirm that `codex delete --help` supports the command on your installed version.
- A reported Codex issue can leave stale database metadata after transcript deletion; if this happens, stop and inspect state before retrying. [Issue #39919](https://github.com/openai/codex/issues/39919).
- ZIPs may contain sensitive conversation details. Store or dispose of them securely.

## Tests

Synthetic tests, with no real Codex sessions deleted:

```powershell
python -m unittest -v test_cleaner.py
```

On Linux, UI tests require a working display, for example `xvfb-run -a python -m unittest -v test_cleaner.py`.

**Validated:** 10 synthetic unit and GUI tests, including sort orders, selection preservation, protection actions, and backed-up/no-backup delete requests. **Not tested** against your local Windows Codex installation.

## Open source and contributions

Released under the [MIT License](LICENSE). Contributions and issue reports are welcome. Please do not attach real Codex session transcripts, local indexes, credentials, or backups to public issues.

GitHub Actions runs Windows core tests and Linux GUI tests under a virtual display. These automated tests use synthetic fixtures and mocks; they **do not** validate permanent deletion on real Codex installations.
