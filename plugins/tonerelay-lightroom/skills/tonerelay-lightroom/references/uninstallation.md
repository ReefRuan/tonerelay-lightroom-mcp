# Uninstallation

Use the installed version's supported entry and installation manifest. Full
software removal includes client integration, every owned MCP session, the
Bridge, Lightroom registration, and managed downloads/files. Client-only removal
stops at that client's integration. Preserve unrelated configuration, photos,
Catalogs, presets, personal data and recoverable backups.

Inspect `inventory` or `uninstall` (plan only) once. Identify all clients using
the same root. If full removal affects clients outside the authorized scope,
describe the impact and ask whether to continue; proceed when that same scope
is already authorized. A legacy install without a manifest needs a bounded
inventory of its known locations; a marker alone does not own all contents.

## Three steps

1. Remove authorized client integrations first to prevent respawn. Back up each
   affected configuration and preserve other servers. For JSON entries the Mac
   orchestrator removes only the recorded, unchanged key. Use the official
   removal API/UI for other integrations, then record the observed result with
   `record-client --client <name> --config-path <location> --client-kind official
   --removal-evidence <observed-result>`. This is operator evidence, not an
   automatic inspection of the client's loaded tools.
   Check outstanding jobs before stopping services. MCP may have multiple
   client/session instances. Match creation identity and installation ownership,
   request termination, wait within a fixed budget and check for respawn.
   Unknown identity goes through its original owner; do not kill by name.
2. Save work and normally quit Lightroom when requested. Remove the owned
   Modules link/registration, then reopen normally as needed. Disabling a plugin
   is not file removal; unlinking must never follow the target.
3. Remove manifest-owned files and the dedicated source download. Inspect changed
   or unrecorded leftovers individually; do not recursively delete the root.
   Report retained data, manifest and recovery backups separately from software.

## Mac command and completion

After reviewing all integrations and any shared-use authorization, run
`uninstall --apply --clients-reviewed`. The flag records the operator's completed
scope review; it is not permission to skip a missing authorization. The command
stops at a concrete blocker and can be resumed. It saves a standalone recovery
runner under `backups/uninstaller/`; after the installed control is removed, use
that script with an independent Python and the same `--runtime-root`.

Use `uninstall-check` for offline evidence. It reads recorded client entries,
processes, jobs, registration and files; it never calls the Bridge or queues a
Lightroom probe. Queued `probe_client` records are reported and left in retained
data; other unresolved jobs block automated stopping. A closed port alone is
not process evidence. Unknown service ownership requires investigation.

Check the real client's loaded integrations/tools through its official surface.
Reconnect or create a new session only when refresh cannot otherwise be
confirmed. Do not mistake marketplace search, a model's self-report, or a
configuration file alone for a loaded-tool check. Stop once evidence is enough;
state any unobservable layer and the exact remaining action.

Do not reinstall unless requested. For an explicit clean reinstall, record the
removed baseline, install from the authorized fixed source, then verify the new
connection in a fresh client session. Windows uses its original owner's entry;
its whole-root uninstall deletes data, so use individually verified removal and
backups instead of assuming Mac ledger or process protections exist.
