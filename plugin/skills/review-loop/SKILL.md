---
name: review-loop
description: How the shepherd lead gets a change reviewed - picking reviewers, writing the review brief, triaging and verifying each finding, routing fixes and deciding when the change is done. Use when a change is ready for review or findings come back.
---

# Review loop

Every change bigger than trivial is reviewed before you call it done.

## Pick reviewers

- Never the author of the change. Prefer a different tool from the author's: Codex
  reviews Claude's work and the other way round.
- Two reviewers in parallel (one Claude, one Codex) for risky changes: auth,
  permissions, payments, data migrations, concurrency, public APIs, anything hard to
  undo. Add `security-review` to the brief when security is in play.
- Large change: split the review by area, one reviewer per area, plus one pass over
  the whole for how the parts fit.

## Brief

Role `reviewer`, with its playbook path. Also give:
- **Goal:** what the change is meant to do and for whom, so the reviewer can judge
  behaviour and not only code.
- **Scope:** how to see the change. Uncommitted work: `git diff` plus the new files
  from `git status --short`. A branch: `git diff main...HEAD`. List the files.
- **Focus:** the risky parts you know of, and what is out of scope on purpose.
- **Checks already done:** tests that pass, so the reviewer does not redo them.

## Triage findings

A finding is a hypothesis. Check each one yourself before acting on it:

| Verdict | Meaning | What you do |
|---|---|---|
| Confirmed | you can see or reproduce the defect | fix it |
| Plausible | cannot rule it out cheaply | fix it if cheap, else add a test that settles it |
| Rejected | wrong, or outside the goal | note why, in one line |
| Out of scope | real, but not this change | list it for the user |

With two reviewers, a finding both raise is almost always real. One that only one
raises gets the usual check.

## Fix

- Fixes go to the author of the code, who still has its context, with the list of
  confirmed findings and the expected result. Small fixes you do yourself.
- A fix that changes behaviour or scope beyond the goal is a decision for the user.

## Re-review and finish

- Send the fixes back to the same reviewer, scoped to what changed, with your triage
  (which findings were fixed and which were rejected, and why).
- Stop when the verdict is OK and the tests pass. After three rounds without
  agreement, stop and put the disagreement to the user with both sides.
- In your report to the user, name the reviewers and list the rejected findings with
  reasons, so the user can overrule you.
