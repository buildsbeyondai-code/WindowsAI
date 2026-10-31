# Release checklist

Copy this into the release notes and fill in every **Result** with what you
actually saw. A gate is PASS, FAIL, N/A or UNVERIFIED, and UNVERIFIED is
never PASS. Ship only when no gate is FAIL and every UNVERIFIED gate is named
under *Known limitations* in the CHANGELOG.

## 1. Version

- [ ] `pyproject.toml`, `src/sysperf/__init__.py` and `packaging/msix/AppxManifest.xml` carry the same version
- [ ] CHANGELOG has the entry, including *Known limitations*

## 2. Gates

| Gate | Command / method | Minimum evidence | Result |
|---|---|---|---|
| Requirements | `python -m pytest tests/test_requirements_doc.py` | Every acceptance criterion cites an existing test | |
| Real functionality | `python -m pytest tests/test_livestats_win.py` on Windows; spot-check `/api/live` against Task Manager | Values agree with an independent OS reading | |
| Code quality | `python -m ruff check src tests`; `python -m compileall -q src tests tools`; review `git diff <last-tag>..` | Clean | |
| Testing | `python -m pytest -rs` on this Windows PC (the pre-push hook runs it); Linux, the bubblewrap sandbox and the Python matrix come from the CI run on the release commit, not repeated by hand | 0 failed locally and a green CI run; every skip has a reason | |
| E2E | `python -m pytest tests/test_e2e_ui.py tests/test_e2e_v41.py` with Playwright | 0 failed | |
| Security | `python -m pytest tests/test_hostguard.py tests/test_security_doc.py`; `curl -H "Host: evil.example" http://127.0.0.1:5056/api/health` returns 403 | Guards hold on the running build | |
| Supply chain | CI `supply-chain` job: lock check, `pip-audit`, SBOM artefact | No known vulnerability; SBOM attached | |
| Reliability | `python -m pytest tests/test_supervisor.py`; kill the service once and watch it come back | Restart within 60 s | |
| Performance | `/api/diagnostics` footprint after 1 h; `python tools/soak.py --hours 24` before a minor release | Within REQUIREMENTS.md targets | |
| Packaging | `packaging/msix/build-msix.ps1` from the release commit; install, first launch, upgrade over the previous version, uninstall | Each step works; data survives the upgrade | |
| Operations | Diagnostics tab shows workers alive and no errors | Clean | |

## 3. Ship

- [ ] Tag the release commit (`git tag v<version>`)
- [ ] Build the wheel/sdist (CI `package` job) and the MSIX from that tag
- [ ] Attach the SBOM and this filled-in checklist to the release
- [ ] Update `docs/PROJECT_STATE.md`
