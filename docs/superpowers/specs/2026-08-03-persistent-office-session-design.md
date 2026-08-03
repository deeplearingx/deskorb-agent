# Persistent Office Attachment and Edit History Design

## Goal

Keep one explicitly read Word or Excel attachment available throughout the
current DeskOrb session until the user clicks **Clear**, while keeping the
Office content and edit history in memory and injecting them only into the
individual request that needs them. A later request such as “撤回刚才的修改”
must be able to use the current document state and the session's successful edit
history to produce a new, reviewable inverse plan.

## Confirmed scope

- One active Office attachment at a time; reading another document replaces the
  previous attachment and clears its edit history.
- The attachment survives normal messages and successful Apply operations.
- **Clear**, conversation reset, application shutdown, a read failure, or a
  changed document identity clears the attachment, pending plan, and history.
- Office text, structural targets, pending plans, and edit history remain
  memory-only. They are not written to normal transcript text,
  `ConversationContext`, disk, or logs.
- Word body/table text and Excel values/formulas remain the only editable scope.
- Every write still requires a local preview and an explicit **Apply changes**.
- The document is never saved automatically.

## Alternatives

1. **Session memory with per-request injection (selected).** Retains an
   immutable snapshot and a bounded edit ledger locally, then sends the current
   snapshot plus history only for Office-related requests. It supports undo and
   avoids persisting document text in ordinary conversation context. It repeats
   the document input and therefore uses more tokens.
2. **Put the full document into normal conversation memory.** Cheaper on later
   requests, but makes Clear and stale-document handling ambiguous and leaks the
   document into the provider's normal history. Rejected.
3. **Persist a local document cache/index.** Scales to large documents, but adds
   disk encryption, cache invalidation, and retention obligations not needed for
   the current bounded Office feature. Deferred.

## State model

The overlay owns one `PersistentOfficeAttachment` in memory:

- `snapshot`: the latest `OfficeSnapshot` and its identity/fingerprint;
- `source_hwnd`: the root Office window captured at read time;
- `history`: ordered successful `OfficeEditRecord` values;
- `generation`: monotonically increasing value used to ignore late reads,
  plans, and Apply results;
- `pending_plan`: at most one preview awaiting Apply or Discard.

An `OfficeEditRecord` contains the operation id, timestamp, document identity,
Office kind, and typed before/after target values. It records only a confirmed
successful Apply. A failed or partial COM write does not become a normal undo
record; the UI requires a fresh read and tells the user to use Office Undo if
needed.

After a successful Apply, the live document is reread and replaces the stored
snapshot. The history record is appended, while the attachment remains active.
The post-Apply snapshot fingerprint is expected to differ from the preview
fingerprint.

## Request flow

1. The user explicitly clicks **Read current Word** or **Read current Excel**.
2. The worker reads a bounded COM snapshot in the background and installs it as
   the persistent attachment.
3. For each subsequent message while the attachment exists, the UI classifies
   the request as Office-aware and sends a temporary prompt containing:
   - the current bounded snapshot;
   - concise edit history records (locators and before/after values);
   - the user's new request;
   - the strict JSON plan schema and the instruction that document content is
     untrusted data.
4. The worker uses a no-tools ephemeral request. The provider receives the
   Office payload for that request, and neither the Office payload nor the
   question/answer pair is added to the normal conversation context.
5. A valid plan is shown in the local preview card. Apply revalidates the live
   source, writes, verifies, rereads the snapshot, and appends a history record.
6. A request such as “撤回上一次修改” can reference the latest history record;
   the UI builds an inverse plan, and the local preflight requires each
   current target value to equal the record's after-value before Apply.

Normal messages may still use screenshots when Auto-shot is enabled, except an
Office-aware request, which sends no screenshot so text-only Office models remain
compatible.

## Clear and invalidation

- **Clear** removes the persistent attachment, all Office history, pending plan,
  and raw planning buffer.
- Reading a different Office source replaces all state atomically after the new
  snapshot succeeds; a failed replacement leaves no previous attachment.
- If a background event's generation is stale, it is ignored and cannot restore
  an attachment or plan after Clear.
- Before Apply, changed window/document identity, changed target values, or a
  protected/read-only source rejects the operation and clears the pending plan.
- After successful Apply, the attachment remains active with the refreshed
  snapshot and history.

## Privacy and limits

- Office text and history never appear in `add_user`, normal assistant display,
  copy controls, debug logs, or normal context builders.
- The provider can see the Office payload on each Office-aware request; this is
  an explicit consequence of persistent in-session attachment and must be
  documented to the user.
- Existing Office cell and rendered-character limits remain enforced.
- Clear is the user-visible retention boundary; closing the application also
  drops the in-memory state.

## Tests

Add focused unit coverage for:

1. Attachment surviving two or more messages and successful Apply.
2. A second read replacing the previous attachment and history.
3. Clear/reset and late-result generation invalidation.
4. A successful edit producing a record and a later inverse plan using its
   before/after values.
5. Stale current values rejecting an inverse Apply before any write.
6. Office payload absent from normal context and visible transcript while the
   same payload is present in the isolated request.
7. Word/Excel existing read, plan, Apply, no-save, and text-only backend tests.

## Acceptance criteria

- After one explicit Word read, the user can ask multiple follow-up questions
  about that document without clicking Read again.
- After an Apply, “撤回刚才的修改” produces a preview based on the recorded
  operation and can safely restore the previous text/value after confirmation.
- Clicking Clear makes subsequent messages unable to use the old Office content
  or edit history.
- Office content remains outside ordinary transcript and context memory.
- No Apply operation saves or closes the Office document.
