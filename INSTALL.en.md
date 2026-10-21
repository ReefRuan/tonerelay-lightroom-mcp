# Installation and connection

[中文](INSTALL.md) | English

Use this guide to install and maintain ToneRelay for Lightroom. See the [README](README.en.md) for examples and the [usage guide](USAGE.en.md) for everyday photo editing.

This page and the public Skill guides included in the package form the distribution's installation documentation. Development designs and internal maintenance records are intentionally not distributed; installation does not depend on them.

## Before installing

Prepare Lightroom Classic, an Agent client that supports local MCP servers, and Python 3.10 or later. Use a verified full commit SHA for the installation source. Resolve any version or tag to that fixed commit first, then give it to the Agent so it does not switch source versions during installation.

| Purpose | GitHub primary | Gitee backup |
| --- | --- | --- |
| Complete MCP source, including the installer, Lightroom plug-in, and client Plugin/Skill | [tonerelay-lightroom-mcp](https://github.com/ReefRuan/tonerelay-lightroom-mcp) | [tonerelay-lightroom-mcp](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp) |
| Complete installation prompt (bundled copy) | [INSTALL-PROMPT.en.md](https://github.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/INSTALL-PROMPT.en.md) | [INSTALL-PROMPT.en.md](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/INSTALL-PROMPT.en.md) |
| FAQ and Windows troubleshooting (bundled copy) | [FAQ.en.md](https://github.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/FAQ.en.md) | [FAQ.en.md](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp/blob/main/FAQ.en.md) |

To have an Agent perform setup, copy the [short prompt in the README](README.en.md#one-click-installation). It contains only the GitHub/Gitee MCP entry points: check existing installations read-only first; if source is needed, pin a full commit, obtain its complete snapshot under the dedicated ToneRelay installation directory, and read the local [INSTALL-PROMPT.en.md](INSTALL-PROMPT.en.md), [FAQ.en.md](FAQ.en.md), and this page completely. For a healthy compatible installation, connect only the additional client; do not reinstall or incidentally upgrade it merely to read documentation. Once this source is downloaded, installation does not depend on Portal being online. Portal maintains the website and documents but cannot replace the MCP source checkout.

The English and Chinese prompt and FAQ copies come from Portal's public documents. [INSTALL-CONTEXT.json](INSTALL-CONTEXT.json) records their pinned source commit, file paths, and SHA-256 hashes. It checks documentation-copy consistency; it is not an artifact signature and does not independently establish trusted remote provenance. Maintain the documents in Portal first, then synchronize all four files and the manifest. Do not copy internal development material. Older tags may not contain these files: do not move existing tags or claim to have read missing documents.

For detailed platform and client instructions, read the [installation Skill guide](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/installation.md) bundled with the selected MCP source version.

Before obtaining source, verify the domain, owner, repository name, and public accessibility, and obtain the full commit SHA from verified official commit or release information. Use Git in a dedicated download directory to obtain and check out that fixed commit. Before running the installer, compare `git rev-parse HEAD` with the selected SHA, check source completeness, and retain the actual origin and verification record. `FULL_COMMIT_SHA` is only a placeholder here for a verified full commit, not a directly executable value. `main`, a short SHA, or a manually assigned version label is not a substitute for verification.

For a first installation with no version already fixed by the user or an installation manifest, if only the official Gitee MCP mirror above is reachable, select and pin a version using verifiable official origin, full commit SHA, and integrity evidence from that mirror. GitHub does not have to be online at the same time. Clearly report the Gitee mirror and pinned version used; do not call it the latest version unless the primary source's latest state has been verified.

If the user or an installation manifest already pins a version, switching to the Gitee MCP backup must still obtain the complete source at **the same verified commit**. The installer accepts the verified local `--source-root`; it does not require GitHub to download the Runtime afterward. The mirror can lag behind: if the selected commit is missing, report that it has not been synchronized. Do not silently downgrade or describe the mirror's HEAD as the latest primary version. Stop if origin or integrity cannot be verified; do not fall back to internal repositories or unknown third parties, or disable TLS verification. Retain the complete client Plugin/Skill and connect each client as described below. Downloading source does not install every component.

The source backup does not cover Python package downloads. pip dependencies still require an available, trusted download source. If needed, set `PIP_INDEX_URL` to a verified package source; do not disable TLS verification or install from unknown sources. Managed macOS installations do not read user pip configuration files, so use that environment variable and retain failure logs. Platform arguments, Skill delivery, and client-configuration boundaries still follow the guidance below and the selected source version.

## Install all components

A complete installation includes the Lightroom plug-in, ToneRelay MCP, and the client startup Skill. First choose a dedicated ToneRelay installation directory. Keep source downloads, software, and runtime files in dedicated directories; an existing development checkout can also serve as installation input.

The default installation directory on macOS is `~/Library/Application Support/ToneRelay`. Place the source for the verified, fixed version in its `downloads/source/` directory, enter that source directory, and run the following command. The path below must match the actual download location:

```sh
python3 plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap_macos.py install \
  --source-root "$HOME/Library/Application Support/ToneRelay/downloads/source" \
  --version "fixed-version-label" --manage-source
```

Replace `fixed-version-label` with the verified, fixed version label first. Use `--manage-source` only for the dedicated `downloads/source/` directory created for this installation: it records the downloaded source files individually as uninstallable software. **Never add this option for an existing development checkout.** Such a checkout is read-only installation input, not an asset to remove during uninstallation. You can also use `--source-origin` and `--source-integrity` to record an already verified source, archive hash, or fixed commit; these options do not themselves perform remote verification.

On Windows PowerShell, use the platform-specific entry point:

```powershell
python plugins/tonerelay-lightroom/scripts/tonerelay_bootstrap.py install `
  --source-root "C:\path\to\verified-source" --version "fixed-version-label"
```

Replace the Windows example path and version with the actual values. The original Windows entry point does not support macOS's `--manage-source` or `record-client`. Do not apply those options or their uninstallation protections to Windows.

Continue using the actual paths in the installation output. Pay particular attention to:

| Output field | What to do next |
| --- | --- |
| `lightroom_plugin` | The plug-in path to register with Lightroom. After completing the registration steps below, confirm that it is enabled and check the version actually loaded. |
| `mcp_server` | Add this configuration through the target client's official MCP configuration interface, preserving existing servers. Connect each client separately. |
| `control` | Keep this maintenance entry point for checking status and restoring the connection. |
| `manifest` (macOS) | Keeps ownership records for software, downloaded source, and client connections for safe uninstallation later. |
| `verification` | Review the checks already completed and the steps that still require action. |

### Register the Lightroom plug-in and start Bridge

Installing the Runtime does not mean that the Lightroom plug-in is already registered. Use the `control` entry point from the installation output to run `bridge start` and `lightroom-plugin install`. The following macOS example uses the default path; if you chose a custom installation directory, change the variable to the actual returned path:

```sh
TONERELAY_CONTROL="$HOME/Library/Application Support/ToneRelay/bin/tonerelay"
"$TONERELAY_CONTROL" bridge start
"$TONERELAY_CONTROL" lightroom-plugin install
```

In Windows PowerShell, also use the actual maintenance entry point from the installation output. The following path is a placeholder that must be replaced:

```powershell
$ToneRelayControl = "C:\path\from-install-output\tonerelay.cmd"
& $ToneRelayControl bridge start
& $ToneRelayControl lightroom-plugin install
```

After successful registration, save your work, then quit Lightroom Classic normally and reopen it. Reloading the plug-in alone is not enough to verify that the new version is loaded. Then run `lightroom-plugin status` to check the registered path and the actual plug-in response. If the registration command requires manual action, choose the installation output's `lightroom_plugin` in Lightroom under **File → Plug-in Manager → Add**. Do not add duplicate entries or entries pointing to an old version. Check the ownership of existing registrations from other sources before making changes; do not simply overwrite them.

### Connect MCP and the startup Skill

A complete client Plugin installation should include `skills/`. If you add MCP manually, also install the same startup Skill by following the [client Skill installation steps](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/installation.md#deliver-the-startup-skill-to-each-client). The repository's MCP Plugin entry point targets macOS; on Windows, use the configuration returned by the installer.

Merge `mcp_server` through the target client's official configuration interface, preserving other servers. Bootstrap does not automatically install the configuration and Skill into every client. The client starts MCP; do not launch a separate, detached, persistent stdio process.

### Record each client (macOS)

After connecting a client, run `record-client` through the same `control` entry point to record the actual client in `install-manifest.json`. Record each client separately and give it a distinguishable name. All paths and client names below are placeholders and must be replaced first:

```sh
"$TONERELAY_CONTROL" record-client --client "my-client" \
  --config-path "/absolute/path/to/client-config.json" \
  --server-key "tonerelay-lightroom"
```

The default JSON adapter **only supports a top-level JSON `mcpServers` object**, and the specified entry's `command` must be the exact path to this installation root's `bin/tonerelay-mcp`. Change `--server-key` if you use a different key. The adapter does not support Codex TOML or entries that wrap the launcher with `/bin/sh` or similar commands. For supported records, the uninstaller verifies that the entry has not changed, then removes only the ToneRelay key and backs up the original configuration, preserving other servers.

For Codex TOML, an official Plugin, or a UI-managed connection, use official mode to record the actual configuration location instead:

```sh
"$TONERELAY_CONTROL" record-client --client "my-official-client" \
  --config-path "/absolute/path/to/actual-client-config" \
  --client-kind official
```

Official mode records only the location and operator evidence. **It does not automatically parse, modify, or uninstall these client configurations.** Later, remove the connection through the client's official interface, then follow the [uninstallation guide](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/uninstallation.md) and use `--removal-evidence` to record the removal result actually observed. Do not supply removal evidence during installation. If the actual configuration location is uncertain, keep the client record pending confirmation rather than inventing a path or claiming automatic registration. The original Windows entry point does not have this command. Keep the actual connection location and uninstallation steps; do not claim that Windows has the same manifest capabilities.

## Verify that it is ready to use

Open the target Catalog in Lightroom, select a photo, and say “Start editing in Lightroom” in the target client. Follow the prompt to confirm the initial Catalog binding, then check the actual connection status and current photo name. The client connection is usable only once it can read the photo selected for this check.

To inspect configuration or troubleshoot, you can say:

> Show the current ToneRelay configuration and connection status.

The initial Catalog binding requires explicit confirmation. Connectivity and photo-selection checks after binding remain read-only. Photo modifications begin only with a subsequent, explicit editing task.

## Connection failures and recovery

First check status through the `control` entry point provided by the installed version. On macOS, use `bridge status`, `bridge start`, `lightroom-plugin status`, and `mcp status` as needed. Refresh or reconnect in the actual client, then verify the current photo selection again.

| Symptom | Where to check |
| --- | --- |
| The client has no ToneRelay tools | Check that MCP and the Skill are each enabled and that the startup command still points to the actual installation location. |
| Bridge is unreachable | Check Bridge status through the maintenance entry point returned by the installer. |
| The Lightroom plug-in does not respond | Confirm that the Lightroom interface is available and the plug-in is enabled. After an upgrade, quit normally and reopen as needed. |
| Catalog authorization is incomplete | Complete the initial confirmation using the actual Catalog name, or resolve an existing binding conflict. |
| Python dependency downloads fail | Check the installation output and network connection. Use the dependency troubleshooting section in the [bundled FAQ](FAQ.en.md). |

You can check client configuration files with `tools/check_client_mcp.py` in the source (JSON MCP configurations; TOML configurations require Python 3.11+). It checks startup paths and does not replace a real connection check in the client.

## Upgrade and uninstall

Use a verified new fixed version to follow the appropriate platform's upgrade or repair steps, preserving existing configuration and editing data. Afterward, verify the version actually loaded in Lightroom and the client connection again.

See the [uninstallation guide](plugins/tonerelay-lightroom/skills/tonerelay-lightroom/references/uninstallation.md) for the complete procedure. The macOS maintenance entry point's `uninstall` command presents a plan first by default. Review client connections, processes, Lightroom registrations, and the software manifest before carrying out deletion according to the guide. Windows uses its original installation entry point; uninstalling the entire root deletes data inside it, so confirm backups and actual file ownership first.

[Back to introduction](README.en.md) · [Start editing](USAGE.en.md) · [ToneRelay Portal](https://reefruan.github.io/tonerelay-portal/?lang=en)
