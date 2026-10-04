---
name: nakama-orchestrate
description: Run a multi-agent coding or review session over herdr with the nakama CLI — split work into tasks, run pi/Claude builders in worktrees, gate, cross-model review, up to 2 fix rounds, then report or merge. Use when the user asks to orchestrate, delegate to a crew, or run builders/reviewers in parallel. Requires HERDR_ENV=1.
---

# nakama-orchestrate

You plan, dispatch and verify. You never write product code yourself. Load the `nakama` skill first: it has the commands and file formats.

## 0. Setup (once per session)
1. `nakama sweep`. Show anything it lists and ask before tearing any of it down.
2. Ask the user:
   - Integration: **review** (default — stop and leave the branches) or **merge into <branch>**?
   - May I push and open PRs without asking? (default **no**)
   - Roster OK? Show `nakama roles` and take per-session overrides (passed later as `--kind/--model/--budget`). `max_builders` (default 3)?
3. Note `git status --porcelain` of the main checkout.
4. Find the repo's own verification commands (tests, lint, build) in AGENTS.md / CLAUDE.md / README / CI config. These become every task's Acceptance. If you can't find them, ask.

## 1. Mode
- Review mode (a branch, diff or PR is given; no code to write): ≥2 reviewers on different models, each `--no-worktree` (or `--join` an existing builder), same scope, in parallel. Each task.md names the scope (e.g. `git diff main...HEAD`) and the intent. Then step 5's verification, one merged report, teardown, stop.
- Code mode: steps 2–6.

## 2. Split
- One task = one deliverable checkable in one pass. Tasks touch separate modules or files; tasks touching the same files are kept together or run in sequence.
- At most `max_builders` builders at once. Write a dependent task's task.md only after its dependency's result arrives.
- Show the split (slug, base, role/model, budget, Acceptance) and wait for the user's OK.

## 3. Build (per task)
`nakama launch builder …` → write task.md (Acceptance = the repo's verification commands scoped to the change) → `nakama prompt <name> task.md` → `nakama wait <name>` until it is no longer `running`.
- succeeded → step 4.
- needs_input → answer it if you know the answer, else ask the user; write fix-N.md and prompt with `--answer`.
- failed / malformed / partial with nothing usable → teardown; record the task as stopped.
Never do delegated work yourself while waiting. Wait for the whole batch before combining results.

## 4. Gate
Run the Acceptance commands yourself in the builder's worktree, writing the output to `<dir>/gate-N.log`. Check the main checkout's `git status --porcelain` still matches Setup.
- Green → step 5.
- Red → fix-N.md with the failing lines and a pointer to gate-N.log → `nakama prompt <name> fix-N.md`. `round_limit` → step 6, stopped.
- Resource symptoms in any build output (the build daemon died, out-of-memory errors, heavy swapping): stop and propose a lower `max_builders` to the user.

## 5. Review
`nakama launch reviewer <slug> --join <builder>` (add `--model` if the role default equals the builder's model). task.md scope = `git diff <base>...HEAD` plus intent and the user's decisions; a re-review lists the prior findings and their fixes.
- After each review, `git -C <worktree> status --porcelain` must show nothing outside .agent/; otherwise reject that review.
- Verify every finding against the code before accepting it. Drop speculation, style and duplicates. Tag sources (reviewer names, or both).
- Accepted findings → fix-N.md to the builder → back to step 4. None → step 6.

## 6. Finish
- `round_limit` or stopped → report what is left, `nakama teardown` (keep the worktree).
- Clean → `nakama teardown` the reviewer and the builder, then:
  - review integration: report branch, commits and gate evidence.
  - merge integration, one task at a time: `git -C <worktree> rebase <target>`, then from the main checkout `git checkout <target> && git merge --ff-only agent/<name>`. A conflict stops and asks the user. After the merge, `wt remove agent/<name> --yes`.
  - Push/PR only if Setup allowed it, and then following the repo's own PR rules. Otherwise print the ready-to-run commands.
- Final report: per task — status, rounds, budget extensions, branch, evidence, accepted findings, anything stopped.
