# electa-bot

Polls Jira Cloud for tickets labeled for the bot, checks each is implementable, has a coding agent
(`agy` or `claude`) implement it in an isolated git worktree, verifies the result, and opens a **draft** GitHub PR.
A human reviews and merges; the bot never merges and only pushes `bot/*` branches.

## Setup
```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
copy config.example.toml config.toml   # edit Jira URL/JQL, repo settings
copy .env.example .env                 # JIRA_EMAIL, JIRA_API_TOKEN (GitHub falls back to `gh auth token`)
.venv\Scripts\python -m pytest         # no network or engine credits needed
```

## Use
```powershell
electa-bot --dry-run ticket VS-123    # full run on one ticket, no Jira writes / push / PR
electa-bot --dry-run run               # poll once, same dry behavior
electa-bot run                         # for real
```
Kill switch: create `~/.electa-bot/.bot-paused` (or whatever `work_dir` is) and runs exit immediately.
Schedule `electa-bot run` every ~15 minutes with Windows Task Scheduler; an internal lock prevents overlaps.

## How a ticket flows
claim (label `bot-claimed` in Jira) -> readiness check (vague tickets get questions, no code) -> worktree off
`origin/<default>` -> engine edits -> guardrails (non-empty diff, size cap, allowed/denied paths) -> setup + check
commands from config -> re-verify diff -> commit (repo hooks run) -> push -> draft PR -> comment + transition in Jira.
Failures comment on the ticket, move it to the `needs_info` status, keep the claim label, and notify Slack if configured.

## Engines
- `antigravity`: `agy --mode accept-edits --sandbox --output-format json -p ...` (verified headless; needs AI credits).
- `claude`: `claude -p ... --permission-mode acceptEdits` (written from docs, **untested**, CLI not installed here).
