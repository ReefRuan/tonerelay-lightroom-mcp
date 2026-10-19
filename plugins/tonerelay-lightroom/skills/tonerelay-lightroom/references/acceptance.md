# Requested acceptance testing

Read the installation procedure first. Use only the acceptance scope the user
requested; preserve evidence without exposing credentials or photo content.

## Clean-install acceptance: Kimi Work and WorkBuddy on macOS

Run this reset before each session explicitly testing installation from scratch.
Windows reset remains with its platform owner; do not use its whole-root uninstall.

1. Record client version, ToneRelay state, Lightroom Plug-in Manager entries,
   Runtime build and start time. Back up client and Lightroom plugin configuration
   in the run's owner-only evidence directory; redact secrets and personal paths
   from summaries.
2. Remove only ToneRelay from the target client. Kimi Work uses Plugin Center's
   Personal plugin uninstall; do not substitute Kimi Code configuration.
   WorkBuddy uses its official user-level MCP configuration: merge JSON and
   preserve every other server.
3. Remove confirmed stale ToneRelay registrations through their owner:
   `lightroom-plugin uninstall` for a Mac Modules entry owned by this Runtime,
   Plug-in Manager for manual entries. Leave unrelated plugins intact and verify
   the row is gone. If safe removal is unavailable, report the reset incomplete;
   do not edit Lightroom's private preference files.
4. Normally quit/reopen Lightroom and the client, verify removal persisted and
   start a fresh client session. Remaining ToneRelay entries/connections mean
   the clean-install prerequisite failed.
5. For Portal-driven acceptance, copy the current installation prompt with the
   page's copy control into that session; preserve the prompt and action log,
   without replacing it with hand-written commands. For other installation
   sources, record the exact requested source instead of introducing Portal.
6. Record elapsed time, manual steps, errors and actual tool exposure. Classify
   findings as prompt, Runtime/bootstrap/dependency, client integration, or
   environment/permission issues. Missing declared dependencies belong to the
   pinned installer environment; do not patch system Python to hide the failure.

## Photo-writing acceptance

Run only when the user authorizes the trial in a disposable test Catalog.
First verify the new client exposes ToneRelay and read-only health, active Catalog
and selection succeed. Keep the actual access mode and Sandbox registration;
if the requested trial requires safe mode and it is not active, report that
prerequisite rather than changing modes.

For an authorized color-match trial, choose a master source and an authorized
reference. Follow Runtime Operator rules for Episode mode and same-capture
preconditions, edit only the Episode virtual copy, read back the exact settings
and inspect a controlled render against the reference. Verify both originals'
settings remain unchanged and record Lightroom module/selection changes.
Unknown write outcomes follow Runtime recovery before any further write.

Report color matching, same-photo parameter variants and cross-photo batch work
separately. Variants on one Episode do not prove batch support. A fast preview is
not edit verification; converting JPEG to PNG does not restore source precision.
