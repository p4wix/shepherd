---
name: dispatch
description: How the shepherd lead sends a task to a worker over Herdr - checking state, writing a self-contained brief with a role and playbook, running workers in parallel, waiting, reading results, resetting context and handling blocked or stalled workers.
---

# Dispatch a task to a worker

Load the `herdr` skill first; this skill is the shepherd-specific way to use it.

## Check who is free

```bash
herdr agent list
```

Only `idle` and `done` workers take work. `working` is busy. `blocked` waits for a
permission prompt that only the user may answer: tell the user which worker and pane,
do not answer it and do not send it more work. `unknown`: read it before sending
anything (`herdr agent read <worker> --source recent-unwrapped --lines 40`).

## Write the brief

The worker does not see your conversation. The brief is all it knows.

```text
Task T2 from <lead>. Your role for this task: implementer.
Read <absolute path>/playbooks/implementer.md first.

Goal: <what and why, in 1-3 sentences>
Context: <what the user wants, decisions already made, relevant findings, names of
  functions, files and commands; paste short excerpts rather than "as discussed">
Scope: you may change <files or directories>. Do not touch <files others are editing>.
  Work in <worktree path> (only when using worktrees).
Done when: <observable result>
Verify with: <exact commands>
Report: as the playbook says. Also: <anything extra you need>.
```

Rules for briefs:
- One task per brief, with an id that matches the board.
- Absolute paths. Name the playbook with the path from the playbook table.
- State what is out of scope; workers otherwise drift into nearby fixes.
- With no playbook that fits, write the role inline: what to do, what not to do,
  what to report.

## Send it

Pass the brief through a quoted here-document, so quotes, backticks and `$` survive:

```bash
herdr agent prompt <worker> "$(cat <<'EOF'
<brief>
EOF
)" --wait --timeout 1800000
```

`--wait` returns when the worker settles (`idle`, `done` or `blocked`). A timeout or
`agent_prompt_stalled` does not prove the prompt was lost: check `herdr agent get` and
`agent read` before sending anything again, or the worker gets the task twice.

**Parallel work:** run each `herdr agent prompt ... --wait` as a separate background
command (Bash `run_in_background`), one per worker. You are notified as each one
finishes and can keep working meanwhile. Do not chain waits in one foreground command.

## Read the result

```bash
herdr agent read <worker> --source recent-unwrapped --lines 200
```

The report is the worker's last reply. If it is cut off, read more lines. If it still
does not fit, ask the worker to write its full report to a temporary file and reply
with the path only, then read that file. Codex may only be able to write inside the
project and `/tmp`, so pick a path there.

Then check the result (`shepherd:orchestrate`, step 6) and update the board.

## Follow-ups and fresh context

- A follow-up on the same task (a redo, fixing its own findings) goes to the same
  worker, which still has the context. Refer to its report, do not repeat everything.
- A new, unrelated task: reset the worker first, so old context does not leak in.
  The shepherd instructions survive the reset.

```bash
herdr agent prompt <claude-worker> "/clear"
herdr agent prompt <codex-worker> "/new"
herdr agent read <worker> --source visible --lines 30
```

Send these without `--wait`: a reset does not start a turn, so `--wait` would stall,
and `agent wait` returns at once because the worker is already idle. Before sending
the brief, read the pane and check that the old conversation is gone (Claude shows an
empty session, Codex a new session header). If it is still there, read again after a
moment; never send the brief on top of an unfinished reset.

## Stop or redirect a worker

- Wrong direction: `herdr agent send-keys <worker> esc` stops the current turn, then
  prompt it with the correction.
- Never kill a worker's pane or start new agents unless the user asks for it.
