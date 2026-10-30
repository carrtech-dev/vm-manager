# Project workflow

## Worktrees

- Create every change in a dedicated Git worktree on a task branch named
  `codex/<task>`. This includes code, tests, documentation and configuration, and
  applies to every agent, including delegated agents.
- Never edit files directly in the main checkout. Preserve unrelated changes.
- After verifying the task is merged into local `main`, leave the task worktree
  and remove it. Never force-remove a worktree containing uncommitted or unmerged
  work. Keep work recoverable if a genuine blocker prevents completion.

## Validation and integration

- Run the project's relevant tests and required checks, and fix failures before
  merging. For application code, the standard checks are:

  ```sh
  .venv/bin/python -m pytest
  npm run check
  npm test
  ```

- Commit completed changes using conventional commit messages, then merge them
  into local `main`. Routine local merges are authorized; do not stop at an
  unmerged branch or ask for approval again.
- If `main` has changed, resolve integration conflicts and validate the
  integrated result before considering the merge complete.

## GitHub publication

- The project's public GitHub repository is
  `https://github.com/carrtech-dev/vm-manager`; use it as the `origin` remote.
- After every completed code change, push the tested, integrated `main` branch
  to GitHub as part of finishing the task. Push workflow and documentation
  updates too so GitHub stays current. Routine pushes to this repository are
  authorized; do not ask again or leave completed work only on the local machine.
- If the remote has advanced, fetch and integrate its changes, resolve conflicts
  and run relevant checks before pushing. Never force-push shared history.
- Verify that `origin/main` and local `main` point to the same commit after the
  push. If publication fails, preserve the local commits and report the blocker;
  do not claim the task is fully finished.
- Keep secrets, credentials, private keys, environment files, runtime databases,
  backups, VM disks, ISO images and local acceptance artifacts out of Git. Review
  staged changes before committing because this repository is public.

## Deployment

- Deployment is part of completing code changes after checks pass and changes
  are merged into local `main`. Routine deployment to the established environment
  is authorized. An explicit instruction not to deploy takes precedence;
  documentation-only changes need no runtime rollout.
- Follow the deployment and recovery procedure in `README.md`. From a clean,
  integrated main checkout, run `python3 scripts/deploy.py`. Preserve persistent
  data, take required backups and apply any required migrations.
- Verify service health and the changed behaviour in the established environment.
  Deploy every affected component. Do not run destructive acceptance scripts
  against production account data; follow each script's documented prerequisites
  and use disposable resources where required.
- If deployment or verification is blocked, retain recoverable work and report
  the blocker. Do not describe the job as fully finished.
