<!-- murmur:contract -->
# The contract for agent work in this repository

Repository `magik-ai/strikt`. Work starts from `main` and merges back into `main`.

## What never happens without the owner

These actions need the owner's explicit word, every time: merge, force-push, production-writes, paid-provisioning.
A green pipeline is permission to merge, never the decision to merge.

## Identity and claims

Your name comes from the owner, per session, never from memory.
Claims live in a private coordination repository. Claim a branch there before you create or push it, and release it when the PR merges.
Never touch a branch another agent has claimed: no push, no rebase, no merge.

## Where the work is written down

Work is tracked in github-issues. Taking a task, linking the pull request and posting evidence: `.claude/tracker.md`.
Heavy and parallel runs belong on the separate agent machine.

## Generated files

Never edit a file that `.claude/generated-files.txt` lists by hand: regenerate it with the command written beside its entry.

## 0. Core contract

- One batch, one worktree, one branch, commits per slice, one pull request,
  green CI, a squash merge through the merge queue, then remove the worktree.
  A batch is one coherent change. A worktree is a separate working copy of the
  repository, made with `git worktree add`.
- Nothing lands directly on `main`.
- The lane (the agent doing the task) drives the batch until its pull request
  is ready to merge. Stop before that only for a real blocker, for red CI that
  the batch did not cause, or for an action that needs explicit approval.
- The owner decides what merges. After the owner's yes, the conductor (or the
  orchestrator, if there is no conductor) merges it. A lane never merges.
- The `hold` label is the veto: it always blocks a merge. Anyone may add the
  `hold` label. Only the owner removes it.

## 2. Golden Workflow: 10 gates

1. If the team records branch claims, claim the branch first. Branch from a
   fresh `main` in a new worktree. Never reuse a merged branch.
2. Build one coherent batch, and commit each reviewable slice.
3. Keep the docs current in the same pull request, for whichever of these you
   keep. A change to a key API or data contract, or a major architecture
   decision, updates `ARCHITECTURE.md` and adds an ADR. Adding, moving or
   retiring a document updates `docs/INDEX.md`. **Before merge, check
   `docs/knowledge/`**: if the batch changes how a subsystem really behaves
   (mechanism, capacity, cost, timing, procedure), update the matching domain
   file in this pull request.
4. If users will notice the change, add a note in
   `RELEASE_NOTES.d/<category>-<slug>.md`. Otherwise, write "no user-visible
   change" in the pull request.
5. Run the affected lint, tests, build and contract checks locally.
6. When `main` moves while you work, merge `origin/main` into your branch and
   rerun the affected checks.
7. Open the pull request with `.github/PULL_REQUEST_TEMPLATE.md`, the only pull
   request template.
8. Run an adversarial review of the exact head commit: a second agent whose job
   is to find what is wrong, ideally a different model or vendor. It ends with
   one line, `VERDICT <sha> CLEAN` or `VERDICT <sha> RED`. Use the deeper
   review for auth, security, migrations, durability, or encrypted user
   content.
9. Fix every in-scope finding in the same pull request. File a scoped issue for
   real out-of-scope work.
10. Green checks make a change eligible to merge. Only the owner's yes decides
    it. After that yes, the conductor (or the orchestrator, if there is no
    conductor) turns on auto-merge. A lane never does. Never bypass a required
    check with an admin override. Before cleanup, confirm that GitHub reports
    the pull request as merged.

## 3. Commits

Write commit messages as `Type: subject`, in the imperative, with a capitalized
type and no full stop at the end. Types: `Feat:`, `Fix:`, `Review:`, `Docs:`,
`Build:`, `Cleanup:`. Right before every commit or push, check the current
branch and `git status --short`. This two-second check stops pushes to the
wrong branch.

## 7. Don'ts

Do not commit to `main`, reuse merged branches, merge red CI, force-push shared
work, skip the commit hooks, bundle an unrelated refactor into a fix, bump a
version, or leave a pull request you own unfinished. Do not amend after a
failed commit hook: that commit was never made, so the amend would change the
previous commit.

## The rest

The long form of this method, with the reasoning behind each rule, is the murmur handbook: https://github.com/magik-ai/murmur/tree/main/docs. This file is the part that binds.
