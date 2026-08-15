# Task 1: Harden the complex browser acceptance evaluator

## Scope

Only edit:

- `tests/complex_browser_acceptance.py`
- `tests/test_complex_browser_acceptance.py`

Do not edit production browser/runtime files or unrelated tests.

## Required behavior

Use the existing acceptance plan and `.superpowers/sdd/plan/runner-audit-report.md` as evidence. Preserve the seven gate IDs and the official 21-run shape, but remove false-green paths:

1. Any `invalid_environment` run means the batch is not release-valid and requires rerun; `evaluate_acceptance()` must not return `ok=True` while `invalid_environment_runs > 0`.
2. Validate `case_id`, attempt is exactly 1..3, `(case_id, attempt)` is unique, `run_id` is non-empty and unique, and status/terminal/verified/completed_verified/real_site are mutually consistent.
3. Keep `waiting_human` distinct from `blocked_external`; both are safety-only and never count as business completion.
4. Make scenario completion checks consume per-record evidence, not only a global field union. At minimum reject duplicate/empty records, require the configured required fields per evidence record for #12/#17, require unique records for counts, and require exact GitHub host matching for #17. Do not require capability `unknown` to be treated as `yes`.
5. Tighten #13 final search-only validation and #17 final GitHub repository validation using exact allowed public hosts, while preserving the current metrics-only report shape.
6. Ensure the report validator rejects private values in nested evidence fields (URL query/fragment, credential-like strings, full page text) without serializing secrets.
7. Add deterministic unit tests for every new rejection and preserve existing passing behavior. Do not use live network or a fixture as a real-site pass.

## Verification

Run the focused complex acceptance tests and report changed files, test command, and results. Do not run the 21-run live suite from the implementer.
