---
description: Best practices for running terminal commands to prevent stuck "Running.." states
---

# Command Execution Best Practices

These rules prevent commands from getting stuck in a "Running.." state due to the IDE
failing to detect command completion. Apply these on EVERY `run_command` call.

## Rule 1: Use High `WaitMsBeforeAsync` for Fast Commands

For commands expected to finish within a few seconds (git status, git log, git diff --stat,
ls, cat, echo, pip show, python --version, etc.), ALWAYS set `WaitMsBeforeAsync` to **5000**.

This gives the command enough time to complete synchronously so the IDE never sends it
to background monitoring (where completion detection can fail).

```
WaitMsBeforeAsync: 5000   # for fast commands (< 5s expected)
WaitMsBeforeAsync: 500    # ONLY for long-running commands (servers, builds, installs)
```

## Rule 2: Limit Output to Prevent Truncation Cascades

When output gets truncated, the IDE may auto-trigger follow-up commands (like `git status --short`)
that can get stuck. Prevent this by limiting output upfront:

- Use `--short`, `--stat`, `--oneline`, `-n N` flags on git commands
- Pipe through `head -n 50` for potentially long output
- Use `--no-pager` explicitly on git commands
- Prefer `git diff --stat` over `git diff` when full diff isn't needed

Examples:
```bash
# GOOD: limited output
git log -n 5 --oneline
git diff --stat
git diff -- path/to/file.py | head -n 80

# BAD: unbounded output that may truncate
git log
git diff
```

## Rule 3: Batch Related Quick Commands

Instead of running multiple fast commands sequentially (which can cause race conditions),
batch them into a single call with separators:

```bash
# GOOD: one call, no race conditions
git status --short && echo "---" && git log -n 3 --oneline && echo "---" && git diff --stat

# BAD: three separate rapid calls
# Call 1: git status --short
# Call 2: git log -n 3 --oneline
# Call 3: git diff --stat
```

## Rule 4: Always Follow Up Async Commands with `command_status`

If a command goes async (returns a background command ID), immediately call `command_status`
with `WaitDurationSeconds: 30` to block until completion rather than leaving it in limbo.

## Rule 5: Terminate Stuck Commands

If a command appears stuck in "Running.." but should have completed, use `send_command_input`
with `Terminate: true` to force-kill it, then re-run with a higher `WaitMsBeforeAsync`.
