# Root Office Output Ignore Design

## Goal

Keep local provider templates, generated Office documents, and Microsoft Office
lock files out of Git without hiding the tracked fixtures and templates below
`OfficeCLI-main`.

## Selected scope

Add root-anchored rules for:

- `/volcengine.env.example`
- `/*.doc` and `/*.docx`
- `/*.ppt` and `/*.pptx`
- `/~$*.doc*` and `/~$*.ppt*`

Root anchoring is required. Recursive `*.docx` or `*.pptx` rules would also
hide future OfficeCLI fixtures, examples, and reference templates.

The already tracked root file `officecli-test.docx` remains tracked because
`.gitignore` affects only untracked files. This change does not remove files,
delete generated output, or alter the Git index for existing tracked files.

## Validation

After editing `.gitignore`:

1. Use `git check-ignore -v` to confirm `volcengine.env.example`, the current
   root Word/PPT outputs, and representative Office lock files match the new
   rules.
2. Confirm a representative untracked path below `OfficeCLI-main` would not be
   ignored by the new root-only rules.
3. Confirm `officecli-test.docx` remains tracked.

