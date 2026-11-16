# Publishing a release

Releases are built and uploaded by GitHub Actions
(`.github/workflows/release.yml`) when a version tag is pushed. Nobody needs a
PyPI password or API token.

## One-time setup

1. **Choose a license** and add a `LICENSE` file, plus a `license` entry in
   `pyproject.toml`. Without one, nobody else may legally use the package.
2. **Create a PyPI account** at <https://pypi.org> for the person who will own
   the project, with two-factor authentication.
3. **Add a trusted publisher on PyPI** (Account settings → Publishing → Add a
   new pending publisher):
   - PyPI project name: `ai-permission-supervisor`
   - Owner: `TonyINSY6629`
   - Repository: `IBM_SkillBuild_Fall2026_Team_TINSY_AI-Permission-Supervisor-System`
   - Workflow: `release.yml`
   - Environment: `pypi`
4. **Create the `pypi` environment on GitHub** (repository Settings →
   Environments → New environment → `pypi`). Optionally require a reviewer, so
   every release needs a second person's approval.

## Each release

1. Make sure CI is green on `main`.
2. Update the version in `ai_permission_supervisor/__init__.py`
   (`__version__ = "0.1.1"`) and add a section to `CHANGELOG.md`.
3. Commit, then tag and push:

   ```bash
   git tag v0.1.1
   git push origin main v0.1.1
   ```

4. The release workflow checks that the tag matches the version, runs the
   tests, builds the package, uploads it to PyPI, and creates a GitHub release
   with the files attached.

## Checking a build locally

```bash
pip install -e ".[dev]"
python -m build
python -m twine check dist/*
```

To try the wheel exactly as a user would, install it into a fresh virtual
environment and run `aps doctor`.
