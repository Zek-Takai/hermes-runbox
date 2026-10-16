# hermes-runbox

A Hermes plugin that lets the agent build, run and test Python in an **offline container per
chat**, and get the results back as a plain issue list:

```
broken.py:2:7: F821 Undefined name `undefined_name`
test_calc.py:7: test-failed test_calc.py::test_bad: assert 4 == 5
main.py:2: ZeroDivisionError division by zero
```

Status: core runner and tool (phase 2). Desktop "Runs" tab and full docs to follow.

## Tool

| Action | What it does |
|---|---|
| `sync` | Copy host files/folders (`paths`) or inline `files` into the chat's workspace; `clean` empties it first |
| `run` | Run a shell `command` in `/workspace` (`timeout`, default 120 s, max 600 s) |
| `check` | `ruff check` with error rules only (`E4,E7,E9,F`: syntax, undefined names, unused imports) |
| `test` | `pytest`; failures and errors come back as `file:line` issues |
| `install` | `pip install` PyPI `packages` (asks for approval: it uses the network) |
| `status` | Backend, container state, workspace files, installed packages, recent runs |
| `stop` | Remove the container (workspace and packages kept) |
| `reset` | Remove the container, workspace and packages |

## Isolation

- Work container: `--network none`, `--cap-drop ALL` (plus `DAC_OVERRIDE`, `CHOWN`, `FOWNER`),
  `no-new-privileges`, `--pids-limit 256`, memory/CPU limits, tmpfs `/tmp` and `/run`, `--init`.
  It sees only its workspace (read-write), its packages and the checker toolchain (read-only).
- `install` runs pip in a separate short-lived container that has network access and mounts only
  the chat's package folder. A `pre_tool_call` hook sends every `install` through Hermes'
  approval prompt.
- runbox uses its own Docker client config (`plugin-data/runbox/docker-config`), so the user's
  `~/.docker` settings and active context are never read or changed. On macOS it runs a
  dedicated colima profile `runbox` (2 CPU, 2 GB, started with `--activate=false`).

## Lifecycle

- The VM (macOS) and container start on the first call that needs them.
- A chat's container is removed after `idle_minutes` (default 15) without runbox use; on macOS
  the VM stops once no container is left.
- A chat's workspace is removed when the chat is deleted, or after `idle_days` (default 90).

## Requirements

- Hermes `>= 0.22.0`.
- macOS: `brew install colima docker`. Linux: a running Docker daemon.
- The Hermes data folder must be under your home folder (colima shares only that).

## Development

```
plugin/              the installable package
  plugin.yaml        manifest
  __init__.py        registration, idle stop and cleanup thread
  runbox/            engine (docker/colima), store, issues, tool, lifecycle, guidance
tests/               pytest suite
```

```
ln -s /path/to/hermes-runbox/plugin ~/.hermes/plugins/runbox
hermes plugins enable runbox
python -m pytest tests -q      # with the Hermes test interpreter
```

## License

MIT
