# Agent Relay

**Alpha.** A local, opt-in command line queue for a caller such as Groot. The default transport talks one turn at a time to a **new, bridge-owned** Claude Code session. An experimental MCP channel can reach an existing conversation only after that session is deliberately resumed with the channel enabled; it cannot attach to or inject into an arbitrary running terminal. A two-turn, no-tools Claude smoke test passed on Windows; live channel behavior remains unverified. No daemon, network listener, tunnel, telemetry, credentials, or autostart is installed by this project.

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

## Inspect existing Claude sessions

`gcb targets` runs Claude Code's read-only `agents --json` command without creating a bridge queue. It returns session UUID, kind, status, PID, and a hash of the working directory. `gcb targets --include-path` opts in to displaying the path. `gcb target-check --session UUID --repo PATH` verifies the exact active session and working directory. These commands **do not send** to discovered sessions. An active interactive session is marked `unsupported_active_interactive`; an unregistered background session is marked `background_not_enrolled`. The bridge's `send` command accepts only its own registered session IDs, so discovery cannot silently grant control of another session.

Claude's [CLI reference](https://code.claude.com/docs/en/cli-reference) documents message routing to running background sessions. Its [channels](https://code.claude.com/docs/en/channels) can inject events into an opted-in running session and provide a reply tool, including when the session stays open in a terminal. A session launched without the channel must be deliberately restarted with it at a safe checkpoint; discovery alone does not enable that path.

## Opt-in two-way channel (research preview)

The channel queue binds a registered session UUID, exact Git root, and Claude PID before it accepts messages. `channel-register --session UUID --repo REPO_ID` records that intended target without contacting it. An opted-in channel process binds itself and receives a private nonce; `channel-send --session UUID --source ACTOR --key EVENT_ID --text MESSAGE` then queues an idempotent message. `channel-read ID` and `channel-watch ID` show its state and reply. Registration and discovery do not install or start a channel.

The states distinguish `queued`, `dispatching`, `emitted`, `completed`, and `unknown`. `emitted` means only that the MCP notification was written to Claude's transport. [Claude does not acknowledge channel notifications](https://code.claude.com/docs/en/channels-reference), so it is **not** proof that the model read the message. `completed` requires a matching call to the channel reply tool. On restart, any message that might have been delivered becomes `unknown`; later messages stay blocked until `channel-resolve ID --reason 'inspection notes'`. The bridge never silently resends an ambiguous request. The channel has no remote permission relay capability, so messages cannot approve Claude's file or command prompts.

The adapter uses the official MCP SDK over stdio, plus a private local SQLite queue. It opens no HTTP port. The Node subprocess invokes Python in isolated import mode with the bridge source path explicitly pinned outside Claude's working directory. The queue's private ACL and the local OS account are its authentication boundary. A different process with the same user's filesystem access remains trusted; this is not an OS sandbox.

### Controlled setup for an existing conversation

Claude Code 2.1.248+ and Node 20+ are required. The channel is **not loaded into an already running session**. At a safe checkpoint, obtain the exact UUID with `python -m gcb.cli targets`, and confirm it with `target-check --session UUID --repo PATH`. Review the requested paths and the [Claude Channels preview warning](https://code.claude.com/docs/en/channels-reference#test-during-the-research-preview) before changing that session. The commands below are templates; do not execute them against another person's session or active worktree:

```powershell
# From the Agent Relay checkout. Install only the pinned official SDK, without package scripts.
python -m pip install -e .
npm --prefix channel ci --ignore-scripts --no-audit --no-fund

$homeDir = Join-Path $HOME '.gcb'
$repo = (python -m gcb.cli --home $homeDir repo-add 'C:\path\to\the\exact\Git\root' | ConvertFrom-Json).id
python -m gcb.cli --home $homeDir channel-register --session SESSION_UUID --repo $repo
python -m gcb.cli --home $homeDir channel-config --session SESSION_UUID > (Join-Path $homeDir 'agent-relay-mcp.json')
```

After Claude finishes its current turn, exit its terminal session normally. In the **same Git worktree**, resume the same UUID with the reviewed config and Claude's explicit development-channel opt-in:

```powershell
claude --resume SESSION_UUID --mcp-config (Join-Path $HOME '.gcb\agent-relay-mcp.json') --dangerously-load-development-channels server:agent-relay
```

Claude Code displays both the custom channel warning and MCP server consent prompt locally. Confirm only after reviewing them. If its process identity, channel availability, or private queue checks fail, the channel will not bind. `python -m gcb.cli channel-status` must show the intended UUID with `healthy: true` before a message is sent. The health check requires a recent poll and expires if the subprocess stops. From an authenticated local Groot task, send one request and poll the same ID:

```powershell
$request = (python -m gcb.cli channel-send --session SESSION_UUID --source authenticated-user --key stable-event-id --text 'Please review this change.' | ConvertFrom-Json).id
python -m gcb.cli channel-watch $request --timeout 120
```

The `source` string is attribution from the trusted caller, not authentication. A response is shown only after Claude calls the channel's `reply` tool for that exact request ID. If Claude requests permission to edit or run commands, approve it in Claude's own terminal; the channel never relays or grants permission. A channel restart after dispatch records `unknown` and requires manual inspection. This route preserves conversation context by resuming its UUID, but a channel-enabled restart and live conversation have **not yet been verified** for an existing terminal session. The laptop must remain awake, online, and running Claude for delivery.

## Opt-in Claude transport

`gcb session-add --repo REPO_ID --name trial --adapter claude` creates metadata and a fresh UUID; it does not call Claude. To process its request, `gcb run-once --live --budget-usd 0.05 --timeout 60` requires an explicit per-turn cost cap. Missing live flags or budget leave the request queued. Check your Claude account, selected provider/model, billing terms, settings, and the allowed repository before a real call. The CLI uses whichever account/provider your local Claude Code selects. `--max-budget-usd` is a Claude Code estimate for **one invocation**, not an account spending limit. No paid calls were made during development or tests.

The `claude` adapter starts Claude Code with `--safe-mode --strict-mcp-config --tools "" --permission-mode dontAsk --permission-prompts none`. It provides **conversation only**: Claude cannot read or edit the repository through tools. Safe mode suppresses custom hooks, plugins, and settings, while managed policy still applies. The bridge rejects a stream whose initialization event does not confirm an empty tool list.

## Reviewed patch mode (offline tested)

`claude-patch` is a limited coding workflow. Claude receives selected tracked UTF-8 files as text and proposes a unified diff. It has **no Claude tools**. The bridge validates that the diff changes only those files and applies it locally only after a person reviews the reply and supplies its exact digest. This avoids relying on a Claude hook to enforce file permissions: [Claude's hook reference](https://code.claude.com/docs/en/hooks) says hook startup errors and timeouts can be nonblocking. It does not provide an interactive Claude coding terminal, arbitrary commands, or autonomous test execution.

At a safe checkpoint, make a separate clean worktree on a named branch. Do not use a worktree occupied by an existing Claude process:

```powershell
git -C 'C:\path\to\project' worktree add -b bridge-work 'C:\path\to\bridge-work' HEAD
$repo = (gcb repo-add 'C:\path\to\bridge-work' | ConvertFrom-Json).id
$session = (gcb session-add --repo $repo --name patch-work --adapter claude-patch | ConvertFrom-Json).id
$request = (gcb send --session $session --source local-user --key unique-event-002 --file src/example.py --text 'Make the requested change.' | ConvertFrom-Json).id
gcb run-once --live --budget-usd 0.05 --timeout 300
gcb read $request
gcb patch-check $request
```

`gcb read` shows the full proposed patch. Inspect it and the target files. `patch-check` returns the SHA-256 digest and confirms that the patch applies to the pinned clean worktree. To approve that exact proposal, run `gcb patch-apply $request --digest REVIEWED_SHA256 --source local-user`. The source is an attribution label from a trusted local caller, not proof of identity. A denied proposal needs no action; it never reaches the filesystem. `patch-apply` stages the approved change in the separate worktree. Run tests or commit it through your normal reviewed local workflow. A changed branch, commit, dirty worktree, changed patch, unselected path, symlink, hard link, or protected `.env*` path blocks application.

All bridge Git commands ignore inherited `GIT_*` environment overrides, global and system Git config, executable local filter and diff driver settings, and configured filesystem monitors. Selected files with Git attributes are refused. Git patch whitespace behavior is pinned; before application the bridge applies the patch to disposable file copies, then checks that both staged and working bytes match those reviewed results. CRLF patches that Git cannot apply are refused. A crash after Git applies a patch but before recording success leaves a dirty worktree and blocks automatic retry; inspect it manually. These checks do not isolate the worktree from another local process that can modify it concurrently.

One patch request can include 1–12 explicitly selected tracked files, up to 12 KB total context. Their contents and the instruction are stored in the private queue; select files without secrets. This deliberately limits the size of changes Claude can propose. For a second change after applying a patch, review and commit the worktree, then register a new patch session at that checkpoint. The patch adapter has passed offline fixture tests only. No live coding call has been made, and model output quality remains unverified. It uses the same opt-in `--live --budget-usd` guard as the conversation adapter.

Claude output is untrusted data. Do not treat its reply or tool output as a command to Groot. Only a final JSON result with the expected session UUID and working directory is accepted. Known key/token shapes are redacted heuristically from stored replies and CLI output; **arbitrary secrets cannot be reliably detected**. Do not put secrets in prompts. The private queue database holds the original prompt until manually removed. `read` and `watch` omit the prompt unless `--include-prompt` is passed.

## Delivery and recovery

The queue is SQLite under `~/.gcb` by default, or `GCB_HOME` / `--home`. On Unix the directory must be mode `0700`; on Windows the CLI verifies its ACL and refuses broad read/write access. It does not modify an existing ACL. Protect backups of this directory too. Avoid shared, synchronized, or repository folders for queue state. The `.gcb/` directory is Git ignored for convenience, but ignoring a file is not an access control measure.

The queue admits at most 32 pending requests; prompts are limited to 16 KB and expire after 24 hours by default. A single local worker lock and SQLite transaction prevent ordinary duplicate claims. After a crash, timeout, protocol ambiguity, or output limit, a request is `unknown` and its session is paused. Inspect the local Claude session and repository before `gcb resolve REQUEST_ID --session-exists yes|no --reason 'inspection notes'`. Resolution does **not** replay the request. Choose `yes` only if Claude initialized that UUID; the next turn then resumes it. `no` clears the resume marker, so the next turn starts under that UUID. A deliberate retry needs a new key. Exactly-once Claude execution cannot be guaranteed across failures.

One `run-once` call processes at most one request. Stop by ceasing to call it; no service is installed. Stdout is capped at 1 MB during reading and stderr at 16 KB. On Windows, the child starts suspended, joins a kill-on-close Job Object, and only then runs. This terminated fake child processes in repeated local timeout and overflow tests. On Unix, the adapter uses a process group. An independently detached Unix process could survive. No remote network or credential setup is part of this project.

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

Offline tests have run on Windows with Python 3.11, including repeated fake-child termination and adversarial patch checks. CI tests and package builds passed on Windows and Ubuntu with Python 3.10–3.13 for the conversation-only release. A single bounded, two-turn conversation smoke test passed on Windows: both requests completed, the second recalled the first, and the disposable Git tree stayed clean. The raw Claude stream was not retained. Patch mode has not yet had a live model test.

Licensed under MIT. See [CONTRIBUTING.md](CONTRIBUTING.md).
