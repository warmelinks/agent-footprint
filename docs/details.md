# agent-footprint: details

Reference material behind the [README](../README.md).

## Opening the view automatically

Python packages can't run anything at install time, so this is a separate step. Run it from an
installed copy, not through `uvx`: the hooks call the program that set them up. `setup` adds a
session-start hook to Claude Code, Codex CLI and Gemini CLI, and a small plugin to OpenCode (not to
Vibe yet: its sessions are read, but nothing opens when one starts). Each one
runs `agent-footprint open`, which opens the terminal view next to the agent:

| Terminal | What opens |
|---|---|
| tmux, zellij, Terminator, kitty¹, WezTerm, iTerm2, Windows Terminal (native or WSL) | a split pane |
| GNOME Terminal, Konsole, macOS Terminal | a new tab or window |
| anything else (e.g. an IDE's terminal) | the browser dashboard, started once; it stops by itself 15 minutes after its last browser tab closes |

¹ kitty needs `allow_remote_control yes` in `kitty.conf`.

Inside an IDE (VS Code, JetBrains), including an agent extension's hooks, the browser dashboard
opens: the terminal the IDE was started from isn't the one you're working in. tmux started in the
IDE's terminal is split as usual.

**macOS:** the first time the view opens in iTerm2 or Terminal, macOS asks whether agent-footprint
may control that app. Run `agent-footprint open` there once by hand to answer it (or allow it under
System Settings › Privacy & Security › Automation); from a hook, the question may go unseen. kitty's
and WezTerm's command-line tools are also found inside their app bundles. Install with `uv tool
install` (or pipx on a Python ≥ 3.10): the Python that comes with macOS is 3.9.

Only one terminal view opens at a time, however many sessions you start. Set `"launch"` in the config
to force a terminal (`"terminal": "wezterm"`), to turn off the browser fallback, or to resize the split.
`setup` only changes its own entries in each agent's config file, and backs up the file
(`*.agent-footprint.bak`) the first time. `setup --status` (and `info`) reports a hook whose program
no longer exists, e.g. after its virtualenv was deleted; running `setup` again repairs it.

`setup` also asks whether to name sessions after their first prompt (off by default; see [What is stored](#history-is-kept-after-agents-purge-their-logs)).

## Where logs are read

| Agent | Where it reads | Notes |
|---|---|---|
| Claude Code | `~/.claude/projects/*/<session>.jsonl` and subagent logs | Responses deduplicated by request id |
| Codex CLI | `~/.codex/sessions/**/rollout-*.jsonl` and `archived_sessions/` | Compressed `.jsonl.zst` with the `zstd` extra or Python 3.14+ |
| Gemini CLI | `~/.gemini/tmp/*/chats/session-*.jsonl` (and legacy `.json`) | Subagent chats counted under their parent session |
| OpenCode | `~/.local/share/opencode/opencode.db` (and legacy `storage/`) | Read-only; child sessions counted under their parent |
| Mistral Vibe | `~/.vibe/logs/session/*/meta.json` (or `save_dir` in its `config.toml`) | Vibe logs running token totals per session (updated after each turn), not per response: each increase is recorded when seen, so a session read afterwards counts at its last activity |
| GitHub Copilot (VS Code chat) | VS Code's `workspaceStorage/*/chatSessions/` and `globalStorage/emptyWindowChatSessions/` (Code and Code - Insiders) | Output tokens and model calls are exact; Copilot logs one prompt size per request, so input tokens are estimated (see below). The model "Auto" picked is used |

No hooks or plugins are needed in the agents just for reading, and past sessions work too. The
Claude Code and Codex extensions for VS Code and JetBrains write the same logs, so they are counted.

**Logs in other places.** An agent can have several log folders, and all of them are read:

- **Environment variables:** default locations follow each agent's own variables: `CLAUDE_CONFIG_DIR`,
  `CODEX_HOME`, `GEMINI_CLI_HOME`, `XDG_DATA_HOME`, `OPENCODE_DB` and `VIBE_HOME`.
- **Hooks:** with `setup` hooks installed, every session start reports its transcript path, so a
  folder used through an alias or a per-project variable is picked up automatically.
- **WSL:** agents installed on the Windows side are found in each Windows user profile on `C:`
  (add other drives with `"path"`).
- **Config:** `"path"` under `"sources"` takes one folder or a list, for anything else
  (e.g. a synced folder from another machine).

Records are keyed by agent and request id, so a folder reached twice is never counted twice.
`agent-footprint info` lists every folder in use.

**Windows** works natively (PowerShell, cmd, Windows Terminal, VS Code's terminal) and under WSL.
Files go to the same places under your user folder: `~\.config\agent-footprint`,
`~\.local\share\agent-footprint` and `~\.cache\agent-footprint`. WSL reads the Windows-side agents'
logs too, but `setup` there only installs hooks in the WSL-side agents; run `setup` in Windows for the
others.

**Adding an agent:** subclass `FileSource` in `src/agent_footprint/sources/base.py`. Implement
`default_root()`, `units()` (its log files) and `parse_line()` (one JSON line to usage records with
model and token counts), then register it in `sources/__init__.py` and add a fixture test.

## History is kept after agents purge their logs

Claude Code deletes transcripts after `cleanupPeriodDays` (30 days by default), and Gemini CLI after
`general.sessionRetention.maxAge` (also 30 days by default). Each time agent-footprint runs, it copies
new usage records into `~/.local/share/agent-footprint/history.sqlite` (`$XDG_DATA_HOME`), and they
stay there after the source logs are gone. That is what makes month and year views possible.
(Version 0.1.0 kept it in `~/.cache`; it is moved automatically.)

**What is stored:** token counts, timestamps, model names, and each session's id and project folder
name. No prompt or response content, with one opt-in exception: with `"store_titles": true` (or
`agent-footprint setup --titles on`), sessions are named after the first 90 characters of their first
prompt, and those titles are stored. Turning it off erases stored titles.

- A stored response takes about 320 bytes including indexes: around 15 MB a year at 4,000 responses a month.
- `serve` and `watch` sync continuously. To capture history when neither runs, add
  `agent-footprint sync` to cron or to an agent's session-start hook at least every few weeks.
- Only files that changed since the last sync are read, and only from where reading stopped.

## How the numbers are computed

Each response's model and token counts go into the
[EcoLogits LLM inference methodology](https://ecologits.ai/latest/methodology/llm_inference/), using
EcoLogits' model size estimates, provider datacenter defaults (PUE, WUE, location) and
electricity mixes. The tests check the results against EcoLogits' own API.

- **Model size:** most providers don't publish parameter counts. EcoLogits estimates a range, which is
  the main source of the low–high spread.
- **Model names:** Bedrock, Vertex and OpenRouter ids, dated snapshots and `[1m]` suffixes are
  normalised. A model EcoLogits doesn't know yet is estimated as the closest known model of the same
  family. The dashboard lists every such substitution, and also says when a request had no model in
  the log, ran on Bedrock or Vertex (the model maker's datacenter defaults are used), or ran locally
  (e.g. Ollama through OpenCode; estimated as if served from a datacenter).
- **Input tokens:** EcoLogits counts only generated tokens. Agent sessions re-send large cached
  contexts on every request, so input tokens are added as weighted "output-token equivalents" (new
  input ×0.05, cache reads ×0.005). Set the weights to 0 in the config for pure EcoLogits figures.
- **Not counted:** training the models, the hardware's end-of-life, networking and your own device
  (all outside [EcoLogits' scope](https://ecologits.ai/latest/methodology/)), water used in making the
  hardware (not modelled), requests an agent doesn't log (for example, title generation), and sessions
  that ran on other machines (cloud or SSH sessions). "Life cycle" here means the life cycle of the
  hardware serving the requests (making it and running it), not of the model.

- **GitHub Copilot's input tokens** are estimated: it logs the prompt size of the last model call of each
  request only. The calls in a request are taken to grow linearly from the previous request's prompt
  to that one, and only the growth counts as new input; the rest as cache reads. The dashboard says so
  whenever Copilot is in view.

Treat the results as order-of-magnitude estimates, not measurements.

### Why input tokens are weighted

EcoLogits counts only generated (output) tokens. Coding agents re-send their whole context on every
request, mostly as cache reads: in a typical month of Claude Code use that is ~1,300M cache-read
tokens against ~6M output tokens. Reading context isn't free (the model still runs over it), but
per token it is much cheaper than generating:

- Prefill processes all input tokens in parallel and is compute-bound, while generation produces one
  token at a time and is memory-bandwidth-bound. So prefill energy per token is a small fraction of
  decode energy.
- A cache read skips recomputing the cached part and mostly costs memory traffic, so it is cheaper again.

The weights (new input ×0.05, cache writes ×0.05, cache reads ×0.005 output-token equivalents) are a
**judgement, not a measurement**. API prices put the ratios higher: input costs about 0.2× output,
and cache reads 0.1× input. Prices reflect cost and margin, not just energy, so they are an upper
reference. The choice matters: for the history above, EcoLogits' own method gives 8–14 kg CO₂e;
the default weights give 16–28 kg; price ratios would give 43–73 kg. The dashboard shows the
EcoLogits-only figure next to the assumption, and setting all weights to 0 in the config reproduces
EcoLogits exactly.

**One person's day.** Each indicator is also shown as a share of one person's daily footprint:

- **Electricity and greenhouse gases:** electricity generated per person and greenhouse-gas emissions
  per person (incl. land use) for the world, from Our World in Data (Ember; Jones et al.). Set
  `"compare_country"` (an ISO code such as `"NLD"` or `"DEU"`) to add your country's average.
- **Water:** full water footprints (Mekonnen & Hoekstra), including rainwater taken up by crops. An
  agent's water is cooling and power-plant water only, so its share is a lower bound. A national
  water figure is shown only where a sourced one is on file (currently the Netherlands).
- **Carbon budget:** a 1.5 °C-compatible budget of 2.5 t per person per year (~6.85 kg/day).

Past 100%, the share is shown in person-days. Sources are listed in the dashboard.

## Keeping the figures up to date

| Figures | Kept current by |
|---|---|
| Models, datacenters, electricity grids (EcoLogits) | Dependabot proposes each EcoLogits release; CI tests it and it is merged when green; a weekly job tests the newest release |
| Electricity and greenhouse gases per person (world and countries) | `scripts/update_factors.py` refetches Our World in Data monthly and opens a pull request for review (changes over 25% are flagged) |
| Everyday comparisons, water footprints, the 1.5 °C budget | each figure has an `as_of` year and a review interval; a yearly job opens an issue listing those due (`scripts/stale_factors.py`) |

Users get new figures through releases. `serve` and `watch` check PyPI at most once a day and show a
notice when newer EcoLogits data or a newer agent-footprint is available (turn off with
`"auto_update_check": false`). `agent-footprint update` upgrades both in the tool's own environment
(`uv tool`, `pipx` or `pip`); for a source checkout, `git pull` brings new figures.

## Configuration

```bash
agent-footprint config               # creates ~/.config/agent-footprint/config.json and prints its path
agent-footprint info                 # versions, agents found, history size, update status
```

The config holds the grid zone (ISO-3 code, e.g. `"FRA"`), datacenter PUE/WUE, batch size, hardware
lifetime, input-token weights, per-provider overrides, which agents to read (and where), the
country to compare with, session titles, and what `agent-footprint open` does. Both views pick up
changes live. A setting that can't be used (a typo, an unknown country code, a value of the wrong
type, or a file that isn't valid JSON) is reported by `info`, the dashboard and `watch`, and the
default is used instead.

The dashboard only answers requests addressed to `localhost`, so other web pages can't read it.
`serve --host 0.0.0.0` makes it reachable from other machines, without authentication.
