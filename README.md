# agent-footprint

A live view of the environmental footprint of your AI coding agents: electricity, water, carbon and
mineral/metal depletion, across the full life cycle (running the servers plus a time-share of
manufacturing them). It reads the logs your agents already write, compares the numbers to everyday
things (phone charges, glasses of water, km driven) and shows day, week, month and year overviews.
The impact model is [EcoLogits](https://ecologits.ai).

## Install

Python 3.10+. Until it is on PyPI, install from a checkout of this repository:

```bash
uv tool install --editable ".[zstd]"
```

Once published:

```bash
uvx agent-footprint serve            # try it without installing
uv tool install agent-footprint      # or: pipx install agent-footprint
```

Optionally, open the view automatically whenever you start a coding agent (run from an installed
copy, not `uvx`):

```bash
agent-footprint setup                # asks per agent; --yes for all, --status to check, --remove to undo
```

## Usage

```bash
agent-footprint serve                # browser dashboard at http://localhost:8765
agent-footprint watch                # terminal view: today, all agents, live
agent-footprint watch --week         # also --month, --year; e.g. --month 2026-09
agent-footprint watch latest         # follow the newest session (or pass a session id)
agent-footprint watch --once         # print a snapshot and exit
agent-footprint open                 # open the terminal view in a split next to this one
agent-footprint info                 # versions, agents and log folders found, history size
agent-footprint config               # create the config file and print its path
agent-footprint update               # upgrade agent-footprint and EcoLogits
```

**watch keys:** ←/→ step through Year, Month, Week, Today and active sessions · ↓/↑ previous/next
period · `y m w d` jump to a period · `1–9` Today or an active session · `f` follow newest · `q` quit.

## Supported agents

✅ works · 🧪 implemented, not yet tried on a real install · ❌ not supported

| Agent | Reads usage | Opens view on session start (`setup`) |
|---|:-:|:-:|
| Claude Code (CLI, VS Code, JetBrains) | ✅ | ✅ |
| Codex CLI (and its IDE extensions) | 🧪 | 🧪 |
| Gemini CLI | 🧪 | 🧪 |
| OpenCode | 🧪 | 🧪 |
| Mistral Vibe | ✅ | ❌ |
| GitHub Copilot, VS Code chat¹ | ✅ | ❌ |
| Copilot CLI, Cursor, Windsurf, Cline / Roo Code, Continue, Aider | ❌ | ❌ |

¹ Copilot logs one prompt size per request, so its input tokens are estimated.

Past sessions are read too, and usage is kept in a local history after agents delete their logs.
Only token counts, timestamps, model names and session/project ids are stored; no prompts or
responses (session titles are opt-in).

## Supported systems

| System | Works | Notes |
|---|:-:|---|
| Linux | ✅ | |
| WSL | ✅ | Also reads agents installed on the Windows side; run `setup` in Windows for their hooks |
| Windows | ✅ | PowerShell, cmd, Windows Terminal, VS Code |
| macOS | 🧪 | No Mac tested yet; iTerm2/Terminal ask once for Automation permission |

`agent-footprint open` (and the `setup` hook) picks the right way to show the view:

| Terminal | Opens | Tried |
|---|---|:-:|
| Terminator | split pane | ✅ |
| tmux, zellij, kitty², WezTerm, iTerm2, Windows Terminal | split pane | 🧪 |
| GNOME Terminal, Konsole, macOS Terminal | new tab or window | 🧪 |
| IDE terminals and anything else | browser dashboard | ✅ |

² needs `allow_remote_control yes` in `kitty.conf`.

## How the numbers are computed

Each response's model and token counts go into EcoLogits'
[LLM inference methodology](https://ecologits.ai/latest/methodology/llm_inference/). Because coding
agents re-send large cached contexts, input tokens are also counted, at a small weight relative to
output tokens (configurable; set to 0 for pure EcoLogits figures). Treat the results as
order-of-magnitude estimates, not measurements.

More in [docs/details.md](docs/details.md): log locations, history, the methodology and its
assumptions, how figures are kept current, and configuration.

## Development

```bash
uv sync --extra zstd
uv run python -m pytest
```

To add an agent, subclass `FileSource` in `src/agent_footprint/sources/base.py` (see the existing
sources) and register it in `sources/__init__.py`.

## License

MIT; see `LICENSE`. EcoLogits is MPL-2.0 and is installed as a dependency, not bundled.
