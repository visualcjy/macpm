# Releasing macpm 2.0

This checklist prepares a GitHub tag and a PyPI upload.  It does not push or
publish anything by itself.

## 1. Preconditions

- macOS Apple Silicon, Python 3.9+.
- Clean working tree (`git status`), all tests passing.
- Version is `2.0` in:
  - `setup.py`
  - `macpm/__init__.py`
  - `macpm/macpm.py` (`version = 'macpm v2.0'`)
- `CHANGELOG.md` has the 2.0 entry.

## 2. Test from source

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -e .
python3 -m unittest discover -s tests -v

macpm --help
macpm-dump           # one sample per second, Ctrl+C to stop
macpm-sensors        # temperatures/fans, optional
```

## 3. Build and validate PyPI artifacts

```sh
python3 -m pip install --upgrade build twine
rm -rf dist build *.egg-info
python3 -m build
python3 -m twine check dist/*
```

Test the wheel in a **clean** venv (this is the real "no source tree" test):

```sh
python3 -m venv /tmp/macpm-wheel-test
/tmp/macpm-wheel-test/bin/pip install dist/macpm-2.0-py3-none-any.whl
/tmp/macpm-wheel-test/bin/macpm --help
/tmp/macpm-wheel-test/bin/macpm-dump   # Ctrl+C
```

## 4. Publish to PyPI

Publishing uses PyPI Trusted Publishing (OIDC) — no API token is stored in
the repository or in GitHub secrets.

One-time setup:

1. On PyPI, add a trusted publisher.  Because `macpm` already exists on
   PyPI, use the project settings page —
   <https://pypi.org/manage/project/macpm/settings/publishing/> —
   not the account-level "pending publisher" page (that one is only for
   projects that do not exist yet).

   | Field | Value |
   |---|---|
   | Owner | `visualcjy` |
   | Repository name | `macpm` |
   | Workflow name | `release.yml` |
   | Environment name | `pypi` |

2. On GitHub, keep the repository variable
   `PUBLISH_PYPI=true` (Settings → Secrets and variables → Actions →
   Variables) and the `pypi` environment.  Without the variable the release
   workflow only creates the GitHub release.

After that, pushing a `v*` tag builds and uploads to PyPI automatically:
the `release.yml` workflow runs in the `pypi` environment, and the PyPI job
skips files that already exist, so re-runs are safe.  A manual
`workflow_dispatch` on the tag re-runs only the build and PyPI jobs.

Manual alternative (fallback, e.g. when OIDC is unavailable):

```sh
# requires a PyPI API token in ~/.pypirc or TWINE_PASSWORD
python3 -m twine upload --non-interactive \
    dist/macpm-2.0.tar.gz dist/macpm-2.0-py3-none-any.whl
```

Then verify:

```sh
python3 -m pip install --upgrade macpm==2.0
macpm --version
```

## 5. Publish to GitHub

```sh
git add setup.py pyproject.toml MANIFEST.in README.md CHANGELOG.md RELEASE.md \
        macpm packaging tests .github bin
git commit -m "Release macpm 2.0: rootless IOReport sampler"
git tag -a v2.0 -m "macpm 2.0"
git push origin main
git push origin v2.0
```

The `Release` workflow builds the sdist/wheel, checks metadata and attaches
the artifacts to a GitHub release.  The PyPI job is opt-in: set the repository
variable `PUBLISH_PYPI=true` (Settings → Secrets and variables → Actions →
Variables) to publish to PyPI via trusted publishing.  Leaving it unset keeps
tag pushes to a GitHub release only.
If you prefer a manual release, create one from the tag and upload
`dist/*.whl`, `dist/*.tar.gz` and the standalone tarball.

## 6. Standalone arm64 binaries (optional)

On an Apple Silicon build machine:

```sh
PYTHON=python3 packaging/build.sh
```

This produces:

```text
dist/macpm-2.0-macos-arm64.tar.gz
```

Attach it to the GitHub release.  The binaries are ad-hoc signed, not
notarized; see `packaging/README.txt` for the Gatekeeper note.  To notarize,
pass `CODESIGN_ID` to the build script and submit/staple the archive with
`notarytool`.
