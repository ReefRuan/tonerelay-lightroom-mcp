# Working-copy iteration and image delivery

Base: public ToneRelay commit e78ea5e2a398d222da3c127b8858278e7d5f24f0.
This task separates routine editing from independent Episode candidates, and
model inspection from user-facing image delivery. End-to-end memory transport,
render retention/cleanup and deleting existing copies are out of scope.

## Development

1. Create only Episode v0 when there is no editable working copy, then continue
   on that UUID with trlr_apply_settings. Independent candidates remain opt-in.
2. Recover forgotten working UUIDs from relevant TRLR copies and history,
   verifying Target/Catalog and master_uuid. Names and version order alone do
   not establish identity; preserve deliberate user target changes.
3. Refuse protected copy names (保护/锁定/勿改/只读/留档 and independent English
   protected/locked/readonly/read-only/do-not-edit forms). Bind names into settings
   tokens and recheck inside the Lightroom write gate; native masks share this
   guard. Read-only inspection remains available.
4. Expose master_uuid/edit_protected in live inventories and settings reads.
   Plugins without copy_protection_v1 cannot perform in-place writes through
   the updated Runtime. Release 0.8.0.0 uses plugin build poll-lifecycle-0.8.0.0.
5. Add display=false to image-returning MCP tools. Preserve native image blocks,
   omit image-delivery files for internal iteration and batch scoring. Ordinary
   conversational viewing and final results use display=true. Deliver saved
   operation images later without another edit/render; respect the existing
   local-image environment opt-out.
6. Align initialization instructions, tool descriptions, UI fallback and evidence
   guidance. Historical images do not change after live working-copy edits.

## Tests

- Start on a master, create only v0, apply twenty corrections to its UUID:
  one new copy total and master unchanged.
- Cover protected Chinese/English names, case/word boundaries, normal renames,
  old-plugin capability, stale tokens and rename-before-write races. Refused
  writes must not change settings or leave an unknown claim.
- Exercise actual Lua apply/native-mask guards using SDK doubles. Protected
  copies may be inspected but not edited.
- Check master linkage/protection metadata survives sanitization without paths.
- Verify omitted/false display returns native image blocks with no image-delivery
  directory. Explicit true generates exact-byte hashed references and reuses
  them. Environment opt-out suppresses files.
- Reject invalid display before writes. Changing only display for an existing
  request must not repeat an edit/render; presentation failures preserve the
  known edit outcome.
- Run prompt-contract, MCP protocol, Runtime and Lua regression suites in isolated
  temporary roots. Do not submit real photo writes.

## Rollout boundary

Record exact commands, exit codes and pass/fail/skip counts. Offline tests prove
protocol, state and gate behavior, not Lightroom rendering or every client's UI.
Keep the installed Runtime/Catalog untouched during development. Deploy MCP and
the matching Lightroom plugin together, preserve data/configuration, then check
the actual plugin build and image delivery in the real client.
