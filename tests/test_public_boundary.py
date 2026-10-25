import io
import os
import runpy
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

CHECK = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/check_public.py"))


def test_public_examples_reject_operational_context_but_allow_generic_paths():
    check = CHECK["content_has_private_context"]
    assert check("README.md", "private/config/profile-bulk-20260101.toml")
    assert check("fixture.json", '{"path": "/home/candidate/Documents/cv.pdf"}')
    assert not check("README.md", "private/config/profile.toml")
    assert not check("README.md", "/home/user/project/")


def test_nested_credentials_are_forbidden():
    check = CHECK["forbidden_paths"]
    assert check(["package/.env", "package/telegram.session", "package/token.json"])
    assert not check(["package/.env.example", "tests/fixtures/public/profile.toml"])


def test_automated_ci_cannot_be_reintroduced_in_public_files_or_archives():
    paths = [".github/workflows/ci.yml", "package/.github/workflows/release.yml",
             ".gitlab-ci.yml", ".circleci/config.yml"]
    assert CHECK["forbidden_paths"](paths) == paths


def test_history_check_ignores_broken_non_branch_codex_checkpoint_refs(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "README.md").write_text("Generic project documentation\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test Author",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test Author",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "initial"],
                   check=True, env=env)
    broken_ref = repo / ".git" / "refs" / "codex" / "checkpoints" / "stale"
    broken_ref.parent.mkdir(parents=True)
    broken_ref.write_text("f" * 40 + "\n", encoding="ascii")

    assert CHECK["check_history"](str(repo)) == []


@pytest.mark.parametrize("suffix", [".whl", ".tar.gz"])
def test_distribution_privacy_checks_include_fixtures(tmp_path, monkeypatch, suffix):
    monkeypatch.chdir(tmp_path)
    Path("dist").mkdir()
    payload = b'{"resume": "/home/candidate/Documents/cv.pdf"}'
    path = Path("dist") / ("example" + suffix)
    if suffix == ".whl":
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("fixtures/candidate.json", payload)
    else:
        with tarfile.open(path, "w:gz") as archive:
            info = tarfile.TarInfo("fixtures/candidate.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    assert CHECK["check_artifacts"]()
