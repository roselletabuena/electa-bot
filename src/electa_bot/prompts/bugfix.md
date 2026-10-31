{% from "_partials.md" import ticket_note, ticket, rules %}
BUGFIX. You are fixing the defect in Jira ticket {{ issue.key }} in this repository, following the repo's spec-aware bugfix method (.agents/skills/spec-aware-bugfix/SKILL.md; read it if present).
{{ ticket_note() }}

{{ ticket(issue) }}

{% if agents_md | trim %}
Repository conventions (AGENTS.md):
{{ agents_md | trim }}

{% endif %}
Method:
{% if spec_hint %}
- The affected feature probably lives in `{{ spec_hint }}`; check its spec.md first.
{% endif %}
- Find the root cause, then classify the defect:
  A = pure code defect (spec is right): add a failing regression test at the seam, then the minimal fix.
  B = stale spec (spec disagrees with intended behavior): update spec.md first with a short revision note, align contracts and tests, then the code.
  C = underspecified boundary: append the missing scenario to the spec's edge cases, add a boundary test, then the guard.
  D = the fix would break a contract/invariant or the constitution, or needs a product decision: change NOTHING and explain the trade-offs.
- A regression test is mandatory for A, B and C.
{{ rules(repo) }}
- You cannot run commands; the bot runs the repo's checks afterwards, so write lint-clean, typed code.
- Finish with a short plain-text report (root cause, spec changes, code changes, regression test) whose LAST line is exactly `CLASS: A`, `CLASS: B`, `CLASS: C` or `CLASS: D`.
