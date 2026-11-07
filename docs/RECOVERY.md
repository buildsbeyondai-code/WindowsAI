# Release recovery

What to do when a release, the Store pipeline or the repository goes wrong.
App incidents on a running PC are in [OPERATIONS.md](OPERATIONS.md#incident-response).

The release path, for reference:

1. A `v*` tag is pushed.
2. `MSIX` builds x64 and Arm64 packages and runs the frozen smoke test.
3. Its `release` job waits for approval in the **`release` environment**, then
   publishes a GitHub Release with `SHA256SUMS.txt`.
4. `Store submit` (on release, and hourly) submits the newest **non-draft,
   non-pre-release** GitHub Release to Partner Center if the Store is behind.
   It checks the packages against `SHA256SUMS.txt` first.

Nothing reaches the Store without a published GitHub Release, and no release
is published without passing step 3.

## One-time setup (owner)

- **Settings → Environments → New environment `release` → Required reviewers:
  yourself.** Until this is set, the environment exists but approves itself,
  and a pushed tag publishes as before.
- Optionally tick *Prevent self-review* off (a solo maintainer must be able to
  approve their own release) and limit *Deployment branches and tags* to `v*`.
- **Settings → Code security → Secret scanning and Push protection → Enable.**

## Stop everything first

```bash
gh variable set STORE_AUTO_SUBMIT --body false --repo buildsbeyondai-code/WindowsAI
```

Store submission is now paused; every hourly run skips. Turn it back on with
`--body true` once the problem is fixed. Also set self-evolution Autonomy to
**Observe** if AI patches might be involved.

## A tag was pushed by mistake

- **Release job still waiting:** Actions → the MSIX run → *Review deployments*
  → **Reject**. Nothing is published.
- Then delete the tag: `git push origin :refs/tags/vX.Y.Z` and
  `git tag -d vX.Y.Z`.

## A bad release is on GitHub but not yet in the Store

1. Pause submission (above).
2. Mark it so the Store pipeline skips it:
   `gh release edit vX.Y.Z --prerelease --repo buildsbeyondai-code/WindowsAI`.
   `Store submit` reads `releases/latest`, which ignores pre-releases.
3. Check whether a submission already started: run *Store submit* manually with
   **dry_run** ticked and read its summary.
4. Fix forward (next section), then turn submission back on.

## A bad build is already in the Store

The Store accepts only package versions **higher** than the published one, so
roll back by shipping the last good code under a new version:

1. Pause submission.
2. Confirm the last good release is intact:
   `python tools/verify_release.py --tag vLAST_GOOD`.
3. Revert the bad change on `main` (`git revert <commit>`), bump the version
   (`src/sysperf/__init__.py`, `pyproject.toml`, a CHANGELOG entry saying it
   restores vLAST_GOOD behaviour) and tag `vX.Y.(Z+1)`.
4. Approve the release, turn submission back on, and watch the next
   *Store submit* run. Certification usually takes hours; users keep the bad
   build until then.
5. If the build harms users, also halt it in Partner Center (Product → the
   submission → *Halt*) while the fix certifies.

## The Store client secret leaked

1. Pause submission.
2. In the Entra admin centre, open App registrations → **SysPerf Store CLI** →
   Certificates & secrets: create a new secret, then **delete the old one**.
3. Update the repository secret:
   `gh secret set STORE_CLIENT_SECRET --repo buildsbeyondai-code/WindowsAI`.
4. In Partner Center, check the submission history for anything you did not
   start, and halt it.
5. Turn submission back on and run *Store submit* with **dry_run** to confirm
   the new secret works.

## Verifying a release by hand

```bash
python tools/verify_release.py --tag v5.5.3     # downloads with gh, then checks
python tools/verify_release.py path/to/folder   # files you already have
```

Exit code 0 = every file matches `SHA256SUMS.txt` and nothing unlisted is
present; 1 = mismatch, missing or unlisted file; 2 = usage or download error.

## Losing the repository or its history

Keep an off-site mirror and refresh it after each release:

```bash
git clone --mirror https://github.com/buildsbeyondai-code/WindowsAI.git
git -C WindowsAI.git remote update --prune      # later refreshes
```

To restore, create an empty repository and run
`git -C WindowsAI.git push --mirror <new-url>`, then re-add the
`STORE_CLIENT_SECRET` secret, the `STORE_AUTO_SUBMIT` variable and the
`release` environment. Releases and their files are not in git: re-download
them with `gh release download` while the old repository exists, or rebuild
them from the tag.
