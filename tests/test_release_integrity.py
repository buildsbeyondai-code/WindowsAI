"""Release integrity: tools/verify_release.py and the workflow guarantees it relies on."""
from __future__ import annotations

import hashlib
import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _tool():
    spec = importlib.util.spec_from_file_location("verify_release", ROOT / "tools" / "verify_release.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vr = _tool()


def _release(tmp_path: Path, files: dict[str, bytes], star: bool = False) -> Path:
    for name, data in files.items():
        (tmp_path / name).write_bytes(data)
    sep = " *" if star else "  "
    lines = [f"{hashlib.sha256(d).hexdigest()}{sep}{n}" for n, d in files.items()]
    (tmp_path / vr.SUMS).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return tmp_path


def test_matching_release_verifies(tmp_path, capsys):
    folder = _release(tmp_path, {"SysPerf_1.0.0.0_x64.msix": b"x64", "SysPerf_1.0.0.0_arm64.msix": b"arm"})
    assert vr.verify(folder) == []
    assert vr.main([str(folder)]) == 0
    assert "2 file(s) match" in capsys.readouterr().out


def test_binary_mode_marker_is_accepted(tmp_path):
    assert vr.verify(_release(tmp_path, {"a.msix": b"a"}, star=True)) == []


def test_tampered_file_fails(tmp_path, capsys):
    folder = _release(tmp_path, {"a.msix": b"good"})
    (folder / "a.msix").write_bytes(b"evil")
    assert vr.verify(folder) == ["checksum mismatch: a.msix"]
    assert vr.main([str(folder)]) == 1
    assert "FAILED" in capsys.readouterr().out


def test_missing_and_unlisted_packages_fail(tmp_path):
    folder = _release(tmp_path, {"a.msix": b"a", "b.msix": b"b"})
    (folder / "b.msix").unlink()
    (folder / "extra.msixbundle").write_bytes(b"?")
    (folder / "notes.md").write_text("not a package")      # non-packages are ignored
    assert vr.verify(folder) == ["missing: b.msix", "not listed in SHA256SUMS.txt: extra.msixbundle"]


@pytest.mark.parametrize("text, error", [
    ("", "is empty"),
    ("not a checksum line\n", "line 1"),
    ("0" * 64 + "  ../escape.msix\n", "names a path"),
    ("0" * 64 + "  a.msix\n" + "1" * 64 + "  a.msix\n", "twice"),
])
def test_malformed_sums_are_rejected(text, error):
    with pytest.raises(ValueError, match=re.escape(error)):
        vr.parse_sums(text)


def test_missing_sums_file_and_bad_folder(tmp_path):
    assert vr.verify(tmp_path) == [f"SHA256SUMS.txt not found in {tmp_path}"]
    assert vr.main([str(tmp_path / "nope")]) == 2


def test_tag_download_failure_is_a_usage_error(monkeypatch, capsys):
    def fail(tag, repo, dest):
        raise RuntimeError("no such release")
    monkeypatch.setattr(vr, "download", fail)
    assert vr.main(["--tag", "v0.0.0"]) == 2
    assert "no such release" in capsys.readouterr().err


def test_every_workflow_action_is_pinned_to_a_commit():
    """A moved tag on a third-party action cannot change what CI or a release runs."""
    unpinned = []
    for wf in WORKFLOWS:
        for n, line in enumerate(wf.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"\s*-?\s*uses:\s*([^\s#]+)", line)
            if m and not m.group(1).startswith("./") and not re.search(r"@[0-9a-f]{40}$", m.group(1)):
                unpinned.append(f"{wf.name}:{n} {m.group(1)}")
    assert not unpinned, unpinned


def test_release_publishing_waits_for_the_release_environment():
    """Publishing a GitHub Release (which the Store-submit workflow then ships) is gated."""
    text = (ROOT / ".github" / "workflows" / "msix.yml").read_text(encoding="utf-8")
    job = text.split("\n  release:\n", 1)[1]
    assert re.search(r"^    environment: release$", job, re.M)
    assert "gh release create" in job
    # The only job that can write to the repository is the gated one.
    assert text.count("contents: write") == 1
