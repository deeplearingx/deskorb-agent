# DeskOrb project instructions

- Use the dedicated Conda environment `deskorb-agent` for all project Python
  commands, tests, and desktop runs.
- Do not use the unrelated `marketmind` environment for this repository.
- Prefer `conda run -n deskorb-agent python ...` or activate the environment
  with `conda activate deskorb-agent`.
- Run the app through `Start DeskOrb Agent.cmd`; it delegates to the Conda
  launcher and keeps the local Playwright MCP setup checks.
- The standard test command is:
  `conda run -n deskorb-agent python -s -m pytest -q`
