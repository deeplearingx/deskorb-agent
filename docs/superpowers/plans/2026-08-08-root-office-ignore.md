# Root Office Output Ignore Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ignore the project-local Volcengine template, root-generated Word/PowerPoint files, and Office lock files without hiding OfficeCLI fixtures.

**Architecture:** Add root-anchored patterns after the existing environment-file exceptions in `.gitignore`. Validate both positive matches at the repository root and a negative nested-path case below `OfficeCLI-main`; do not delete files or remove tracked files from the index.

**Tech Stack:** Git ignore patterns, PowerShell, `git check-ignore`

---

## File structure

- Modify `.gitignore`: add root-only local-output rules.
- No test source files are needed; Git's own ignore matcher is the behavioral test.

### Task 1: Ignore root-local environment and Office outputs

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Verify the target paths are not currently ignored**

Run:

```powershell
$targets = @(
  'volcengine.env.example',
  '战锤40k介绍.docx',
  '战锤40k基因原体介绍.pptx',
  '~$draft.docx',
  '~$draft.pptx'
)
foreach ($target in $targets) {
  git check-ignore --no-index -q -- $target
  if ($LASTEXITCODE -eq 0) { throw "Target is already ignored: $target" }
}
```

Expected: exit code `0` from the PowerShell script because each nested
`git check-ignore` call returns non-zero before the new rules exist.

- [ ] **Step 2: Add the root-anchored ignore rules**

Append this block after the existing environment-file rules in `.gitignore`:

```gitignore

# Project-local provider template and generated Office outputs
/volcengine.env.example
/*.doc
/*.docx
/*.ppt
/*.pptx
/~$*.doc*
/~$*.ppt*
```

- [ ] **Step 3: Verify every intended root path matches**

Run:

```powershell
$targets = @(
  'volcengine.env.example',
  '战锤40k介绍.docx',
  '战锤40k基因原体介绍.pptx',
  '~$draft.docx',
  '~$draft.pptx'
)
foreach ($target in $targets) {
  git check-ignore --no-index -q -- $target
  if ($LASTEXITCODE -ne 0) { throw "Target is not ignored: $target" }
}
```

Expected: exit code `0`; every target is ignored.

- [ ] **Step 4: Verify OfficeCLI fixtures remain eligible for tracking**

Run:

```powershell
git check-ignore --no-index -q -- 'OfficeCLI-main/examples/future-fixture.docx'
if ($LASTEXITCODE -eq 0) { throw 'Nested OfficeCLI DOCX was unexpectedly ignored.' }
git check-ignore --no-index -q -- 'OfficeCLI-main/examples/future-fixture.pptx'
if ($LASTEXITCODE -eq 0) { throw 'Nested OfficeCLI PPTX was unexpectedly ignored.' }
git ls-files --error-unmatch -- 'officecli-test.docx'
```

Expected: the two nested hypothetical fixture paths are not ignored, and
`git ls-files` prints `officecli-test.docx`.

- [ ] **Step 5: Verify repository status and whitespace**

Run:

```powershell
git diff --check -- .gitignore
git status --short -- .gitignore volcengine.env.example '*.doc' '*.docx' '*.ppt' '*.pptx'
```

Expected: `git diff --check` succeeds; `.gitignore` remains modified, while
the untracked root Volcengine template and generated Word/PPT files no longer
appear. Existing tracked Office files remain tracked.

- [ ] **Step 6: Preserve the user's existing `.gitignore` changes**

Do not stage or commit `.gitignore` in this task. It already contains unrelated
uncommitted user changes, so leaving the completed edit unstaged avoids bundling
those changes into an unintended commit.
