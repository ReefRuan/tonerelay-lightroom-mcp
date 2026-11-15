# tonerelay-cloud

Drive **Adobe Lightroom (cloud)** — the desktop app, not Classic — from the ToneRelay engine.

ToneRelay has two halves: an HTTP job queue (`services/lr-ops`) and a Lua plugin
(`plugins/lightroom/lr-xmp-mimic.lrdevplugin`) that executes those jobs inside Lightroom
Classic. The queue half is editor-agnostic; only the executor is Classic-specific.

This directory replaces the executor. Same wire protocol, same job shapes, different
editor — Lightroom cloud speaks Adobe's **External Controller API** (a local WebSocket),
which is the only automation surface it has. There is no plugin SDK and no catalog API.

```
ToneRelay CLI / workbench / MCP server
        │  HTTP job queue  (GET /jobs/next, POST /jobs/<id>/result)
        ▼
  lr_ops serve                  ← unmodified
        │
        ▼
  adapter.mjs (node)            ← this directory
        │  WebSocket ws://127.0.0.1:7682
        ▼
  Adobe Lightroom (cloud) 10.0
```

## Requirements

- Adobe Lightroom (cloud) desktop, 9.4 or newer (tested on **10.0**, macOS).
- Node.js 20+ (tested on 26.13). No npm dependencies — plain ESM.
- Python 3.10+ to run `lr_ops`.

## Setup

1. **Turn on the controller API** in Lightroom:
   `Preferences → Interface → Enable external controllers`, then quit and relaunch
   Lightroom. A listening port appears and is recorded in
   `~/Library/Application Support/Adobe/Lightroom CC/Connections/connections.json`.
2. **Start the job queue** from the repository root:

   ```bash
   cd services/lr-ops
   LR_XMP_MIMIC_BRIDGE_PORT=17778 \
   LR_XMP_MIMIC_RUNTIME_DATA_ROOT=/tmp/lr-ops-data \
     python3 -m lr_ops serve
   ```

   Port 17778 avoids 17777 only because a sandbox process may already hold it; use the
   default 17777 when it is free.
3. **Start the adapter:**

   ```bash
   LR_OPS_URL=http://127.0.0.1:17778 node adapter.mjs
   ```

   The first connection makes Lightroom show a **Pair dialog** — click Allow. The client
   id is cached in `.lr-client.json`, so it never asks again.
4. **Open the photo you want ToneRelay to work on** and stay in the Develop module.

## Use it through the CLI

```bash
cd tonerelay-lightroom-mcp/services/lr-ops && export LR_XMP_MIMIC_BRIDGE_PORT=17778

python3 -m lr_ops probe                      # adapter + Lightroom identity
python3 -m lr_ops selection                  # the open photo
python3 -m lr_ops state cloud:active         # full develop state
python3 -m lr_ops baseline cloud:active      # remember the current look
python3 -m lr_ops apply cloud:active '{"Exposure2012": 0.35, "Clarity2012": 20}'
python3 -m lr_ops reset cloud:active         # back to the captured baseline
```

## Tests

**Unit tests — no Lightroom needed, CI-friendly:**

```bash
node --test test/        # 14 tests, ~1.4 s
```

They cover the `crs:` namespace regression, "null means default", whole-document
replacement, range/unknown rejection, the in-place guard, baseline reset, the
restore-on-exit helper, batch error envelopes, the unsupported-command reasons,
and the process signal path (a shutdown that swallows its own log line is a bug
that only shows up in production).

**Acceptance test — needs a live Lightroom:**

```bash
LR_OPS_URL=http://127.0.0.1:17778 node verify.mjs
```

24 assertions covering probe/binding, selection, state, write + readback, the in-place
guard, reset, `command_batch`, and unsupported-command reporting. It captures the open
photo's settings first and **always writes them back**, so it is safe on a real library.

## Safety: cloud edits the real photo

Classic ToneRelay works on a virtual copy, so the original is never at risk. Lightroom
cloud has no copies — an edit lands on the actual photo. Two things exist to soften that:

- **`reset`** after each round: the CLI already restores the captured baseline.
- **`LR_CLOUD_RESTORE_ON_EXIT=1`**: on `SIGINT`/`SIGTERM` the adapter writes the captured
  baseline back before exiting.

```bash
LR_OPS_URL=http://127.0.0.1:17778 LR_CLOUD_RESTORE_ON_EXIT=1 node adapter.mjs
# ... edit ... then Ctrl-C, or `kill -TERM <pid>`
```

A `SIGKILL` (or a crash) cannot run the handler, so the edits stay. If that matters, also
call `reset`. The handler is deliberately opt-in: restoring settings the user meant to keep
would be worse than the risk it removes.

## What is verified (Lightroom 10.0, macOS)

| Behaviour | Result |
| --- | --- |
| `probe_client` | 163 parameters (145 global, 19 local), binding handshake accepted |
| `get_selection` | the open photo, as `cloud:active` |
| `get_develop_state` | 99 concrete values of 163 — the rest are unset at default |
| `apply_candidate` | exact readback, ~140–500 ms per write |
| in-place guard | stale snapshot rejected with `status: conflict` |
| `reset_to_baseline` | restores the captured settings set |
| `command_batch` | ordered execution, `failed_at_index` on error |
| persistence | edits survive a Lightroom quit + relaunch **in the catalog** |
| restore on exit | `SIGTERM` writes the baseline back, 99 settings, exit 0 |

## Where cloud differs from Classic

- **No catalog, no photo list.** The controller API can only address *the photo currently
  open*; there is no "select by uuid", no search, no folders. `cloud:active` is a handle
  for "whatever is on screen", and `getActiveImageName`/`getActivePhotoId` are not
  implemented by Adobe (`invalid_request` in 9.6 and 10.0).
- **No virtual copies.** Classic ToneRelay edits a working virtual copy and protects the
  original; cloud has no copies, so `create_virtual_copy` is refused with a reason.
- **`setImageSettingsXMP` replaces the whole develop state.** Omitted attributes reset to
  default. Every write here is read-modify-write over the full settings set.
- **`getValue` returns null for "at default".** Reads are normalised against
  `getDefault()`; parameters with no default are omitted rather than invented.
- **No commit verb.** There is no save call — Lightroom persists on its own schedule.
  Sidecar XMP files exist on disk but stay stale, so never read them back to verify.
- **No export, no import, no thumbnails** through this API, so `export_preview` and
  `import_photo` are refused.
- **Masks** exist in the protocol (`createMaskGroup`, `addMaskComponent`, …) but are not
  implemented here.

## Files

| File | Role |
| --- | --- |
| `lrws.mjs` | controller WebSocket client: port discovery, pairing, request/response |
| `xmp.mjs` | settings → `crs:` XMP document |
| `develop.mjs` | read/validate/apply develop settings, readback settle loop |
| `adapter.mjs` | lr-ops job executor (the Classic `Bridge.lua` counterpart) |
| `verify.mjs` | acceptance test (live Lightroom) |
| `test/adapter.test.mjs` | unit tests against a fake controller |
| `services/lr-ops/tests/test_cloud_adapter_unit.py` | runs the tests above from pytest, so one command covers both |

## Troubleshooting

- **`ECONNREFUSED` / close code 1006** — Lightroom is not running, or the controller
  preference was toggled (toggling it quits the app).
- **Hangs on first connect** — the Pair dialog is waiting behind the Lightroom window.
- **Readback stays unchanged after an apply** — you are probably writing an XMP document
  with unprefixed attributes; every `crs:` attribute must carry the prefix or Lightroom
  ignores the document silently.
- **`403 Forbidden` from the queue** — something else owns the port; pick another.
