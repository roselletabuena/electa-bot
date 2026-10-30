from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from electa_bot.config import Config, JiraConfig, RepoConfig
from electa_bot.engine.base import EngineResult
from electa_bot.github import PullRequest
from electa_bot.jira import Issue
from electa_bot.notify import Notifier
from electa_bot.pipeline import Pipeline
from electa_bot.repo import RepoManager
from electa_bot.state import State

GIT_ID = ["-c", "user.name=t", "-c", "user.email=t@t"]


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare 'GitHub' with one commit on main containing src/app.txt and AGENTS.md."""
    bare = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    git("init", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    git("init", "-b", "main", str(seed), cwd=tmp_path)
    (seed / "src").mkdir()
    (seed / "src" / "app.txt").write_text("hello\n")
    (seed / "AGENTS.md").write_text("Be careful.\n")
    git("add", "-A", cwd=seed)
    git(*GIT_ID, "commit", "-m", "init", cwd=seed)
    git("remote", "add", "origin", str(bare), cwd=seed)
    git("push", "origin", "main", cwd=seed)
    return bare


@pytest.fixture
def repo_cfg() -> RepoConfig:
    return RepoConfig(
        name="acme/app",
        jira_projects=["ELE"],
        check_commands=['python -c "print(1)"'],
        allowed_paths=["src/"],
        denied_paths=[".github/", ".env*"],
        max_diff_lines=50,
    )


@pytest.fixture
def cfg(tmp_path: Path, repo_cfg: RepoConfig) -> Config:
    return Config(
        jira=JiraConfig(base_url="https://x.atlassian.net", jql="project = VS"),
        repos=[repo_cfg],
        work_dir=tmp_path / "work",
    )


class FakeJira:
    def __init__(self):
        self.calls: list[tuple] = []

    def add_label(self, key, label): self.calls.append(("add_label", key, label))
    def remove_label(self, key, label): self.calls.append(("remove_label", key, label))
    def comment(self, key, text): self.calls.append(("comment", key, text))
    def transition(self, key, name):
        self.calls.append(("transition", key, name))
        return True

    def search(self, jql, max_results=25): return [make_issue()]
    def names(self): return [c[0] for c in self.calls]


class FakeGitHub:
    def __init__(self):
        self.opened: list[dict] = []

    def open_draft_pr(self, repo, branch, base, title, body, labels=None):
        self.opened.append({"repo": repo, "branch": branch, "base": base, "title": title, "body": body})
        return PullRequest(1, f"https://github.com/{repo}/pull/1")


class FakeEngine:
    """Answers the readiness prompt, and 'implements' by writing a file (or not)."""

    def __init__(
        self,
        ready=True,
        edits: dict[str, str] | None = None,
        fail=False,
        repair_edits: dict[str, str] | None = None,
        repair_fail=False,
        commits=False,
        marker: Path | None = None,
    ):
        self.ready = ready
        self.edits = {"src/app.txt": "hello electa\n"} if edits is None else edits
        self.fail = fail
        self.repair_edits = repair_edits or {}
        self.repair_fail = repair_fail
        self.commits = commits  # simulate an agent that commits despite being told not to
        self.marker = marker  # file a setup command creates; lets tests see setup ran first
        self.marker_seen: bool | None = None
        self.prompts: list[str] = []

    @property
    def repair_prompts(self) -> list[str]:
        return [p for p in self.prompts if p.startswith("REPAIR")]

    def run(self, workdir: Path, prompt: str, timeout_s: int) -> EngineResult:
        self.prompts.append(prompt)
        if self.fail:
            return EngineResult(ok=False, error="credits exhausted")
        if prompt.startswith("READINESS CHECK"):
            body = '{"ready": true, "questions": []}' if self.ready else '{"ready": false, "questions": ["Which page?"]}'
            return EngineResult(ok=True, response=body)
        if prompt.startswith("REPAIR"):
            if self.repair_fail:
                return EngineResult(ok=False, error="repair crashed")
            for rel, content in self.repair_edits.items():
                target = workdir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
            return EngineResult(ok=True, response="Fixed it.")
        self.marker_seen = self.marker.exists() if self.marker else None
        for rel, content in self.edits.items():
            target = workdir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        if self.commits:
            git("add", "-A", cwd=workdir)
            git(*GIT_ID, "commit", "-m", "agent's own commit", cwd=workdir)
        return EngineResult(ok=True, response="Changed the greeting.")


def make_issue(key="ELE-1", summary="Fix greeting", project="ELE") -> Issue:
    return Issue(key=key, summary=summary, description="Say hello electa", project=project)


@pytest.fixture
def build(tmp_path: Path, origin: Path, cfg: Config):
    def _build(engine: FakeEngine, dry_run: bool = False):
        jira, gh = FakeJira(), FakeGitHub()
        repos = RepoManager(cfg.work_dir, remote_url=lambda name: str(origin))
        pipeline = Pipeline(cfg, jira, repos, gh, engine, State(cfg.work_dir / "s.sqlite"), Notifier(), dry_run)
        return pipeline, jira, gh, repos

    return _build
