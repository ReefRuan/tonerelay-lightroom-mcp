# ToneRelay FAQ

English FAQ. A Chinese version is provided separately.

## How should I describe the result I want?

Use everyday language, such as “Keep the warm colors and make the person's skin tone look natural.” Look at the result first, then decide the next step.

## Does the tool automatically decide which photo is best?

ToneRelay provides image measurements and comparisons. You and the Agent still decide which image is better.

## Do I have to copy my Catalog before using it?

No Catalog copy is required. Editing uses Lightroom virtual copies, so the original photo is not overwritten.

New installations default to `full`; you can start after verifying and binding the current Catalog you have confirmed. `safe` is an optional Sandbox restriction, not a read-only mode. Existing installations retain your chosen mode, and the installer offers help backing up the Catalog at the end.

## What if the Agent cannot find Lightroom's installation location?

Manually open Lightroom Classic and the Catalog you want to use, then reply “Opened”. The installation prompt requires the Agent to ask explicitly in the client rather than search the disk for the application. Downloads and dependency preparation can continue first; connection verification resumes after Lightroom is open. A probe timeout does not mean the application is closed.

## How do I continue if the Catalog is not bound or authorized?

First check the Catalog name shown by the Agent. Once you confirm it is the Catalog you want to use, reply “Bind”. The Agent will verify the target, save the binding, and read authorization back. If modes differ or the target changes, it must also explain the specific choice rather than just output an error and leave you to guess the next step. Explicitly requested read-only acceptance does not write a binding.

## How do I check versions and upgrade?

Ask the Agent to “Check the ToneRelay version” and report the actual MCP version, Lightroom plug-in version, and build. Verify the pinned installation-source version through the installation-status entry point supported by that version. Different component versions must not be conflated; mark information as unknown if it cannot be obtained from the running components.

An upgrade uses the verified newer version and updates ToneRelay entry points while preserving personal data and the currently valid configuration. A fresh installation does not merge old scattered configuration.

## What if the plug-in is in full mode but the Runtime expects safe?

The access mode and saved binding are inconsistent. If the current Catalog identity is the target you have confirmed, and you explicitly choose to retain `full`, the Agent can save the actual mode with the Catalog confirmation tool and recheck authorization, without making you switch back and forth in Plug-in Manager. If that authorization does not exist, make an explicit choice first; if the Catalog identity changes, confirm the target again. A connected bridge alone is not permission to continue reading or writing photos.

## Does a Catalog backup include original photos?

No. Catalog backups preserve Catalog records; original photos need separate backups. The Agent can help use Lightroom's built-in Catalog backup. Before a manual copy, quit Lightroom normally and retain both `.lrcat` and any existing `.lrcat-data`. See [Adobe's Catalog backup instructions](https://helpx.adobe.com/cn/lightroom-classic/desktop/manage-catalogs-and-files/back-catalog.html).

## Can I set star ratings, flags, and color labels through conversation?

If the connected version provides marking tools, you can batch-set ratings, flags, and color labels, and undo by operation. By default, photos that already have any marking are skipped; you can also choose to fill only empty fields. Existing markings are protected. Before use, ask the Agent to verify that the relevant tools have actually been discovered in the current session. A development implementation does not mean a public pinned release artifact is ready. A “Rejected” flag is a selection judgment, not deletion of the photo.

## Can I install it directly now?

Yes, from a pinned version of the public source. The [primary GitHub MCP repository](https://github.com/ReefRuan/tonerelay-lightroom-mcp) is public, and the [Gitee MCP fallback](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp) provides an official alternative source. Verify the repository identity, access, and full commit; for a tag, also verify the full commit it resolves to. Then read that version's INSTALL.en.md. This is a source preview, not a claim that stable installers exist or all platforms have passed acceptance checks; real-client verification is still required after setup. If provenance or integrity cannot be verified, stop the installation-download branch and report it; documentation reading or verification and reuse of a healthy local installation can continue, without falling back to an internal repository. Using local development source still requires your explicit path and authorization. If you are only adding another Agent client, a healthy installation with a verifiable source can be reused without downloading the complete source again.

## I already use ToneRelay in one Agent client. Do I need to reinstall for another?

Usually not. First verify the existing installation's source and version, and the health of the Runtime, Bridge, Lightroom plug-in, and Catalog authorization. Having only a client Plugin installed does not prove the shared components are ready. Once reuse is verified, the new client only needs its own official connection method to the same local MCP. If it supports Skills, also install the startup Skill from the same-version source, then check the connection read-only in a new session. Do not download, upgrade, or register another Lightroom plug-in just to add a client. Only repair a particular shared layer if it is actually broken or incompatible, and explain the effect on other clients.

## Do Windows and macOS have the same uninstall capabilities?

The new installation manifest, process-identity verification, and resumable uninstall are currently implemented only in the macOS development version and have not completed real-machine Windows acceptance. On Windows, use the entry points supported by the relevant version. Older whole-directory uninstalls delete the `data` inside that directory; to retain personal data, back it up first and then remove software item by item.

## Where are ToneRelay's installation and runtime data stored?

New installations require ToneRelay-owned files to be concentrated under one dedicated root: `~/Library/Application Support/ToneRelay` on macOS, or the installation root supported by the pinned version on Windows. Source copies, Runtime, dependencies, plug-in copies, runtime data, logs, caches, temporary files, and backups all belong there. MCP, Bridge, and their subprocesses must also continue using data, cache, and temporary directories within that root.

Host entries such as client configuration, Lightroom registration, and preferences remain in their original locations and are recorded. Photos, the main Catalog, user presets, and shared interpreters retain their original ownership. Existing deployments may still use external data. Default directories and prompt requirements do not prove actual locations; installation must read them back and verify them, without automatic migration.

## Is configuration stored in just one file?

The new macOS installation design uses `config.json` under the root to centralize paths, connections, Catalog binding, image dimensions, folder access, and execution switches; it does not migrate old scattered configuration. `install-manifest.json` stores file ownership and external registrations, while task data, logs, and the previous configuration backup have their own directories. Lightroom-managed permissions and host client configuration remain in host locations. The development source's `paths` command can show actual directories without reading photos or submitting plug-in jobs; first verify that the pinned version supports it.

## Why is setting the installation directory not enough?

Dependencies, caches, and temporary files also write to disk, and specifying only the software location does not constrain them. pip, Python, and MCP instances newly started by clients may use different default directories or may not inherit the installation terminal's environment. The installation prompt requires configuring and verifying data, cache, and temporary paths for installation, repair, and subsequent MCP/Bridge runs. The macOS development source now has a unified config.json and a launcher that sets the environment again on every launch, and permits installation sources in the root's downloads directory. A new pinned release and real-client acceptance must still be verified separately; source changes alone do not prove that an installation passed. If the version cannot meet these requirements, report that it is not ready rather than switching to a development repository.

## Can I download the templates in the gallery?

Yes. Open a gallery detail view to download the package or copy its link. Each of the six official style packs has a ZIP download containing `style.md`, a cover, reference images, and per-image license records. Public files are in [the Portal style-pack directory](https://github.com/ReefRuan/tonerelay-portal/tree/main/style-packs/packages). Downloading a style pack does not mean the Runtime is installed or supports automatic installation by pack ID; check the current version's installation instructions before using it.

The gallery detail view for [E01 Adaptive Skin-Tone Fidelity](https://reefruan.github.io/tonerelay-portal/?lang=en&preset=skin-tone-adaptive-fidelity#gallery) lets you read the prompt and source index directly and download the complete candidate effect package, including two physical color-chart photographs with verified licenses. These photos are not standard color values; paired Lightroom results are still pending verification. The detail view for [E02 Luminous Mist Portrait](https://reefruan.github.io/tonerelay-portal/?lang=en&preset=luminous-mist-portrait#gallery) provides a text-link-only candidate package: its six tutorial reference photos appear only as links to their original sites and are not hosted in the ZIP or on the website. Neither effect package is automatically installed by style-pack ID.

## How can I download when GitHub is slow or inaccessible from China?

Installation source and documentation/style packs have two separate official source pairs:

- Complete MCP installation source and its bundled installation prompt and FAQ: [GitHub primary](https://github.com/ReefRuan/tonerelay-lightroom-mcp) / [Gitee source fallback](https://gitee.com/ReefRuan/tonerelay-lightroom-mcp). At a pinned commit containing the documents, read `INSTALL-PROMPT.en.md`, `FAQ.en.md`, and `INSTALL.en.md` in the root completely; installation does not require reaching Portal again. When GitHub is unreachable, obtain complete source at the same pinned full commit from Gitee. Older tags or lagging mirrors may lack the added documents: report this rather than moving old tags or mixing components. Portal cannot replace the MCP source repository.
- Maintenance source for public installation documents, the website, and style resources: [GitHub Portal](https://github.com/ReefRuan/tonerelay-portal) / [Gitee Portal fallback](https://gitee.com/ReefRuan/tonerelay-portal). If an older MCP version lacks the documents, obtain the [English installation prompt on Gitee](https://gitee.com/ReefRuan/tonerelay-portal/blob/main/docs/install-prompt.en.md) separately and record its full commit, while following the target source version's actual capabilities. ZIP fallbacks for the six official style packs are under https://gitee.com/ReefRuan/tonerelay-portal/raw/main/style-packs/downloads/ ; gallery details also provide China-access fallback URLs. Check mirror versions and file availability for E01 and E02 rather than assuming they have synchronized.

Mirrors may lag: neither main nor the word “fallback” means the latest version. Verify the official owner, public access, pinned full commit, and file integrity; do not mix components from different commits. If only the mirror is reachable initially, report the selected mirror, full commit, and verification evidence. Restoring live GitHub access is not required to use an official fallback. Without sufficient evidence, stop the installation-download branch and report it; documentation reading or verification and reuse of a healthy local installation can still continue.

A source fallback does not automatically solve pip dependency downloads. If PIP_INDEX_URL is needed, use only a verified trusted HTTPS index, scope it to this process or the dedicated environment, and record it; do not silently change global settings or disclose credentials. Keep TLS verification enabled. Do not bypass failures with --trusted-host, disabled certificate checks, or packages of unknown origin.

## Why does installation still need human authorization?

When login, a system password, or a step requiring your own confirmation is encountered, the Agent explains what you need to do and then resumes from the checkpoint. Other ordinary installation steps that have already been authorized can continue without interruption.

## How are installation and uninstallation verified?

During installation, check components layer by layer, then verify tool discovery and the read-only connection in a new client session.

During uninstallation, check actual client connections, processes, Lightroom registration, and files against the manifest; do not submit any more plug-in probe jobs. Reconnect or create a new session only if client refresh cannot be confirmed. Mark any unobservable layer as pending verification.

## Will it reinstall after uninstallation?

Not if you only requested uninstallation. If you explicitly request a clean reinstall, complete uninstallation, uninstall-baseline verification, installation, and new-session verification within the same task.

## Windows: why is there no response after installing the Lightroom plug-in?

First check that the local Bridge is running: `%SystemDrive%\ToneRelay\bin\tonerelay.cmd bridge status` should show `listening`. The plug-in works by polling the local Bridge and does not respond when Bridge is absent. Start Bridge in a normal terminal. The installer does not register automatic startup at boot, so Bridge must be started again after restarting the computer.

## Windows: does a plug-in-check timeout mean installation failed?

Not necessarily. This is a short-timeout probe, and the plug-in may not have started polling immediately after Lightroom reopens. Quit normally and reopen Lightroom before checking again; do not try to solve this by repeatedly reinstalling. Investigate it as a connection problem only if multiple consecutive checks time out.

## Windows: do I need to add the plug-in manually in Plug-in Manager?

The installer writes an entry under `%APPDATA%\Adobe\Lightroom\Modules`, and Lightroom loads it automatically at startup, so manual addition is usually unnecessary. “Remove” being disabled in Plug-in Manager for an automatically discovered entry is normal. Do not rename or relocate it merely to make it look “more like a proper installation.”

## Windows: why does it ask for repair or plug-in registration after installation?

First check whether Bridge is listening. A stopped Bridge can also make the plug-in appear unloaded; start Bridge before deciding whether repair is necessary.

## Windows: why won't Lightroom quit?

If a dialog such as Plug-in Manager is open, a quit request will not take effect. Close the dialog first, then quit normally. Do not force-terminate Lightroom while it is writing the Catalog.

## Windows: what should I do if fast thumbnails fail?

First distinguish whether the tool failed to obtain the image or the chat failed to display it. In the latter case, check the client's image-delivery reference and actual conversation-body rendering; a thumbnail in a tool card does not prove it was displayed in the conversation body. Record the version, tool result, and error for the maintainer to investigate; do not repeatedly reinstall on that basis.

## Windows: which MCP configuration should the client use?

Use the `mcp_server` entry printed by the installer. A `.mcp.json` written for macOS cannot be copied unchanged to Windows; doing so will cause the connection to fail.

## Why can't I see ToneRelay tools in a new session?

Some clients require a new session or restart before newly added tools appear. A successful result in the installation session does not replace verification in a new session.
