from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from . import guardrails, triage
from .config import Config, RepoConfig
from .engine.base import CodeEngine
from .github import GitHubClient
from .jira import Issue, JiraClient
from .notify import Notifier
from .prompt import build_pr_body, build_repair_prompt, build_task_prompt
from .repo import RepoManager, slugify
from .state import State

log = logging.getLogger("electa_bot")


@dataclass
class Outcome:
    key: str
    status: str  # pr_opened | dry_run_ok | needs_info | failed | skipped
    detail: str = ""
    pr_url: str | None = None


class Pipeline:
    def __init__(
        self,
        cfg: Config,
        jira: JiraClient,
        repos: RepoManager,
        github: GitHubClient,
        engine: CodeEngine,
        state: State,
        notifier: Notifier,
        dry_run: bool = False,
    ):
        self.cfg = cfg
        self.jira = jira
        self.repos = repos
        self.github = github
        self.engine = engine
        self.state = state
        self.notifier = notifier
        self.dry_run = dry_run

    # -- batch ---------------------------------------------------------------------------------
    def run_once(self) -> list[Outcome]:
        if self.cfg.paused_file.exists():
            self.notifier.send(f"electa-bot paused ({self.cfg.paused_file} exists); nothing done.")
            return []
        budget = min(self.cfg.max_tickets_per_run, self.cfg.daily_cap - self.state.count_today())
        if budget <= 0:
            self.notifier.send("electa-bot: daily ticket cap reached; nothing done.")
            return []

        self.repos.prune_stale()
        issues = self.jira.search(self.cfg.jira.jql, max_results=budget)
        outcomes = [self.process(issue) for issue in issues[:budget]]
        if outcomes:
            self.notifier.send(self.summary(outcomes))
        return outcomes

    @staticmethod
    def summary(outcomes: list[Outcome]) -> str:
        lines = [f"electa-bot run: {len(outcomes)} ticket(s)"]
        lines += [f"- {o.key}: {o.status}" + (f" {o.pr_url}" if o.pr_url else f" ({o.detail})" if o.detail else "") for o in outcomes]
        return "\n".join(lines)

    # -- one ticket ----------------------------------------------------------------------------
    def process(self, issue: Issue) -> Outcome:
        repo = self.cfg.repo_for(issue.project)
        if repo is None:
            return Outcome(issue.key, "skipped", f"no repo configured for Jira project {issue.project}")
        try:
            return self._process(issue, repo)
        except Exception as exc:  # one bad ticket must never stop the batch
            log.exception("unexpected failure on %s", issue.key)
            return self._fail(issue, f"Unexpected error: {exc}")

    def _process(self, issue: Issue, repo: RepoConfig) -> Outcome:
        self._claim(issue)

        readiness = triage.check_readiness(self.engine, issue, self.cfg.engine_timeout_s)
        if not readiness.ready:
            questions = "\n".join(f"- {q}" for q in readiness.questions)
            self._write(self.jira.remove_label, issue.key, self.cfg.jira.claimed_label)
            self._say(issue, f"The bot needs more detail before it can implement this:\n{questions}")
            self._move(issue, "needs_info")
            return self._finish(Outcome(issue.key, "needs_info", questions))

        branch = f"bot/{issue.key}-{slugify(issue.summary)}"
        worktree = self.repos.create_worktree(repo, branch)
        try:
            base = self.repos.head_sha(worktree)
            return self._implement(issue, repo, branch, worktree, base)
        finally:
            self.repos.remove_worktree(repo, worktree)

    def _implement(self, issue: Issue, repo: RepoConfig, branch: str, worktree: Path, base: str) -> Outcome:
        # Setup runs BEFORE the engine so the repo's own tooling (node_modules, lint hooks, a skills
        # sync) is in place while the agent edits. Setup output is re-checked by the final diff guard.
        npm_cache = {"npm_config_cache": str(self.cfg.work_dir / "npm-cache")}
        failed = self._first_failure(guardrails.run_commands(repo.setup_commands, worktree, extra_env=npm_cache))
        if failed:
            return self._fail(issue, f"Setup `{failed.command}` failed:\n{failed.output_tail}")

        agents_md = self._read(worktree / "AGENTS.md")
        result = self.engine.run(worktree, build_task_prompt(issue, repo, agents_md), self.cfg.engine_timeout_s)
        if not result.ok:
            return self._fail(issue, f"The coding engine failed: {result.error}")

        problems = guardrails.check_changes(self.repos.collect_changes(worktree, base), repo)
        if problems:
            return self._fail(issue, "Guardrails rejected the change:\n" + "\n".join(f"- {p}" for p in problems))

        # Checks may fail on code the engine just wrote (lint, types, tests). Feed the failure back
        # for a bounded number of repair rounds; each round re-verifies the diff before re-checking.
        repairs = 0
        while True:
            failed = self._first_failure(guardrails.run_commands(repo.check_commands, worktree, extra_env=npm_cache))
            if failed is None:
                break
            if repairs >= self.cfg.repair_rounds:
                suffix = f" (after {repairs} repair attempt(s))" if repairs else ""
                return self._fail(issue, f"`{failed.command}` failed{suffix}:\n{failed.output_tail}")
            repairs += 1
            log.info("%s: `%s` failed; repair round %d/%d", issue.key, failed.command, repairs, self.cfg.repair_rounds)
            fix = self.engine.run(
                worktree, build_repair_prompt(issue, repo, failed.command, failed.output_tail), self.cfg.engine_timeout_s
            )
            if not fix.ok:
                return self._fail(issue, f"The coding engine failed during repair: {fix.error}")
            problems = guardrails.check_changes(self.repos.collect_changes(worktree, base), repo)
            if problems:
                return self._fail(issue, "Guardrails rejected the repaired change:\n" + "\n".join(f"- {p}" for p in problems))

        # Setup/checks can modify files (lockfiles, formatters), so re-verify the final diff.
        changes = self.repos.collect_changes(worktree, base)
        problems = guardrails.check_changes(changes, repo)
        if problems:
            return self._fail(issue, "Guardrails rejected the final change:\n" + "\n".join(f"- {p}" for p in problems))

        if self.dry_run:
            return Outcome(issue.key, "dry_run_ok", f"{len(changes.files)} file(s), {changes.lines} line(s)")

        self.repos.commit(worktree, f"{issue.key}: {issue.summary}", base)
        self.repos.push(worktree, branch)
        template = self._read(worktree / ".github" / "pull_request_template.md")
        body = build_pr_body(
            f"{self.cfg.jira.base_url}/browse/{issue.key}",
            issue,
            result.response,
            template,
            [*repo.setup_commands, *repo.check_commands],
            repairs,
        )
        pr = self.github.open_draft_pr(
            repo.name, branch, repo.default_branch, f"{issue.key}: {issue.summary}", body, labels=["bot"]
        )
        self._say(issue, f"Draft PR opened: {pr.url}")
        self._move(issue, "in_review")
        return self._finish(Outcome(issue.key, "pr_opened", pr_url=pr.url))

    # -- helpers -------------------------------------------------------------------------------
    @staticmethod
    def _first_failure(results: list[guardrails.CheckResult]) -> guardrails.CheckResult | None:
        return next((r for r in results if not r.ok), None)

    @staticmethod
    def _read(path: Path) -> str:
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    def _write(self, fn, *args) -> None:
        """Run a Jira write unless this is a dry run."""
        if self.dry_run:
            log.info("[dry-run] skip %s%s", fn.__name__, args)
            return
        fn(*args)

    def _claim(self, issue: Issue) -> None:
        self._write(self.jira.add_label, issue.key, self.cfg.jira.claimed_label)
        self._move(issue, "in_progress")
        self._say(issue, "Bot picked this up.")

    def _say(self, issue: Issue, text: str) -> None:
        self._write(self.jira.comment, issue.key, text)

    def _move(self, issue: Issue, which: str) -> None:
        name = self.cfg.jira.transitions[which]
        if self.dry_run:
            log.info("[dry-run] skip transition %s -> %s", issue.key, name)
        elif not self.jira.transition(issue.key, name):
            log.warning("%s: no transition named %r available", issue.key, name)

    def _fail(self, issue: Issue, reason: str) -> Outcome:
        # The claimed label stays so the ticket is not re-picked; a human decides what happens next.
        try:
            self._say(issue, f"The bot could not complete this ticket.\n{reason}")
            self._move(issue, "needs_info")
        except Exception:  # reporting must not mask the original failure
            log.exception("could not report failure on %s", issue.key)
        self.notifier.send(f"electa-bot: {issue.key} failed: {reason[:300]}")
        return self._finish(Outcome(issue.key, "failed", reason))

    def _finish(self, outcome: Outcome) -> Outcome:
        if not self.dry_run:
            self.state.record(outcome.key, outcome.status, outcome.detail, outcome.pr_url)
        return outcome
