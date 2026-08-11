# Live E2E recovery design

## Problem

The runtime already exposes a bounded semantic `click_ref` browser action, but
the live public-browser acceptance probe rejects it before dispatch. This makes
the public acceptance path less capable than the product runtime and forces a
human to perform ordinary read-only navigation.

The current-desktop diagnosis has a separate observability defect: it passes the
repository root to the read-only filesystem tools and assumes a literal
`deskorb-agent-debug.log` filename. The agent's configured working directory and
the opt-in `DESKORB_AGENT_DEBUG_LOG` setting are the actual sources of truth.

## Decision

Use bounded public browsing for live acceptance:

- Allow `navigate`, `snapshot`, `click_ref`, `switch_tab`, `extract`, `verify`, and `wait`.
- A click is allowed only after the browser is known to be on an approved HTTP(S)
  domain for the selected scenario. A batch may establish that origin with an
  approved `navigate` before clicking.
- For scenarios that require pre-click evidence, the guard also requires the
  minimum number of trusted structured candidates before dispatching a click;
  evidence and the click must therefore be separate tool rounds.
- A tab switch is treated as a context change: the selected tab must be observed
  with a fresh snapshot before another click or evidence extraction can proceed.
- Keep `fill_ref`, `submit`, `download`, `upload`, arbitrary tools, account,
  payment, checkout, authentication, and CAPTCHA routes blocked.
- Refresh the observed page URL from the adapter-owned `Page URL` snapshot
  metadata and fail closed if a click lands outside the scenario domain allowlist.
- Prompts may direct the agent to click public result links, but must not ask it
  to bypass human verification or perform irreversible actions.

For current-desktop diagnosis:

- Pass `config.WORKING_DIR` to the read-only runtime.
- List the configured directory before reading a log; use the configured
  `DESKORB_AGENT_DEBUG_LOG` path only when it resolves inside that directory.
- Never invent a log filename or enable debug logging as a side effect.
- Report an unavailable foreground window or missing log as environment evidence,
  not as a successful healthy diagnosis.
- Expose `--working-dir` and an optional scoped `--log-path` so the operator can
  point the diagnostic at the actual readable DeskOrb directory without
  widening the read scope. Bound the provider turn and report a timeout instead
  of hanging the diagnostic process.

## Acceptance criteria

1. Unit tests prove approved-domain clicks are allowed and out-of-scope clicks
   are rejected before dispatch.
2. Unit tests prove Bing's live prompt permits public result clicks and does not
   permit form submission or login navigation.
3. Current-desktop tests prove the configured working directory and safe log
   discovery instructions are used.
4. The focused tests, full unittest suite, deterministic E2E matrix, and local
   browser probe pass.
5. A real Bing run can click an approved public result or reports a truthful
   external block; Taobao CAPTCHA remains a manual handoff and is never bypassed.
6. Live timeout reports distinguish provider timeout before tools from timeout
   after tools, and preserve normalized tool-phase metrics on early exit.

## Risks and controls

Page content and model output remain untrusted. The code-level action allowlist,
domain checks, approval lease, CAPTCHA handoff, and structured evidence checks
remain authoritative. A click can still trigger a page navigation, so the next
adapter snapshot must establish the new URL before evidence is accepted.
