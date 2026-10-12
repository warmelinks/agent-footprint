# Contributing

Issues and pull requests are welcome: bug reports, support for another agent, or corrections to a
figure or its source.

## Running the tests

```bash
uv sync --extra zstd
uv run python -m pytest
uv tool install --editable ".[zstd]"   # optional: use your checkout as the installed agent-footprint
```

CI runs the tests on Linux, Windows and macOS with Python 3.10 and 3.14.

## Adding an agent

1. Subclass `FileSource` (`src/agent_footprint/sources/base.py`) and implement `default_root()`,
   `units()` and `parse_line()`. The existing readers in `src/agent_footprint/sources/` are short
   examples; `sources/__init__.py` explains how log folders are found.
2. Register it in `SOURCES` and `TOOL_LABEL` in `sources/__init__.py`.
3. Add tests in `tests/test_sources.py`, with sample log lines inline or as a small fixture under
   `tests/fixtures/<agent>/`. Samples must not contain real prompts, code or paths: replace them with
   placeholder text.

Readers should be built from a real session's logs or the agent's source code; say which in the
pull request.

## Figures

The everyday comparisons, water footprints and the 1.5 °C budget in `src/agent_footprint/factors.json`
each name their source. To correct one, update the value, its `source` and `as_of`, and link the
source in the pull request. The per-person electricity and greenhouse-gas baselines
(`src/agent_footprint/data/countries.json`) are generated from Our World in Data by
`scripts/update_factors.py`; don't edit them by hand.
