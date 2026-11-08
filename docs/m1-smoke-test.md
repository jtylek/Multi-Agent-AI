# M1 provider smoke test

Use a disposable repository. The generated task has independent Codex and Copilot implementation
subtasks, followed by a Claude test-writing subtask. The job driver independently runs each
subtask's verifier, requests a review from a different installed provider, integrates locally,
and runs the full suite. It stops at `awaiting_approval`; it does not push or merge into `main`.

## 1. Prepare

On Windows, install Python 3.11+, Git, and authenticate the Codex, Copilot and Claude CLIs.
From a checkout of this branch in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e '.[dev]'
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python tools\make_demo_job.py
```

The last command prints the disposable project path and exact dry-run and real-run commands.
It installs pytest into the disposable project's virtual environment. Review those commands
and the generated `plan.md` before a real run. Do not use `--no-venv` for the Windows trial.

## 2. Preview, then run

Run the printed `job start` dry-run command. It does not contact providers or change task state.
Confirm that all three providers are detected and that no `PROBLEM:` lines appear. If Copilot's
npm `.cmd` shim is selected, the command preview should show `kind: npm-shim` with a `node.exe`
prefix. A plain `.cmd` that cannot safely pass a quoted prompt must stop with `invalid_arguments`.

Open the Control Panel, select the disposable project, and click **Live Activity**. Then run
the printed real-job command in PowerShell. It explicitly includes `--execute` and
`--approved-provider-run`; this contacts the providers and may consume subscription quota.
The GUI shows requests and acknowledgments from the mailbox, status and review files, durable
job events, and provider-reported usage when available. A recorded request does not prove that
the recipient saw it. Missing usage is shown as `unknown`, without invented token estimates.

## 3. Inspect the result

```powershell
.\.venv\Scripts\multi-agent-ai job status --project-root <demo-project>
.\.venv\Scripts\multi-agent-ai job report --job <job-id> --project-root <demo-project>
```

Replace the angle-bracket placeholders with the printed path and job ID. Check the report,
each attempt's `result.json`, reviews and full-suite result. A successful run ends at
`awaiting_approval` with an integration branch in the disposable repository. A provider
failure retains the worktree and logs for diagnosis. Do not merge or push the demo branch.

For Linux, use `python3 -m venv .venv`, `.venv/bin/python`, and the same generator. POSIX
provider CLI resolution is covered with fake executable tests, but real Linux provider
execution remains unverified until an authenticated smoke run succeeds.
