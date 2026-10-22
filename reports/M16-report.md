# Milestone 16 report: 2.1.1 released

Date: 2026-10-23 (Sydney). **2.1.1 is released:** `main` and `v2` were fast-forwarded to
`fc2991d`, and the annotated tag **`v2.1.1`** ("Amber Energy Dashboard 2.1.1", tag object
**`eafbf62`**, full `eafbf629fb6bee1794c84558eccce24cfa878c5f`) points to it. All three are
pushed. CI is green on `main`, `v2` and the tag. `fix/own-sensor-devices` is deleted
locally and on GitHub.

**Summary.**
- **M15 change:** the default entry title is now "Amber Energy Dashboard", or "Amber
  Energy Dashboard (…1234)" for each site when there are several, in place of "Amber
  Electric". The migration, tests and docs are updated to match.
- **GitHub access was never broken:** see section 4.
- **Tests:** 508 pass with 100 % coverage locally (9102 stopped), and CI is green on the
  branch and on the release.

## 1. What was built

| Commit | What |
|---|---|
| `e47502b` | Default entry title "Amber Energy Dashboard" (`devices.DEFAULT_TITLE`). The migration needed no other change: `is_default_title` still recognises 2.1.0's "Amber <NMI>" and the new defaults, and "Amber Electric" was never released, so it isn't treated as a default. Tests (`test_init`, `test_config_flow`, `test_energy_setup`, `test_own`) and docs (INSTALL.md page-layout section, DESIGN section 18, CHANGELOG) updated |
| `fc2991d` | CHANGELOG `[2.1.1] - 2026-10-23`; `reports/release-notes-v2.1.1.md` |
| (this) | This report, on `main` |

**Release notes** (`reports/release-notes-v2.1.1.md`), ready to paste into a GitHub
release:
- The retention-boundary fix comes first, with how to check: Settings > Devices &
  services > Amber Energy Dashboard > ⋮ > Download diagnostics, then
  `data.store.retention.method`. A value starting `fallback (Amber…Error)` means the
  boundary was moved during an outage, and 2.1.1 re-measures it at its next run.
- Then the feed-in and own-sensor device fixes, the site device "Amber Energy Dashboard
  (site)" (entity IDs unchanged), the new entry title, and the INSTALL.md section.

No GitHub release object was created (there is no `gh` here, and the brief asked only for
the tag).

## 2. Test results

- **Local, with 9102 stopped:**
  - `ruff check` and `ruff format --check` are clean.
  - `uv run pytest --cov=custom_components.amber_energy_dashboard` (CI's command): **508
    passed**, **100 % coverage** (every file at 100 %), 7 min 0 s, 1 warning.
- **The warning:** a rerun with `-rw` printed no warnings summary, so it didn't recur, and
  I couldn't capture its text. CI's Tests runs are green.
- **CI on `e47502b` (branch):** Tests [39861468804], hassfest [39861468811] and HACS
  validation [39861468818] all succeeded.
- **CI on `fc2991d` (release):** all 9 runs succeeded:

  | Ref | Tests | hassfest | HACS validation |
  |---|---|---|---|
  | `main` | 39863204408 | 39863204282 | 39863204272 |
  | `v2` | 39863204466 | 39863204534 | 39863204467 |
  | `v2.1.1` | 39863204899 | 39863204629 | 39863204627 |

  (Run IDs under `https://github.com/r5e/ha-amber-energy-dashboard/actions/runs/`.)

## 3. Lab verification

None needed for the title change (unit tests cover the migration). **Item 4 skipped:**
- 9102 stayed stopped, so its 07:15 run never happened.
- Its record's failed attempt is dated today (`measured_on` 2026-10-23), and rediscovery
  runs at most once a day, so starting it today would not re-measure the boundary.
- From 2026-10-24, starting it and pressing Run now (or waiting for 07:15) should show a
  measured `method`.

## 4. Open questions assigned to the milestone

**GitHub access: nothing was wrong; the check command was.**
- `ssh -T git@github.com` gives `Permission denied (publickey)`. That is expected here:
  - there is no ssh-agent (`SSH_AUTH_SOCK` is unset), and none is needed;
  - `~/.ssh/config` gives the deploy key `~/.ssh/github_amber` only to the host alias
    `github-amber` (`HostName github.com`, `IdentitiesOnly yes`), so a plain
    `git@github.com` offers no key.
- `git remote -v` uses that alias: `git@github-amber:r5e/ha-amber-energy-dashboard.git`.
- `ssh -T github-amber` gave "Hi r5e/ha-amber-energy-dashboard! You've successfully
  authenticated", and `git ls-remote` and every push worked.
- No change was made. The correct check is `ssh -T github-amber`.

## 5. Deviations and points for the planning chat

- **The 9102 clone keeps the title "Amber Electric".** That was a default for a few hours
  in unreleased code. It isn't in the default list, so the clone keeps it as if a user had
  chosen it. Unreleased, so no user is affected.
- **Commit to `main`:** this report was committed to `main` directly, as instructed (an
  exception to the CLAUDE.md "never commit to main" rule).

## 6. Live Amber API calls

None in this milestone.

## 7. Lab state left behind

- **9102 `amber-test-m3`: stopped**, unchanged since M15: 2.1.1 pre-release code, with the
  title "Amber Electric". Its snapshots are as listed in M15.
- No other clones. VM 101 was not touched.

## 8. Recommended next steps

1. Create the GitHub release for `v2.1.1` from `reports/release-notes-v2.1.1.md` (HACS
   shows the release notes from it).
2. Production: update through HACS, restart, then download diagnostics and check
   `data.store.retention.method`. Also confirm the title (if it was "Amber <NMI>") and the
   site device's new name.
3. Optionally, from 2026-10-24: start 9102 for the retention re-measure check (item 4),
   then stop it again.
