---
name: worktrees
description: Run two or more workers on code changes at the same time without collisions - one git worktree per task, based on the current working tree (untracked files included), then bring each result back as a patch without commits. Use when parallel tasks touch the same files or the same build.
---

# Parallel changes in worktrees

Two workers never edit the same files in the same folder. When parallel tasks touch
the same files, or one task's build or tests would trip over another's half-done
edits, give each task its own git worktree. Nobody commits, so results come back as
patches.

Tasks on clearly separate files can share the main folder; you do not need this.

## Create

Base each worktree on the current working tree, uncommitted and untracked files
included (ignored files are not), without changing the main folder, its index or the
stash:

```bash
cd <project>
wt="/tmp/shepherd-worktrees/<prefix>/T2"
mkdir -p "$(dirname "$wt")"
idx=$(mktemp)
GIT_INDEX_FILE=$idx git read-tree HEAD
GIT_INDEX_FILE=$idx git add -A
base=$(git commit-tree "$(GIT_INDEX_FILE=$idx git write-tree)" -p HEAD -m "shepherd base")
rm -f "$idx"
git worktree add --detach "$wt" "$base"
echo "$base" > "$wt.base"
```

- The base is a loose commit object that no branch points to; nothing is committed to
  the project's history.
- Worktrees live under `/tmp`, because a sandboxed Codex worker can write only in its
  project and `/tmp`. `<prefix>` is the last part of the shepherd cache folder in your prompt
  (`~/.cache/shepherd/<prefix>`): the workspace label made safe for paths.
- Dependencies (`node_modules`, virtualenvs) and ignored files (`.env`) are not in a
  new worktree. Tell the worker how to install them, or copy or symlink them when that
  is safe for the task.

In the brief: "Work only in `/tmp/shepherd-worktrees/<prefix>/T2`. Do not touch the
main folder."

## Bring a result back

After checking the worker's result:

```bash
wt="/tmp/shepherd-worktrees/<prefix>/T2"
git -C "$wt" add -A                          # stage only, nothing is committed
git -C "$wt" diff --cached "$(cat "$wt.base")" > "$wt.patch"
cd <project>
git apply --check "$wt.patch" && git apply "$wt.patch"
```

- Use plain `git apply`, not `--3way`: `--3way` refuses files whose working copy
  differs from the index, which is the normal state with uncommitted work.
- Apply patches one at a time, and run the tests after each.
- When `--check` fails, two tasks overlapped after all. `git apply --reject` applies
  what fits and leaves `.rej` files for the rest: resolve them yourself, or give them
  to the worker that knows the code better, in the main folder. Delete the `.rej`
  files afterwards.
- Competing drafts: apply only the chosen one. Read both first; the loser may have a
  test or an edge case worth keeping.

## Clean up

```bash
git worktree remove --force "$wt"
rm -f "$wt.base" "$wt.patch"
```

Only remove worktrees you created. `git worktree list` shows what is left.
