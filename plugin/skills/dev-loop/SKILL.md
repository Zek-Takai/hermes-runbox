---
name: dev-loop
description: Run, check, test and fix Python in runbox.
version: 1.0.0
author: hermes-runbox
license: MIT
metadata:
  hermes:
    tags: [python, testing, debugging, container]
    related_skills: []
---

# runbox dev loop

A starting point for working with the `runbox` tool. It is read-only; to adapt it, copy it to
`~/.hermes/skills/runbox/SKILL.md` and add your own rules (where finished files go, what needs
the user's word, house style).

## When to use

- The user wants Python code run, checked, tested or debugged.
- You are writing Python and want to prove it works before showing it.
- A project needs a quick run/test loop without touching the user's machine.

Discussing or drafting code without running it does not need runbox.

## The loop

1. **Get the code in.** `sync paths=[...]` with host files or folders (a folder's contents land
   at the workspace root, so imports resolve). For code you write, write the files to a folder
   first and sync by path; inline `files` with long multi-line code is fragile in tool-call JSON.
2. **One full pass before fixing anything.** Call `run`, `check` and `test` together. A crash in
   `run` hides every later bug; `check` (ruff error rules) finds undefined names and unused
   imports, and `test` shows behaviour bugs the crash masks. List the whole chain first.
3. **Fix in the box.** Edit the workspace copy (its host path is in the `sync`/`status` result),
   then repeat the full pass until `run` succeeds, `check` reports no issues and `test` passes.
4. **Report** what was wrong, what changed and the final result.

## How far to go

- "Run this" / "check this": report the defects and wait before fixing; the request scopes what
  you may change.
- "Fix it" / "make it work": fix in the box and loop until clean, then report.
- Your own code: loop until clean before presenting it.

## Getting results out

Fixes stay in the box until the user asks for them. When they do, use `export`: it writes the
changed files back to the folders they were synced from (or under `dest`), reports host files
that changed since the sync as conflicts, and leaves those alone unless `overwrite`. Do not copy
fixes into the user's files any other way.

## Reading results

- Issues come back as `file:line[:col]: CODE message`: ruff codes from `check`, `test-failed` /
  `test-error` from `test` (pointing at the failing line, with the assertion), and the exception
  name from `run` (runtime errors and compile-time SyntaxErrors).
- `test` with no paths runs `test_*.py` files; if there are none, it runs files that define test
  functions and says so in `notes`.
- `run` returns `stdout`, `stderr` and `exit_code`; output is clipped to the end.

## Packages

`install packages=[...]` asks the user for approval every time (it uses the network, in a
separate installer container). Install only what the code needs.

## Lifecycle

- The container starts on demand (about 10 s warm on macOS; the first start after the VM was
  removed downloads it and takes minutes).
- After 15 idle minutes the container is removed and, on macOS, the VM stops. Files and installed
  packages stay; the next action starts a fresh container on them.
- `stop` frees the container now (files kept). `reset` wipes the workspace and packages; use it
  only when asked.

## Troubleshooting

- "file not found" for a file `status` lists: compare with `run ls -la /workspace`. An empty
  container with files on the host means a stale mount: `stop`, then run again.
- The Desktop editor (beside the chat) shows the same box: files, Problems, Output and Runs. The
  user can run, check and test there too; their runs appear in the history marked "you".
