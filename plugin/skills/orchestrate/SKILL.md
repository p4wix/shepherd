---
name: orchestrate
description: The shepherd lead's full loop for a request - understand, size, plan a task graph, assign workers and playbooks, run in parallel, check, integrate, review and report. Use for every request bigger than a quick answer or a one-file fix.
---

# Orchestrate a request

You are the lead. This is the loop from the user's request to a finished, reviewed
result. The mechanics of talking to workers are in `shepherd:dispatch`; parallel code
changes in `shepherd:worktrees`; review in `shepherd:review-loop`.

## 1. Understand

- Restate the goal in one or two sentences and name the acceptance check: a test, a
  command, a behaviour the user can see.
- Read the code that the request touches before asking anything. Many questions
  answer themselves.
- Ask the user only what the code and a sensible default cannot decide: scope,
  trade-offs with user-visible effects, anything costly to undo. Batch the questions
  and recommend an answer for each.
- Big or fuzzy request: send a `planner` or `researcher` first (in parallel, if there
  are several unknowns) and plan on what they bring back.

## 2. Size

| Size | Signs | What you do |
|---|---|---|
| Trivial | answer, one file, under ~10 minutes | do it yourself, no board |
| Small | one area, one worker's worth | one implementer, one reviewer |
| Medium | several areas, parts independent | plan, 2-3 workers in parallel, review |
| Large | architecture, many modules, unknowns | planner first, then waves of work, review per wave |

Delegation costs a brief and a check. Delegate when the piece is well bounded and the
brief is shorter than the work.

## 3. Plan a task graph

Write each task as: id, goal, files it may change, depends on, playbook, verify with.

- Cut along file and module boundaries, so parallel tasks do not touch the same files.
  When they must, use worktrees or run them one after another.
- Put the core first (interfaces, data model, shared types); you often write it
  yourself. Leaves (UI, tests, docs) come after and run in parallel.
- Every implementation task names how it is verified. Tests may be a separate task
  for a `tester`, working on test files while an `implementer` works on the code.
- Keep tasks at 5-30 minutes of worker time. Bigger ones get lost; smaller ones cost
  more in briefs than they save.

## 4. Assign

Match the playbook to the tool (the playbook table in your prompt says what suits):

| Work | First choice | Why |
|---|---|---|
| implementation, tests, running the app, docs | Claude worker | strong at multi-file edits and tool use |
| review, second opinion, independent check | Codex worker | a different model catches different bugs |
| research, debugging, planning | either | split the question when two are free |

Useful patterns:
- **Fan-out research:** one question per worker, in parallel, then you merge.
- **Build and test:** `implementer` on the code and `tester` on the tests in parallel,
  both from the same agreed interface.
- **Build then review:** the author never reviews its own work; a Codex worker does.
- **Two opinions:** for risky or security-sensitive changes, two reviewers in
  parallel (one Claude, one Codex) and compare.
- **Competing drafts:** when the design is unclear, two implementers in separate
  worktrees, then pick one or merge the best of both.

Give a worker a fresh context (`/clear` or `/new`, see `shepherd:dispatch`) when its
next task is unrelated to the last. Keep the context when the task builds on its
previous work, for example fixing its own review findings.

## 5. Run and track

Keep the board in the file your prompt names (`board.md` in the shepherd cache):

```markdown
# Board: <request in a few words>
Goal: <one line>   Done when: <acceptance check>

| Task | Worker | Role | Depends on | State | Result |
|---|---|---|---|---|---|
| T1 core types | lead | - | - | done | src/types.ts |
| T2 API route | wf-worker-1 | implementer | T1 | running | |
| T3 route tests | wf-worker-2 | tester | T1 | running | |
| T4 review T2+T3 | wf-worker-3 | reviewer | T2, T3 | waiting | |

Decisions: <what was decided, by whom>
```

States: waiting, running, blocked (on a permission prompt, tell the user), done,
failed, redo. Update the board on every dispatch and result. After a context
compaction, read the board before doing anything else.

While workers run: check other results, prepare the next briefs, read the code for the
next wave. Tell the user in one line what is running when it takes long.

## 6. Check every result

- Read the report, then the evidence: `git diff` for the files it names, rerun the
  key test or command, open the files that matter.
- Accept, or send it back with the exact gap ("the empty-list case in `parse()` still
  throws, test X fails with ..."). The same worker keeps its context for a redo.
- Two failed attempts at one task: change the brief, split the task, switch the
  worker, or do it yourself.
- Worker questions in a report: answer them, or ask the user when only the user can.

## 7. Integrate and review

- Put the pieces together: worktree patches applied, interfaces match, no dead ends.
- Run the full relevant suite and the acceptance check yourself.
- Run `shepherd:review-loop` on the whole change.

## 8. Report to the user

- **Done:** what now works, in the user's terms.
- **Verified:** commands and results.
- **Decided for you:** choices made without asking, one line each.
- **Open:** what is left, risks, suggestions outside the request.

Clean up: worktrees removed, the board marked done. Nobody commits unless the user
asked for it.
