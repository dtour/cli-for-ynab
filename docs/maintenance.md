# Maintaining CLI for YNAB

## Where things live

| Location | Purpose |
| --- | --- |
| `src/ynab_cli/cli.py` | Command parsing and JSON interface |
| `src/ynab_cli/client.py` | Official SDK transport and serialization |
| `src/ynab_cli/api_registry.py` | Complete SDK operation catalog and schemas |
| `src/ynab_cli/api_operations.py` | Generic API proposals, approval, and receipts |
| `src/ynab_cli/operations.py` | Categorization, allocations, transfers between categories, targets |
| `src/ynab_cli/reports.py` | Spending aggregation |
| `tests/` | Synthetic tests; network and real credentials are blocked |
| `docs/agents.md` | Guide bundled into `ynab guide` |
| `uv.lock` | Exact development and runtime dependency versions |
| `scripts/release_assets.py` | Build versioned assets and the Homebrew formula |
| `.github/workflows/check.yml` | Shared lint, test, and build matrix for CI and releases |
| `dtour/homebrew-tools` | Published formula and Homebrew install tests |

The installed Homebrew copy is separate from a source checkout. Editing source
does not change the installed `ynab`. Use `uv run ynab` in the source checkout
during development; install a new release through Homebrew for normal use.
Credentials and preferences belong to the user's OS, outside both repositories.

## Development

```sh
git clone https://github.com/dtour/cli-for-ynab.git
cd cli-for-ynab
uv sync --locked --no-editable
uv run ruff check .
uv run ruff format --check .
uv run pytest
uv build
```

Push changes to a branch and open a pull request. CI covers Python 3.12 and 3.14
on Linux and macOS. Keep test fixtures synthetic. Include a regression test for
bug fixes and preserve the policy of automatic routine categorization with
review for other changes. Git commits use `dtour <dhiraj.tourani@gmail.com>`.

## Dependencies

For an ordinary dependency update, run `uv lock --upgrade`, inspect the lockfile,
and run the checks above. YNAB is explicitly pinned in `pyproject.toml`; update
that pin deliberately after reviewing its API changes. The catalog test detects
SDK methods that need adding or updating. For a new SDK operation, update the
catalog, policy, test coverage, README, and Homebrew operation-count check.

Review pinned GitHub Actions and uv versions when updating dependencies. The shared
checks and release job use the same versions. Review GitHub dependency alerts as
they appear.
The project does not automatically merge updates or publish on every commit.

## Release

1. Update the version in `pyproject.toml` and `src/ynab_cli/__init__.py`.
   Run `uv lock` and write `docs/releases/vVERSION.md`.
2. Run the checks above. Preview the assets locally with
   `uv run scripts/release_assets.py --tag vVERSION`.
3. Commit, push, and wait for CI to pass. Tag that commit and push the tag:

   ```sh
   git tag vVERSION
   git push origin vVERSION
   ```

4. The Release workflow runs the shared Linux/macOS and Python 3.12/3.14 matrix
   on the tagged commit. Publishing waits for all four combinations to pass lint,
   tests, and builds. Failed or cancelled checks prevent publication. Tag pushes
   run this matrix in Release only; branch pushes and pull requests run it in CI.
   It then publishes the wheel, source archive, `cli-for-ynab.rb`, and `SHA256SUMS`
   to GitHub Releases. Never replace assets for an existing version; fix mistakes
   in a new version.
5. Update the tap from the published formula, after checking the release assets:

   ```sh
   cd /path/to/homebrew-tools
   gh release download vVERSION --repo dtour/cli-for-ynab \
     --pattern cli-for-ynab.rb --dir Formula --clobber
   brew style Formula/cli-for-ynab.rb
   git add Formula/cli-for-ynab.rb
   git commit -m "Update CLI for YNAB to vVERSION"
   git push
   ```

   Tap CI installs and tests that exact formula on Apple Silicon and Intel.
   Users receive it through `brew update` and `brew upgrade dtour/tools/cli-for-ynab`.

The tap uses checked, pinned wheels and supports macOS 14 or later. Moving into
Homebrew core is a separate future contribution, subject to its current package
acceptance and build requirements.
