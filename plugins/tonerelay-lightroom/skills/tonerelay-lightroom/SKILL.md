---
name: tonerelay-lightroom
description: Use when the user asks to start Lightroom or LR retouching, edit the current Lightroom photo, view ToneRelay configuration or tutorials, install ToneRelay, or repair a ToneRelay Lightroom connection.
---

# ToneRelay Lightroom

This Skill is only a routing layer. It does not define Lightroom editing, health
checks, Catalog handling, selection capture, or MCP tool order.

- If ToneRelay MCP tools are available, follow the server's Runtime Operator
  instructions exactly. Do not duplicate or override them here.
- A read-only configuration/tutorial or verification request returns its result
  in the conversation. Do not create report files or edit skills, memory,
  installation instructions, or client configuration unless the user requests
  that work. Report stale skill claims against the actual tool schema instead
  of changing the skill during the query.
- For installation or moving to another machine, use the independent public
  [source installation help](https://github.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/README.md#source-installation)
  and this Plugin's platform entry: on macOS use
  `scripts/tonerelay_bootstrap_macos.py`; on Windows use the original owner
  `scripts/tonerelay_bootstrap.py`. The Mac CLI rejects other hosts. Install an explicit verified
  source tree with an immutable label. Source identity and integrity checks are
  required; the help document is not itself an installation artifact.
- Complete the authorized installation chain: read interpreter/dependency and
  Runtime status, explicitly check/start the Bridge (on Mac verify its source hash),
  register the Lightroom entry (on Mac the stable owned Modules link), and verify its actual loaded build after
  normal quit/reopen when safe. Merge only ToneRelay into the target client's
  official configuration, run the temporary stdio check, then use a real new
  client session to call configuration/tutorials, health and Catalog discovery.
  Explicit user confirmation precedes Catalog binding; read back authorized health.
  Download, configuration writes, bootstrap ready and diagnostic handshake are
  individual checkpoints. Continue until the new client session is verified.
- For an unavailable MCP or startup failure, report the first confirmed blocker
  from a bounded bootstrap `status`. A read-only configuration/tutorial query is
  not installation authorization. Do not turn it into repeated direct-client scripts.
  An unanswered plugin probe means unknown loading; inspect Lightroom availability
  and outstanding jobs before retrying. It does not prove an obsolete registration.
- On macOS follow returned paths and exact commands. Modules entries must belong to this
  root before replacement/removal. A loaded old build requires checking the actual
  registration, saving work and normally restarting Lightroom; no force quit or
  duplicate registration. A running old Bridge requires job investigation and
  stopping through its owner. The Mac entry refuses unknown/legacy PID identity termination. Windows stops must be handled by the original process owner after verifying identity and jobs.
- On macOS preserve the selected data root and its saved choice across upgrade/repair;
  Mac uninstall retains data by default. Windows keeps the original root/data and
  integer-PID implementation; its `--version all` removes data, so do not run that
  action as a Mac-style reset. Back up existing Windows data and confirm the actual
  environment before upgrading. Delegate Windows script failures to its owner;
  do not modify that script or claim the Mac protections apply to it.
- Complete client trust through official UI. If login, tool approval, unknown jobs
  or an unsafe restart blocks work, retain completed checkpoints and report the
  exact pending action. Never write approval records or bypass a refusal.
- The Bridge belongs to its script/operator; the real MCP process belongs to the
  client. An independent diagnostic subprocess does not connect this conversation.
  Never replay a Lightroom write with an unknown outcome. Record Windows host
  acceptance separately. The original Windows status and tools-list checks do not
  have the Mac source-hash, unknown-probe and strict handshake protections; verify
  the actual loaded build and new client session, or report them unverified.

## Repeated-install acceptance (Kimi Work and WorkBuddy)

This reset loop applies to macOS. Windows reset steps remain with its owner and
must preserve user data without running the original whole-root uninstall.
Run this reset loop before **each** Kimi Work or WorkBuddy install/use
acceptance session when the user asks to test installation from scratch. It is
not part of ordinary use and must not remove another user's configuration.

1. Record the client version, installed ToneRelay state, Lightroom Plug-in
   Manager entries, current Runtime build, and start time. Back up the client
   MCP/plugin configuration and Lightroom plugin state to the run's local,
   owner-only evidence directory. Redact secrets, personal paths, and photo
   content from summaries.
2. Uninstall only ToneRelay from the target client. For Kimi Work, use the
   Plugin Center's Personal plugin uninstall action. For WorkBuddy, remove only
   the ToneRelay key from the official user-level MCP configuration, merging
   the JSON and preserving every other server. Never substitute Kimi Code's
   MCP configuration for Kimi Work.
3. Remove the confirmed stale ToneRelay registration(s). On macOS, for a Modules entry owned
   by the current Runtime, use `lightroom-plugin uninstall`; for a manually added
   entry, use Plug-in Manager, leaving Adobe and unrelated plugins alone. Verify that the
   ToneRelay row is gone. If the entry cannot be safely removed through its owner, record that exact
   state and stop this reset as incomplete; do not edit Lightroom's private preference files.
4. Quit and reopen Lightroom and the target client normally. Verify the
   uninstall persisted after restart. Start a new Kimi Work/WorkBuddy session.
   If any ToneRelay client entry or plugin remains connected, do not call the
   next run a clean-install test.
5. Open the local Portal page, copy its current installation prompt using the
   page's copy control, and paste that prompt into the fresh client session.
   Keep the prompt text and resulting action log in the run directory; do not
   hand-compose replacement installation commands. Record the elapsed time,
   every manual/UI step, errors, and whether the client actually exposes the
   expected ToneRelay tools.
6. Classify each finding as: resolved by prompt wording, requires a Runtime
   Plugin/bootstrap/dependency change, client integration limitation, or
   environment/permission block. Dependency failures belong to the Runtime
   installer/package source when the pinned installation does not install a
   declared requirement in its own environment; do not compensate by changing
   system Python or running an unrelated installer.

## Post-install photo acceptance

- Do not begin photo work until the new client session exposes ToneRelay and
  read-only health, active Catalog, and selection checks succeed. Keep the
  plugin's actual `safe` mode and registered sandbox unchanged.
- For an authorized color-match trial, choose a master source photo and an
  authorized reference photo from the disposable test Catalog. Create the
  Episode from the master source; pass the reference separately according to
  Runtime Operator instructions. Make the smallest useful color adjustment on
  the Episode's virtual copy, then read back the exact settings and render the
  controlled artifact. Compare that artifact with the reference. Never edit
  either original photo.
- Report color matching separately from same-photo parameter variants and
  cross-photo batch work. A set of variants on one Episode is not multi-photo
  batch support. After every unknown write outcome, query Episode state and the
  Lightroom issue log before considering another write.
- Record Lightroom module/selection changes and verify the original source
  settings remain unchanged. A fast preview is not edit verification, and a
  JPEG-to-PNG conversion does not restore lossless source pixels.
