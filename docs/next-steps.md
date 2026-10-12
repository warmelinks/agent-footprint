# Next steps

State as of 2026-10-11: everything is committed on `master` and pushed to `github.com/warmelinks/agent-footprint`.
Tests pass: 77 passed, 2 skipped on Linux; 76 passed, 3 skipped under Windows Python 3.13
(`uv run python -m pytest`, which installs the `dev` group). Skips are platform-specific tests.

The review of 2026-10-11 (privacy, history location, hooks, ports, config, dashboard access,
syncing, Windows) is done except for the items under "Open from the review" below.

## Before publishing

- [ ] **Repository and package name.** `pyproject.toml`, the README and the dashboard link to
      `github.com/warmelinks/agent-footprint`. Check that `agent-footprint` is free on
      PyPI, create the GitHub repo, and update the links if the name differs.
- [ ] **Commit author email.** The commits use `steven@soundappraisal.eu` from the local git config;
      decide which address to use publicly before pushing.
- [ ] **GitHub settings** the workflows rely on:
  - "Allow auto-merge", plus a branch protection rule on `master` that requires the CI checks (`automerge.yml`).
  - A PyPI trusted publisher for `release.yml` (environment `pypi`).
  - Labels `ecologits` and `data`, used by the scheduled jobs.
- [ ] **First release:** tag `v0.1.0`; `release.yml` builds and publishes.

## Verification still missing

- [ ] **Codex CLI, Gemini CLI and OpenCode readers** were built from those tools' source code (checked
      2026-10-10) and tested on fixture logs, but never on real installs. Try each with real sessions.
- [ ] **Session-start hooks** for Codex, Gemini and OpenCode: format checked against source, not run
      in the real agents. Codex may ask to trust the hook once.
- [ ] **Terminal launchers:** only Terminator (WSL) was seen live; the tmux, zellij, kitty, WezTerm,
      iTerm2, Windows Terminal, GNOME Terminal, Konsole and macOS Terminal commands are untested.
- [x] **Local setup:** `setup` has been run for Claude Code on both WSL and Windows (the old
      `open_split.sh` hook is gone); session titles are switched on in both configs.

## Windows / VS Code checks (run Claude Code natively in Windows for these)

Native Windows support was built and tested from WSL on 2026-10-11: the test suite passes under
Windows Python 3.13, and `info`, `watch --once`, `serve` (Host check, busy port) and `open`'s browser
fallback were run natively. What only a native session can check:

- [x] **Install:** `uv tool install --editable` of the WSL checkout (`\\wsl.localhost\Ubuntu\...`) works;
      `agent-footprint` is on PATH. Not yet tried: a checkout on a Windows drive.
- [x] **Hook from the VS Code extension:** Claude Code's SessionStart hook runs the bare
      `agent-footprint open --hook --agent claude` and opens the browser dashboard (`hook.log`).
      An empty console window that came with it is fixed (f110e58).
- [ ] **Hook from the CLI** in Windows Terminal, and which shell runs hooks there (Git Bash, cmd,
      PowerShell). A double-quoted absolute path (used when `agent-footprint` isn't on PATH) would not run
      in PowerShell.
- [x] **Hook input:** the Windows log folder was learned from the hook (`roots.json`).
- [ ] **Windows Terminal split:** from a Claude Code session in Windows Terminal, `agent-footprint open`
      should split the pane (`wt.exe -w 0 split-pane`) and run `watch` there.
- [ ] **watch keys** in Windows Terminal, conhost (cmd.exe window) and VS Code's terminal: arrows, y m w d,
      1–9, q; colours and box characters render; resizing redraws.
- [ ] **VS Code fallback:** the dashboard starts once and the browser opens (seen). Still to see: it
      stops 15 minutes after the last tab closes (`serve.log`), and no window appears since f110e58.
- [ ] **Locked files:** `setup` while Claude Code is running (settings.json open); syncing while sessions
      are being written; two Claude Code windows starting at once (`roots.json`, pid file).
- [ ] **Codex, Gemini CLI, OpenCode on Windows**, if installed: their log locations and hooks.
- [x] **Mistral Vibe and GitHub Copilot (VS Code chat)** update live in `watch` and the dashboard
      (checked 2026-10-11). Neither has a session-start hook in `setup` yet; the Copilot CLI is unsupported.
- [ ] **CI:** the `windows-latest` jobs pass once the repository is on GitHub.

## macOS checks (no Mac available yet)

Reviewed by reading the code on 2026-10-11; the `macos-latest` CI jobs will run the unit tests.
Only a Mac can check:

- [ ] **iTerm2:** `agent-footprint open` splits the agent's own session (targeted through
      `ITERM_SESSION_ID`), also with several iTerm windows open; the Automation prompt appears once.
- [ ] **Terminal.app:** a new window opens with `watch`; the same Automation prompt.
- [ ] **kitty / WezTerm** without their CLIs on PATH: found in `/Applications/*.app/Contents/MacOS/`.
- [ ] **Claude Code from VS Code** started from iTerm (`code .`): the hook opens the browser, not an iTerm split.
- [ ] **Copilot:** chats found under `~/Library/Application Support/Code/User`.
- [ ] **watch keys** in iTerm2 and Terminal.app.

## Figures reviewed

Reviewed 2026-10-11; `python scripts/stale_factors.py` reports nothing due until 2027.
- EU household electricity: 3,600 → 3,525 kWh/year (Eurostat 2024), so a day is now 9.7 kWh.
- Petrol car: 170 → 162 g/km (DESNZ 2026).
- Transatlantic flight: still ~0.8 t (DESNZ 2026 with radiative forcing and well-to-tank). The source text now shows the calculation.
- Water footprints and the 1.5 °C budget: no newer edition exists, so only `as_of` was bumped.

## Open from the review

- [ ] **`agent-footprint update` on an "externally managed" Python** (Debian/Ubuntu system Python,
      Homebrew; PEP 668): the `pip install` path fails. Detect it and point to `uv tool` or `pipx`.
- [ ] **One watch view at a time, machine-wide:** a second terminal window or tmux session never gets a
      pane. Document it, or make it per terminal window.
- [ ] **Headline figure:** label the dashboard's main numbers as "EcoLogits + input-token extension"
      (both figures are already shown under "What's behind these numbers?").
- [ ] **Titles erased while switched off** don't come back when they are switched on again (parser state
      marks them "found, not kept"). Option: when titles are on, re-read the title from logs that still
      exist. The WSL history has titles for 15 of 152 sessions because of this.
- [ ] **Code quality:** type hints; a table-driven rewrite of `model.resolve_model` with more cases;
      split `watch.py` (rendering vs input); `logging` instead of `print` in `serve`; a ruff config.

## More agents

- [ ] **Session-start hooks for Vibe and Copilot**, so the view opens with them too (their sessions are
      read already). Vibe has some kind of hooks (its stats count hook-denied tool calls): check its docs.
- [ ] **Copilot CLI:** not supported; no session data on the maintainer's machine. Look at what it
      writes after a real session (`~/.copilot/`).
- [ ] **Cursor, Windsurf, Cline / Roo Code, Continue, Aider:** need real sessions to build and test against.

## Known limitations / ideas

- **The input-token weights** (×0.05 new input and cache writes, ×0.005 cache reads) are a judgement and
  roughly double totals versus EcoLogits alone (see README, "Why input tokens are weighted").
  Measured prefill/cache energy data would let this be grounded better.
- **Remote and cloud sessions** (claude.ai/code, SSH hosts) aren't counted. Options: an rsync mirror as
  an extra source, or an OTLP receiver in `serve` (agents can export usage via OpenTelemetry).
- **Under WSL**, Windows-side agents are read, but `setup` only installs hooks in the WSL-side agent
  configs; Windows-side hooks need `setup` run in Windows.
- **National water footprints** exist only for the Netherlands; other countries show world only.
- **Mistral Vibe** logs running totals per session only: timing is per turn while a view runs, but a
  session first read afterwards counts in full at its last activity.
- **GitHub Copilot's input tokens are estimated** (one prompt size per request is logged); output tokens
  and model calls are exact. Its "Auto" model may be one EcoLogits doesn't know (e.g. Microsoft's
  `mai-*`), estimated as the closest model of that maker and size.
- **Inside an IDE** the browser opens rather than a split; zellij run in an IDE's terminal therefore
  gets the browser too (its variables look the same as inherited ones).

## Where things are

| Area | Files |
|---|---|
| Impact model (EcoLogits wrapper, model-name resolution, baselines) | `src/agent_footprint/model.py` |
| Agent log readers and folder discovery | `src/agent_footprint/sources/` (claude, codex, gemini, opencode, vibe, copilot) |
| History store (kept after agents purge logs) | `src/agent_footprint/store.py`, `~/.local/share/agent-footprint/history.sqlite` |
| Periods and aggregation | `periods.py`, `summary.py`, `views.py` |
| Dashboard and terminal | `server.py` + `web/index.html`, `watch.py` |
| Hooks and launching | `hooks.py` (`setup`), `launch.py` (`open`) |
| Figure upkeep | `scripts/update_factors.py`, `scripts/stale_factors.py`, `.github/workflows/` |
