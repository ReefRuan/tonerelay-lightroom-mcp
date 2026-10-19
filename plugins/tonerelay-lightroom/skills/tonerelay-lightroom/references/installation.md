# Installation and repair

Use only for an authorized setup or repair task. Select the needed steps from the
observed blocker; an existing working installation does not need a reset.
For skill-only installation, go directly to **Deliver the startup skill to each
client** below; do not rerun Runtime or Lightroom installation checkpoints.

## Source and platform

Use the public
[source installation help](https://github.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/README.md#source-installation)
and this Plugin's platform entry: `scripts/tonerelay_bootstrap_macos.py` on macOS,
`scripts/tonerelay_bootstrap.py` on Windows (paths relative to the Plugin root).
Install an explicit verified source tree with an immutable label. Verify canonical
source identity and integrity; the help page is guidance, not an installation
artifact.

A standalone client skill copy does not include the Plugin's bootstrap scripts.
Use the known installed control entry, or locate those scripts in the verified
installation source; do not assume they exist beneath the client's skill folder.

Choose one dedicated installation root before downloading or cloning, and tell
the user the chosen path and fixed source version without adding a separate
approval for a default path. Identify Lightroom only through OS application
registration, standard installation locations or running processes. A valid live
plugin response needs no application-location search. If Lightroom needs opening
but its location is unknown, or it is confirmed not running, ask in the current client:
"Please open Lightroom Classic and the Catalog you want to use, then reply
'opened'." Do not expand into a disk search. Downloads, dependencies and client
setup can continue while waiting; resume live verification after the reply.
A probe timeout alone does not establish that Lightroom is closed.

New macOS
installations use root/config.json and dedicated data/cache/tmp/logs/backups/downloads/run
paths. Keep the download in downloads/source, then use prepare --runtime-root <root>
and install --runtime-root <root> --source-root <root>/downloads/source --version <fixed-label>.
The launchers project configuration into the MCP/Bridge and child environments;
inherited cache/data/temp settings cannot override it. Do not migrate old configuration
implicitly. Existing grouped installations retain their offline maintenance paths.
Add --manage-source only for a download created for this installation, never for an
existing development checkout. Preserve existing data; path changes do not move it.
Record the already-verified origin and digest/commit with `--source-origin` and
`--source-integrity`; these fields do not perform remote verification themselves.

Follow the returned manifest and record each client integration using
`record-client --client <name> --config-path <actual-config>` (JSON `mcpServers`
pointing at the exact launcher). For an official Plugin/UI-managed integration,
use `--client-kind official` and its actual configuration location; remove it
through that client's official interface. Record source identity/integrity,
external registrations, checkpoints and backup locations as work completes.
Older releases without these commands need an explicit path inventory; do not
invent support or infer ownership from a directory name.

## Completion checkpoints

For a full installation, continue through these checkpoints; for repair, repeat
only the affected checkpoints and downstream verification.

1. Read interpreter/dependency and Runtime status. MCP includes lr-ops core;
   default `bridge.mode=external` leaves startup/recovery to the known installed
   control entry (`bridge start` / `bridge status`). No separate lr-ops install
   or new token is required. Preserve credentials. Explicit `builtin` preflights
   in the background during MCP startup; actual requests still require verified
   Bridge identity. Lightroom starts polling only, not the Python service.
   Never manage remote processes. On Mac verify source hash and selected data root.
2. Register the Lightroom entry (on Mac, the stable owned Modules link).
   Verify the actual loaded build after a safe normal quit/reopen when needed.
3. Merge only ToneRelay into the intended client's official configuration,
   preserving other servers, and deliver this skill using the client instructions
   below. Complete login and tool trust through official UI. Codex Plugin, Kimi
   Work personal plugins, Kimi Code CLI MCP and WorkBuddy MCP are separate entry
   points; configuring one does not connect another. MCP registration alone does
   not install a skill.
4. Run the temporary stdio check and read health. If binding is required, show
   the actual Catalog name and mode and ask the user in the current client to
   reply "bind" (Chinese: "绑定") to bind that target. Wait for the reply unless
   that same target and mode already have explicit authorization. Re-probe to
   ensure the target and mode have not changed, confirm through the Runtime
   Operator rules and report authorization readback. Complete this
   before dispatching read-only acceptance; installation does not authorize edits.
5. Reconnect through the real client. In a new session verify tool discovery,
   skill discovery, Runtime instructions, configuration, health and current selection readback.
   Keep that acceptance read-only; report binding conflicts as blocked. Verify actual
   launcher/interpreter, working directory and data/cache/temp paths in the real
   session; unobserved paths remain pending.

## Deliver the startup skill to each client

Use the same `tonerelay-lightroom/` skill directory from this Plugin's `skills/`
directory, including `SKILL.md`, `references/` and `agents/`. Copy the whole skill,
not only its entry file. Do not copy the Runtime operator manual into the skill.
Use the already-verified installation source and record the client destination
and source version separately from the MCP entry. Preserve a locally modified
skill; do not overwrite it silently. Plugin-managed copies must be updated
through the client's official install/update flow, not edited in place.

### Codex

The bundled `.codex-plugin/plugin.json` already declares `"skills": "./skills/"`.
Installing the complete Plugin through the official Plugins interface therefore
delivers this skill with the MCP configuration. Verify that the MCP entry suits
the platform: this repository's bundled `.mcp.json` uses the macOS launcher.
Do not install that launcher entry unchanged on Windows.

For an existing direct MCP installation, keep its working entry and install only
the shared skill directory at `~/.agents/skills/tonerelay-lightroom/` (user scope),
or `<project>/.agents/skills/tonerelay-lightroom/` (project scope). Avoid installing
the same named skill through both the Plugin and a standalone directory. Verify
discovery in the actual client; refresh/restart if its skill list has not updated.

For an existing direct MCP connection, the verified Plugin source also provides
a client-only updater (Python 3.11+):

```text
python scripts/tonerelay_codex_skill.py status
python scripts/tonerelay_codex_skill.py install
```

Run these paths from the Plugin root. `install` installs or updates the complete
skill at the user-scope location above; repeat calls with unchanged files do no
work. Updates back up the previous skill under `~/.codex/backups/tonerelay-lightroom/`
and refuse to replace local modifications. The updater leaves MCP registration,
Runtime and Lightroom untouched. An enabled ToneRelay Plugin requires the official
Plugins update flow instead. The skill becomes available on the next turn; if
discovery remains stale, use the client's official refresh/restart and verify it.

Official references: [Codex skills](https://learn.chatgpt.com/docs/build-skills)
and [plugin packaging](https://developers.openai.com/plugins/build/plugins).

### Kimi

Include the same `skills/tonerelay-lightroom/` directory in the personal Plugin
source, beside `kimi.plugin.json`. Merge this field into the existing manifest,
preserving its real `mcpServers`, identity and other metadata:

```json
{
  "skills": "./skills/"
}
```

This is a manifest fragment, not a replacement manifest. `skillInstructions`
is optional text appended when a plugin skill loads; filling it alone does not
provide `SKILL.md`. Default to discovery by the skill's name and description.
Only if every-session loading is wanted, additionally set
`"sessionStart": {"skill": "tonerelay-lightroom"}` after confirming support in
the actual Kimi product/version. This injects instructions, not an automatic
health check or permission to edit.

Reinstall/update the personal Plugin through the actual client's official flow,
then reload or start a new session. Kimi Code runs a managed copy: editing the
source directory alone does not update an installed copy. Verify both the loaded
skill and the enabled MCP server; a visible Plugin card is insufficient. Do not
assume Kimi Work and Kimi Code share installation state.

Official reference: [Kimi plugins](https://www.kimi.com/code/docs/en/kimi-code-cli/customization/plugins).

### WorkBuddy

Use the official local skill-package import: **专家·技能·连接器 → 技能 → 添加技能
→ 上传技能**. Package only the shared `tonerelay-lightroom/` skill directory,
including its relative references, using the current importer's archive layout.
Do not upload the whole Runtime, source checkout or Codex Plugin as a skill.
This is local skill import, not marketplace publication. In **已安装**, verify
the imported skill is enabled, then invoke it in a new conversation.

Configure the local ToneRelay MCP separately through WorkBuddy's official MCP
entry, preserving the existing working server. Skill import does not install
Lightroom, Runtime or its MCP connection. Do not infer an automatic skill scan
directory from another client's `.agents/skills` convention.

Official references: [WorkBuddy skill import](https://www.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/Skills-Market)
and [skill package structure](https://open.workbuddy.cn/docs/skill).

### Other clients

Use their official skill import or skill directory in addition to registering
MCP. Retain the shared skill's name and relative references. Do not guess a
client's manifest fields or treat a generic MCP JSON entry as skill delivery.
If skill loading is unsupported, report MCP connected / skill unsupported
separately; continue supported work with the Runtime's actual instructions.

### Verify the two deliveries

Check the client's actual skill list/loader for `tonerelay-lightroom` and its
installed source/path, then invoke it explicitly once without editing photos.
Separately verify tool discovery and the authorized read-only Runtime checks.
Record skill discovery and MCP connectivity as separate results. A copied file,
an independent stdio handshake or the model merely saying it knows the skill
does not prove both deliveries. The bootstrap scripts currently return MCP
configuration; these client skill steps are performed by the installation
operator, not automatically by those scripts.

Local image references are enabled by default on every platform. Windows and
macOS installers return an enabled `mcp_server` environment for all same-host
clients; `--client` is only a recipient label. Merge the generated entry into the
actual client configuration and reconnect MCP. Set
`LR_XMP_MIMIC_CLIENT_LOCAL_IMAGES=0` explicitly when the client cannot access the
Server filesystem; launchers preserve an explicit disable. The same tool call
caches its final native image bytes and supplies the real reference. Native
image blocks remain available for every client. Verify the actual client-started
process, the reference, and actual reply-body rendering; tool-card images do not
prove that the assistant body renders images.

Downloads, configuration writes, bootstrap readiness and a diagnostic handshake
are separate checkpoints. An independent subprocess does not connect the current
conversation; a new chat alone does not restart the Server or fix the Bridge.
If approval, login, unknown jobs or unsafe restart blocks progress, report the
completed checkpoints and exact pending action. Never write approval records or
bypass a refusal.

The supervised `python -m workbench_mcp` entrypoint supports explicit
`trlr_refresh_mcp` inspect/refresh. It validates the active worker before
replacement while preserving stdio and the shared Bridge. New code/schema and
Runtime rules are returned; list-change notifications request client rediscovery
but do not prove the client cache updated. First adoption from an older server,
missing tool, disconnected client or ignored notifications still needs official
client reconnect. Do not kill client processes to emulate this feature.

For a cold start, establish who starts the Bridge before declaring the photo
connection ready. The existing `bridge start` maintenance entry can do this for
default external clients. Opening Lightroom or another external client cannot.
If the tools are already loaded, a later Bridge/Lightroom start only needs a new
read-only health check, not a client restart. A detached builtin-started Bridge
outlives that MCP; if it later exits with only external clients remaining, use the
maintenance entry again. A shared managed configuration cannot assign different
client modes through environment overrides. Initial tool discovery is independent
of Bridge startup and Catalog authorization; each needs separate verification.

## Process ownership and data

Upgrade by installing the verified new software and updating ToneRelay's launcher,
plugin copy and client entry. Retain current valid configuration and data in their
registered locations. Fresh installations use the new defaults without reading
or merging legacy scattered configuration. Do not expand routine upgrades into
compatibility or migration projects. Handle only old ToneRelay processes or
registrations that actually block the new version, through their proven owner.
Report the fixed source label and actual loaded plugin version/build separately.

- Follow returned paths and exact commands. Replace/remove a Mac Modules entry
  only after proving it belongs to the selected Runtime root. For a loaded old
  plugin, check registration, save work and normally reopen Lightroom; do not
  force quit or add duplicate registrations.
- The Bridge belongs to its script/operator; MCP belongs to the client.
  Before stopping an old Bridge, verify process identity and outstanding jobs.
  Mac refuses unknown/legacy PID termination. Windows termination must go
  through its original process owner after the same identity/job investigation.
- On Mac preserve the selected data root and saved choice across upgrade/repair;
  uninstall retains data by default. Use a dedicated, demonstrably ToneRelay-owned
  install root. An ownership marker alone does not prove pre-existing contents
  belong to ToneRelay; inspect before any removal.
- Windows retains its original root/data and integer-PID behavior.
  `--version all` deletes data: do not use it as a reset. Confirm the actual
  environment and back up existing data before upgrading. Refer Windows script
  failures to its maintainer; this operating skill does not authorize script edits
  or imply Mac protections on Windows.

## Failure evidence

Report the first confirmed blocker from bounded status/diagnostics. An unanswered
plugin probe means loading and authorization are unknown, not that registration
is obsolete. Check Lightroom availability, lock state and outstanding jobs before
retrying; repeat only after a relevant state change or new diagnostic evidence.
Do not replay a Lightroom write whose outcome is unknown.

Windows status/tools-list checks do not establish Mac source-hash or strict
handshake guarantees. Verify the actual loaded build and real client session on
each platform, or report the missing layer as unverified.

New installations default to full. Preserve an existing safe choice; an already
authorized current Catalog and full mode can be synchronized with trlr_confirm_catalog
without repeatedly changing Plugin Manager. Read back actual/expected/authorized.
After installation, offer help backing up the Catalog once as an optional follow-up.
Use Lightroom backup or an authorized copy after normal quit, including .lrcat-data
when present. Catalog backups do not include original photos and are retained user data.
