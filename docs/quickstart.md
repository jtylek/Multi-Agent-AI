# Quick start

This guide walks through a safe, local dry run on Windows. It creates a disposable Git
repository and coordination plan, but **does not start an AI agent or use provider quota**.
For the product's architecture and longer-term goals, see the [README](../README.md).

## Prerequisites

- Windows 10 or 11, PowerShell, and Git.
- Python 3.11 or newer. The examples use Python 3.12 through the Windows Python launcher:

  ```powershell
  py -3.12 --version
  git --version
  ```

- A local checkout of Multi-Agent AI. There is no published installer yet.

The dry run does not need a provider CLI. The command checks whether the selected CLI is
available, so choose `codex` or `copilot` only if you have it installed. Do not add `--execute`
for this walkthrough.

## 1. Install the command-line tool

Open PowerShell in the Multi-Agent AI checkout:

```powershell
$InstallRoot = (Get-Location).Path
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'
```

The virtual environment keeps the tool and its development dependencies separate from other
Python projects. The runtime itself uses only the Python standard library. Keep this PowerShell
window open so `$InstallRoot` and `$Demo` remain available in later steps.

## 2. Create a disposable target repository

Create a uniquely named practice folder under your temporary directory:

```powershell
$Demo = Join-Path $env:TEMP ("agent-demo-" + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $Demo | Out-Null
Set-Location $Demo
git init
"A small practice repository." | Set-Content README.md
git add README.md
git -c user.name="Demo User" -c user.email="demo@example.invalid" commit -m "Initial demo"
```

The local `git -c` options set an identity for this one disposable commit without changing
your global Git configuration.

## 3. Initialize the coordination state

Run the installed CLI, pointing it at the target repository:

```powershell
& (Join-Path $InstallRoot '.venv\Scripts\multi-agent-ai.exe') init --project-root $Demo
```

By default this creates `$Demo\.multi-agent-ai\`, including `messages\`, `tasks\`, and the
SQLite index. The coordination files are the source of truth; SQLite is a derived index.
`init` does not edit `.gitignore`, so exclude the state directory yourself:

```powershell
Add-Content .gitignore '.multi-agent-ai/'
git add .gitignore
git -c user.name="Demo User" -c user.email="demo@example.invalid" commit -m "Ignore local coordination state"
```

Do not put credentials or secrets in coordination files.

## 4. Add a small task plan

Create the task folder, capture the current commit and local time, then print those values:

```powershell
New-Item -ItemType Directory -Force .multi-agent-ai\tasks\demo | Out-Null
$BaseCommit = git rev-parse HEAD
$Updated = Get-Date -Format 'yyyy-MM-dd HH:mm'
Write-Host "Use base-commit: $BaseCommit and updated: $Updated in the plan below."
```

The following PowerShell here-string fills in the timestamp and base commit. It writes the
plan file in UTF-8; the parser accepts the BOM written by Windows PowerShell 5.1.

```powershell
$Plan = @"
task: demo
coordinator: me
revision: 1
updated: $Updated
base-commit: $BaseCommit
integration-branch: task/demo/integration

## Goal

Review the README.

## Acceptance criteria

- Report whether the README is present and readable.

## Verification

`git status --short`

| id | what | files/area | depends on | worktree | owner | reviewer | state |
| --- | --- | --- | --- | --- | --- | --- | --- |
| R1 | review README | README.md | - | no | codex-w1 | waived | assigned |
"@
$Plan | Set-Content .multi-agent-ai\tasks\demo\plan.md -Encoding utf8
```

`coordinator: me` and `owner: codex-w1` are example IDs for this practice plan; IDs are local
labels, not provider accounts. The plan records a base commit and an integration branch, but
this dry run creates neither worker branches nor worktrees.

## 5. Inspect the state

From any directory, run:

```powershell
& (Join-Path $InstallRoot '.venv\Scripts\multi-agent-ai.exe') inspect --project-root $Demo
```

The report should list the `demo` task and its `R1` subtask. `inspect` is read-only: it does
not notify agents, alter plans, or launch provider processes.

## 6. Preview a worker attempt (dry run)

From the target repository, preview a read-only review using an installed provider CLI:

```powershell
& (Join-Path $InstallRoot '.venv\Scripts\multi-agent-ai.exe') worker run `
    --project-root $Demo `
    --provider codex `
    --task demo `
    --subtask R1 `
    --agent-id codex-w1 `
    --mode review `
    --worktree (Resolve-Path .)
```

`worker run` is a dry run by default. It prints the resolved command and any preflight
problems; the expected output says `dry run: nothing was written or started`. If the selected
CLI is unavailable, the report identifies that problem. No provider process is launched and
no usage is consumed.

A real run requires `--execute` and can use provider quota. **Do not add it as part of this
quick start.** Real provider smoke tests are a later milestone and require explicit user
approval.

## Next steps

- Read the [messaging and joint-task protocol](protocol/agent-messaging.md) and its
  [file-format reference](protocol/formats.md) before coordinating real agents.
- Check [implementation status](implementation-status.md) for implemented features and
  limitations.
- For the larger architecture and roadmap, continue with the [README](../README.md).
