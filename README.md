# Agent Relay

**Alpha.** A local, opt-in command line queue for a caller such as Groot to send one turn at a time to a **new, bridge-owned** Claude Code session. It cannot attach to an arbitrary running terminal session. The Claude adapter has not been tested against a live model; mock transport and offline protocol fixtures are tested. No daemon, network listener, tunnel, telemetry, credentials, or autostart is installed.

If an authorized Groot task can already run local commands on an online computer, it can invoke this CLI directly. That attended phone → Groot → local CLI path needs no additional HTTP service. Unattended event delivery while no local task is running would need a separately authorized connector and worker. An asleep or offline computer cannot execute local requests.

## Quick start (mock, no model call)

Requires Python 3.10+ and Git. Use an existing **disposable Git repository** as the fixture. Commands below are PowerShell examples, with placeholder paths only:

```powershell
python -m pip install -e .
$repo = (gcb repo-add 'C:\path\to\disposable-repo' | ConvertFrom-Json).id
$session = (gcb session-add --repo $repo --name trial --adapter mock | ConvertFrom-Json).id
$request = (gcb send --session $session --source local-user --key trial-001 --text 'Hello' | ConvertFrom-Json).id
gcb run-once
gcb read $request
```

`gcb watch $request --timeout 60` waits locally for completion. `gcb sessions` and `gcb repos` list bridge metadata only. Each command emits JSON. `queued` confirms durable storage; it is **not** acknowledgment by Claude. `running` is claimed, and `completed` has a verified final result. A repeated source/key/prompt returns the same request ID. A changed prompt with the same source/key is rejected. The source label is caller supplied and is not authentication; a trusted local task must derive it from its authenticated upstream identity.

## Opt-in Claude transport

`gcb session-add --repo REPO_ID --name trial --adapter claude` creates metadata and a fresh UUID; it does not call Claude. To process its request, `gcb run-once --live --budget-usd 0.05 --timeout 60` requires an explicit per-turn cost cap. Missing live flags or budget leave the request queued. Check your Claude account, selected provider/model, billing terms, settings, and the allowed repository before a real call. The CLI uses whichever account/provider your local Claude Code selects. `--max-budget-usd` is a Claude Code estimate for **one invocation**, not an account spending limit. No paid calls were made during development or tests.

The current adapter deliberately starts Claude Code with `--safe-mode --strict-mcp-config --tools "" --permission-mode dontAsk --permission-prompts none`. It provides **conversation only**: Claude cannot read or edit the repository through tools. Safe mode suppresses custom hooks, plugins, and settings, while managed policy still applies. The bridge rejects a stream whose initialization event does not confirm an empty tool list. It has no remote permission approval channel. A future coding adapter requires a reviewed permission design; do not remove these flags casually.

Claude output is untrusted data. Do not treat its reply or tool output as a command to Groot. Only a final JSON result with the expected session UUID and working directory is accepted. Known key/token shapes are redacted heuristically from stored replies and CLI output; **arbitrary secrets cannot be reliably detected**. Do not put secrets in prompts. The private queue database holds the original prompt until manually removed. `read` and `watch` omit the prompt unless `--include-prompt` is passed.

## Delivery and recovery

The queue is SQLite under `~/.gcb` by default, or `GCB_HOME` / `--home`. On Unix the directory must be mode `0700`; on Windows the CLI verifies its ACL and refuses broad read/write access. It does not modify an existing ACL. Protect backups of this directory too. Avoid shared, synchronized, or repository folders for queue state. The `.gcb/` directory is Git ignored for convenience, but ignoring a file is not an access control measure.

The queue admits at most 32 pending requests; prompts are limited to 16 KB and expire after 24 hours by default. A single local worker lock and SQLite transaction prevent ordinary duplicate claims. After a crash, timeout, protocol ambiguity, or output limit, a request is `unknown` and its session is paused. Inspect the local Claude session and repository before `gcb resolve REQUEST_ID --session-exists yes|no --reason 'inspection notes'`. Resolution does **not** replay the request. Choose `yes` only if Claude initialized that UUID; the next turn then resumes it. A deliberate retry needs a new key. Exactly-once Claude execution cannot be guaranteed across failures.

One `run-once` call processes at most one request. Stop by ceasing to call it; no service is installed. Stdout is capped at 1 MB during reading, stderr at 16 KB, and the process tree is terminated on timeout or overflow. The process-tree cleanup uses `taskkill /T` on Windows and a process group on Unix; externally detached children may survive. No remote network or credential setup is part of this project.

## Architecture and boundaries

```
authenticated local execution → gcb send → private SQLite queue → gcb run-once
                                                   ↑                    ↓
                                             gcb read/watch ← mock or Claude CLI
```

The caller must authenticate its upstream message and supply a stable event ID as `--key`; the CLI only trusts local OS access. Repositories are allowed by canonical Git root and Git directory, and checked again immediately before launch. A local actor able to modify the working tree or replace its path can still change what Claude sees; do not treat the repository check as a sandbox. Session IDs are created by this bridge only, and it never searches or resumes unrelated sessions. See [SECURITY.md](SECURITY.md) for the threat model.

The [Claude Code CLI reference](https://code.claude.com/docs/en/cli-reference) documents print mode, JSON events, explicit session IDs, resume, budget, and permission flags. [Programmatic Claude Code](https://code.claude.com/docs/en/headless) documents stream events and unattended permission handling. Claude's own [Remote Control](https://code.claude.com/docs/en/remote-control) is a separate option for using Claude's app; it does not connect this queue to another assistant.

## Development

```powershell
python -m unittest discover -s tests -v
python -m pip wheel --no-deps . -w dist
```

Offline tests have run on Windows with Python 3.11. CI runs tests and a package build on Windows and Ubuntu for supported Python versions. No live Claude protocol fixture has yet been captured; treat the live adapter as unverified until a separately approved no-tools smoke test passes.

Licensed under MIT. See [CONTRIBUTING.md](CONTRIBUTING.md).
