# PRD-21205 · Code review

Status: frozen @ a386444

## Round 1 · 2026-10-30 · reviewed dev/PRD-21205 @ 39c071b (base main @ aeff9a9)

Verdict: approved

### Findings

1. [suggestion] `src/superops_mcp/tools/automation.py:134` (`superops_automation_scripts_run`) — AC4 says "a missing or unknown `assetId` or `scriptId`" both fail with `invalid_argument`. The implementation only validates "missing" (blank/omitted) client-side and raises `ValueError` -> `invalid_argument`; an "unknown" (well-formed but non-existent) id would instead surface through `SuperOpsError.to_envelope()` via `classify_status`, which maps a SuperOps "not found" style error to `not_found` (see `api_client.py:67-73`, precedent `test_technician_get_missing_is_not_found` for exactly this pattern on an existing tool). `TC5` only exercises the blank-id half; the unknown-id half isn't exercised anywhere since no mock covers a SuperOps-side rejection for this tool, and no real vendor account is available this round. Net effect is the same either way — no script is dispatched — only the error *code* differs from the AC's literal text. Not blocking: this matches the established codebase convention (blank input = `invalid_argument`, nonexistent record = `not_found`) used by every other write tool in this repo, so flagging rather than demanding a special case that would be inconsistent with the rest of the connector. Worth a one-line note to BA at INT acceptance in case the "unknown id" half of AC4/AC7 is checked literally against the error code.

No other findings. Scope, reuse, credential handling, error envelope, annotations, naming and README table are all consistent with the existing four domains and the technical design's change list.

### Verification performed

- `git diff origin/main...HEAD -- src/ README.md tests/` reviewed in full (8 files, 844 insertions / 6 deletions) — matches the technical design's change list exactly (`automation.py` new, two one-line edits in `navigation.py`/`server.py`, README table append, `tests/test_tools.py` + `tests/test_api_client.py` extended). No file outside this list touched; no other ticket's commits on the branch (`git log --oneline origin/main..HEAD`, 6 commits, all `Dev01.MBAgent`, all PRD-21205).
- Re-ran the repo's own gate on the exact reviewed commit: `uv run pytest` -> `455 passed, 1 warning` (matches the Self-test section's "final" count; no `collection error`, no skipped test). Baseline (402, per Self-test) not independently re-run this round — trusted from the dev leg's own baseline/final pair plus `tdd.py diff` exit 0 already recorded.
- Traced `superops_automation_scripts_run`'s and `superops_automation_alerts_resolve`'s error paths by hand against `_common.py:run_tool` and `api_client.py:classify_status` to confirm `ValueError` -> `invalid_argument` (AC4) and the documented `resolveAlerts: Boolean` deviation -> `upstream_error` on a falsy confirmation (both match the test assertions and the Self-test's "Deviations" section).
- `grep -rnP '[\x{4e00}-\x{9fff}]' --exclude-dir=.git --exclude-dir=.venv .` on the reviewed commit — no output (no Chinese text in the repo).
- README's `## MSPbots Connector` section diffed against `~/.claude/skills/vendor-mcp-development/reference/readme-connector-section.md` — unchanged by this ticket, still verbatim.
- `gitleaks` is not installed in this environment; skipped (no credential-shaped strings observed while reading the diff by hand — no tokens, no hardcoded headers).
- Credentials/multi-tenant isolation (AC7): the new tools go through the same `client_factory` / `GatewayCredentialsMiddleware` contextvars path as every existing tool (`_common.py:93-146`), introduce no new state, and no new cross-request cache — confirmed by reading the code, not by re-running `test_middleware.py` against a live two-tenant scenario (that part of TC8 is correctly recorded as untested, needs a real vendor account, not a blocker per `references/mcp-track.md` §3).

### Not re-run this round

- TC8's two-tenant half (needs a real SuperOps account) — left as recorded (`untested`, with reason).
