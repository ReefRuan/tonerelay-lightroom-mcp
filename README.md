# ToneRelay for Lightroom

ToneRelay connects an MCP client to Lightroom Classic through a local Python Runtime and a Lua plugin. Photo edits use virtual copies and require a confirmed Catalog. The MCP process uses stdio; the Lightroom bridge is a separate lr-ops process.

## Source installation

Obtain a verified, immutable version from the canonical `ReefRuan/tonerelay-lightroom-mcp` repository. Check repository identity, visibility and artifact integrity before installing. This tree does not establish that a public release or verified installation artifact is available.

With compatible Python 3.10 or later, run from that verified source tree:

```sh
python3 plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap_macos.py install \
  --source-root "/path/to/verified-source" --version "fixed-version-label"
```

```powershell
python plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap.py install `
  --source-root "C:\path\to\verified-source" --version "fixed-version-label"
```

The first command uses the dedicated macOS entry and rejects other hosts. The
PowerShell command uses the original Windows entry maintained by its owner;
Windows implementation is unchanged here. Read `runtime_root`, `data_root`, `control`, `launcher`, `lightroom_plugin`, `mcp_server` and `verification` in the JSON result. Use the returned paths; quote paths containing spaces. macOS defaults to `~/Library/Application Support/ToneRelay`; Windows defaults to `%SystemDrive%\ToneRelay`. If that Windows root is unwritable, the diagnostic supplies a per-user path to pass with `--runtime-root`. Windows uses `.venv\Scripts\python.exe` and CMD launchers; macOS uses `.venv/bin/python` and shell launchers. Windows host, Lightroom and client acceptance must be recorded separately; POSIX layout on Linux does not establish full support.

## macOS component commands

The macOS `control` drives four components. The following source hash, process
identity, Modules ownership and strict handshake checks belong to that entry.
Windows does not gain them from using the same component names. Each accepts `install`, `uninstall`, `start`, `stop` and `status`; an inapplicable action explains the applicable command.

| Component | Useful actions | Process owner |
|---|---|---|
| `runtime` | `install`, `uninstall --version <label>` or `--version all`, `status` | Version directories |
| `bridge` | `start`, `stop`, `status` | Operator/bootstrap script |
| `lightroom-plugin` | `install`, `uninstall`, `status` | Lightroom |
| `mcp` | `start` for a temporary protocol check, `status` | Target client for the real session |

For example, use `"<control>" bridge start` in the macOS shell. Installation also attempts to start the local Bridge. A reachable port alone does not verify it: inspect `lr_ops_build_matches_active` or `matches_active_source` for the source hash check. A mismatched or unknown build requires investigating outstanding jobs and stopping the Bridge through its owner before repair. Non-loopback targets are managed on their host. Bridge stop checks recorded OS process identity and Runtime ownership; it refuses a legacy integer PID or changed identity.

Run `lightroom-plugin install` to create the user Modules link through `active`. The installer refuses to replace another install's entry or an ordinary directory. Save work and quit/reopen Lightroom normally when it is safe to do so. A Modules link is registration evidence; the live probe's expected and loaded builds establish actual loading. A probe timeout leaves loading unknown and does not justify adding duplicate plugins or rebinding a Catalog.

Merge the printed `mcp_server` into the target client's official configuration, preserving other servers. The checked-in Codex MCP entry uses a macOS shell. Run `mcp start` to check initialize and tools/list in the installed environment. This diagnostic process exits after the check. The target client starts and holds its own MCP process.

Open a new target-client session and actually call `trlr_get_runtime_config`, a configuration tutorial, `trlr_health` and `trlr_list_catalogs`. Confirm the intended active Catalog explicitly before `trlr_confirm_catalog`, then read health again. Installation is complete only when the client exposes the tools, the loaded plugin matches, and health reports `lrops: true` and `catalog_policy.authorized: true`. Bootstrap `ready`, a successful download, a written configuration or an independent handshake does not establish this client acceptance. If login, approval, unknown jobs or a required restart blocks completion, retain completed steps and report the exact next action.

## macOS data and removal

The data selection order is explicit `LR_XMP_MIMIC_RUNTIME_DATA_ROOT`, saved `data-selection.json`, existing nonempty `<runtime-root>/data`, existing nonempty `~/lr-xmp-mimic-runtime-data`, then new `<runtime-root>/data`. Install and repair save the selection. Existing data stays in place; upgrades do not move or delete Catalog bindings, Episodes or settings. Changing the explicit selection requires regenerating the client entry and reconnecting it.

On macOS, before uninstalling, stop new jobs, investigate unknown writes, wait for idle and close the target client's MCP session. Remove this installation's Modules link, restart Lightroom normally, and stop the Bridge through its owner. `runtime uninstall --version all` removes managed Runtime files while preserving selected data and its selection record. It refuses an unrecognized root or deletion that would include selected data. There is no implicit data purge.

## Windows original entry

Windows retains its original root/data, integer PID and full-root uninstall
behavior. It has no Mac data-selection record or new process identity/source
hash checks. Back up existing data and confirm the actual client environment
before upgrading. Do not run `runtime uninstall --version all` as a data-preserving
reset: it removes the root and its data. Process stops must be handled by the
original operator after checking identity and outstanding jobs.

The original Windows status and independent handshake do not prove the live build
or target client is ready. Verify the actual Lightroom build and a real new
client session, and record unknown results. Script failures and native Windows
validation remain with its Windows maintainer; do not modify that script to
apply the Mac recovery steps. No native Windows acceptance is claimed here.

## Dependency download help

Dependencies are installed in the version's venv from `apps/workbench/requirements.txt`. Inspect the pip failure and check network access to the configured package index. If your organization supplies a trusted mirror, set `PIP_INDEX_URL` for the installation process and retry from the same verified source. Keep index credentials out of reports and source files. Do not install into system Python to compensate for a failed Runtime environment.

The Mac `repair` rewrites launchers and attempts Bridge startup; it does not recreate missing dependencies or the interpreter. For an incomplete environment, install a fresh immutable label from the verified source, then verify the live Bridge, Lightroom build and new client session again.

## Safety

`full` and `safe` are Lightroom plugin access modes. `safe` additionally requires its registered Sandbox; neither bypasses Catalog identity checks. Confirmation records the plugin's current mode and identity, without changing plugin settings. Fast previews are unverified; controlled renders and readback provide execution evidence. Unknown Lightroom writes are investigated before any retry.
