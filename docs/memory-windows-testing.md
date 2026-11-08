
> **Platform scope update (2026-11-02):** Multi-Agent-AI targets **Windows 10/11 and Linux** with one shared Python orchestration engine and Tkinter control panel. Windows uses a PyInstaller/Inno Setup installer; Linux uses a per-user archive installer. Platform-specific notes below describe implementation details or historical Windows tests, **not a Windows-only product restriction**. Linux provider execution and both installers still require end-to-end verification.

# Persistent memory M1 — Windows test guide

**Status:** Developer preview. Do not expose the database to untrusted agents or treat stored text as trusted instructions. Memory is currently a manual CLI subsystem; automatic worker retrieval and verified actor authorization are not yet implemented.

## Prerequisites

Windows 10/11, Git, Python 3.12 (with `py` launcher), PowerShell, and a local clone of `main` (the former `feature/persistent-memory-m1` branch has been merged).

```powershell
git clone https://github.com/jtylek/Multi-Agent-AI.git
cd Multi-Agent-AI
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q
```

## Manual smoke test

Run from the repository root in PowerShell:

```powershell
$cli = ".\.venv\Scripts\multi-agent-ai.exe"
& $cli init --project-root .
& $cli memory init --state-root .multi-agent-ai
"Copilot shim behavior must be verified on Windows" | Set-Content -Encoding utf8 memory-note.txt
& $cli memory add --state-root .multi-agent-ai --project test-project --kind lesson --title "Windows CLI" --content-file memory-note.txt --actor human --source-type task --source-ref smoke-test
```

Copy the returned `id`, then:

```powershell
& $cli memory show --state-root .multi-agent-ai --project test-project <ID>
& $cli memory promote --state-root .multi-agent-ai --project test-project <ID> --expected-revision 1 --actor human --reason "manual smoke test" --evidence "test:manual"
& $cli memory search --state-root .multi-agent-ai --project test-project "Copilot"
& $cli memory doctor --state-root .multi-agent-ai
& $cli memory retract --state-root .multi-agent-ai --project test-project <ID> --expected-revision 2 --actor human --reason "smoke test cleanup"
```

Replace `<ID>` with the returned UUID; do not type the angle brackets. Delete `memory-note.txt` when finished. The `.multi-agent-ai` state directory is excluded from Git by project policy; verify your local `.gitignore` before committing anything.

## Expected results

- `init` and `memory init` succeed.
- Added memory starts as `proposed` and does not appear in normal search.
- Promotion makes it searchable only under `test-project`.
- Stale revisions fail without changing the record.
- Retraction hides it from normal search while retaining revision history.
- `memory doctor` reports SQLite integrity `ok`.
- No model API quota is used by memory CLI commands.

## Limitations

- CLI actor names are self-declared, **not authenticated**. Do not grant this command to autonomous or untrusted workers.
- Secret detection is best-effort and not a substitute for reviewing memory content.
- No automatic injection into Claude/Codex/Copilot prompts yet.
- Windows CI provides a reproducible verification environment, but passing CI is not proof that real provider orchestration works.
