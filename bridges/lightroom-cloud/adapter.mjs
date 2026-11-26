// lr-ops job executor for Lightroom desktop (cloud).
//
// The ToneRelay engine is split in two: `services/lr-ops` is an HTTP job queue
// that the workbench talks to, and `plugins/lightroom/.../Bridge.lua` is the
// executor *inside* Lightroom Classic that polls it. This file is a drop-in
// replacement for the second half: same queue, same job format, same result
// format — but the work is done through Lightroom's External Controller API
// instead of the Classic Lua SDK.
//
//   GET  /jobs/next            -> oldest queued job, or empty body
//   POST /jobs/<id>/result     <- {"ok": bool, "data"|"error": ...}
//
// Run:  node adapter.mjs            (poll forever)
//       node adapter.mjs --once     (drain one job, for tests)
//       LR_OPS_URL=http://127.0.0.1:17778 node adapter.mjs
// Port 17777 is the Lightroom Classic bridge. This process exits if pointed there.

import { writeSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { connect } from './lrws.mjs';
import {
  parameterNames,
  readSettings,
} from './develop.mjs';

const SERVICE = new URL(process.env.LR_OPS_URL ?? 'http://127.0.0.1:17778');
const TOKEN = process.env.LR_XMP_MIMIC_LOCAL_TOKEN ?? null;
const POLL_MS = Number(process.env.LR_CLOUD_POLL_MS ?? 500);
const BUILD_ID = 'lightroom-cloud-adapter-0.1.0';
const PLUGIN_VERSION = '0.8.0.0-cloud';
const ACTIVE_UUID = 'cloud:active';
const CATALOG_IDENTITY = 'lightroom-cloud:active-library';
export const CLASSIC_BRIDGE_PORT = '17777';
// The controller API exposes no photo id. cloud:active names whatever is open,
// so a stored baseline can land on a different photo. Writes stay closed.
export const PHOTO_IDENTITY_UNBOUND =
  'photo_identity_unbound: the open photo has no stable id, so this adapter will not write';

// Synchronous on purpose: process.exit() truncates piped stderr, and the two
// lines that matter most are the ones written while shutting down.
const log = (...args) =>
  writeSync(2, `${new Date().toISOString().slice(11, 19)} ${args.join(' ')}\n`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function usesClassicBridge(url) {
  const parsed = url instanceof URL ? url : new URL(url);
  const port = parsed.port || (parsed.protocol === 'https:' ? '443' : '80');
  return port === CLASSIC_BRIDGE_PORT;
}

function writeRefused() {
  return {
    ok: false,
    photo: photoInfo(),
    status: 'conflict',
    code: 'photo_identity_unbound',
    applied: false,
    error: PHOTO_IDENTITY_UNBOUND,
  };
}

// --- service ---------------------------------------------------------------

async function api(path, init = {}) {
  const headers = { ...(init.headers ?? {}) };
  if (TOKEN) headers['X-LR-XMP-Token'] = TOKEN;
  if (init.body) headers['Content-Type'] = 'application/json';
  const res = await fetch(new URL(path, SERVICE), { ...init, headers });
  const text = await res.text();
  if (!res.ok) throw new Error(`${path} -> ${res.status} ${text.slice(0, 200)}`);
  return text ? JSON.parse(text) : null;
}

const nextJob = () => api('jobs/next');
const postResult = (id, body) =>
  api(`jobs/${id}/result`, { method: 'POST', body: JSON.stringify(body) });

// --- session ---------------------------------------------------------------

/** Holds the controller connection and the per-photo caches. */
class Session {
  constructor() {
    this.lr = null;
    this.defaults = new Map();
    this.baselines = new Map();
    this.names = null;
  }

  async ensure() {
    if (this.lr) return this.lr;
    this.lr = await connect({ name: 'tonerelay-cloud', version: PLUGIN_VERSION });
    this.names = await parameterNames(this.lr);
    return this.lr;
  }

  async drop() {
    try {
      this.lr?.close();
    } catch {
      /* already gone */
    }
    this.lr = null;
    this.names = null;
  }

  async withLr(fn) {
    const lr = await this.ensure();
    try {
      return await fn(lr);
    } catch (err) {
      // A dropped socket must not poison the next job.
      await this.drop();
      throw err;
    }
  }
}

/** Shape matches Commands.lua's photoInfo() so the workbench keeps parsing it. */
function photoInfo(extra = {}) {
  return {
    uuid: ACTIVE_UUID,
    file_name: '',
    copy_name: '',
    is_virtual_copy: false,
    master_uuid: null,
    edit_protected: false,
    width: 0,
    height: 0,
    ...extra,
  };
}

// --- commands --------------------------------------------------------------

export const commands = {
  async probe_client(_params, session) {
    return session.withLr(async (lr) => ({
      lr_version: lr.version,
      plugin_version: PLUGIN_VERSION,
      build_id: BUILD_ID,
      platform: process.platform,
      api: 'external-controller',
      catalog_open: true,
      // Cloud exposes no library identity, so the binding is advisory: it is
      // stable for the life of the adapter and never validated against a path.
      access_mode: 'full',
      catalog_identity: CATALOG_IDENTITY,
      // False so the Classic Catalog handshake does not treat this process as
      // an authorized library. Reads still return the open photo.
      catalog_authorized: false,
      capabilities: {
        expected_access_mode: true,
        expected_catalog_identity: true,
        develop_state: true,
        develop_snapshot: true,
        develop_apply: false,
        xmp_roundtrip: false,
        in_place_settings_v1: false,
        hsl: true,
        white_balance: true,
        split_toning: true,
        color_grade: true,
        post_crop_vignette: true,
        passthrough_settings: false,
        curves: false,
        develop_presets_read: false,
        catalog_search: false,
        catalog_folders_v1: false,
        import_photo: false,
        export_preview: false,
        preview_fast_request_jpeg_thumbnail: false,
        local_masks: false,
        copy_protection_v1: false,
        lightroom_cloud_adapter: true,
        photo_identity_bound: false,
      },
      parameter_count: session.names.global.length,
      local_parameter_count: session.names.local.length,
      photo: photoInfo(),
    }));
  },

  async get_selection(_params, session) {
    await session.ensure();
    return { target: photoInfo() };
  },

  async get_develop_state(params, session) {
    return { photo: photoInfo(), settings: await readSettingsFor(session) };
  },

  async get_develop_snapshot(params, session) {
    return { photo: photoInfo(), settings: await readSettingsFor(session) };
  },

  async capture_baseline(params, session) {
    const settings = await readSettingsFor(session);
    session.baselines.set(params?.photo_uuid ?? ACTIVE_UUID, settings);
    return { photo: photoInfo(), baseline: settings };
  },

  async apply_candidate(_params, _session) {
    return writeRefused();
  },

  async reset_to_baseline(_params, _session) {
    return { ...writeRefused(), restored: 0 };
  },

  async command_batch(params, session) {
    const entries = params?.commands;
    if (!Array.isArray(entries)) throw new Error('command_batch requires {"commands": [...]}');
    const results = [];
    let failedAt = null;
    for (let i = 0; i < entries.length; i += 1) {
      const entry = entries[i];
      const handler = commands[entry?.command];
      if (!handler) {
        failedAt = i;
        const reason = unsupportedReason(entry?.command);
        results.push({
          command: entry?.command,
          ok: false,
          error: reason
            ? `unsupported_command: ${entry?.command} (${reason})`
            : `unsupported_command: ${entry?.command}`,
        });
        break;
      }
      try {
        const data = await handler(entry.params ?? {}, session);
        if (data && data.ok === false) {
          failedAt = i;
          results.push({ command: entry.command, ok: false, error: data.error, data });
          break;
        }
        results.push({ command: entry.command, ok: true, data });
      } catch (err) {
        failedAt = i;
        results.push({ command: entry.command, ok: false, error: err.message });
        break;
      }
    }
    const out = { results };
    if (failedAt !== null) {
      out.failed_at_index = failedAt;
      out.results_so_far = results;
      out.ok = false;
      out.error = results[failedAt].error;
      return out;
    }
    return out;
  },
};

/**
 * Put the open photo back to the last captured baseline.
 *
 * Exported because the exit path is not the only place that wants it: the
 * workbench's "discard this round" can call the same helper, and tests can
 * exercise it without a process.
 */
export async function restoreBaselines(session) {
  void session;
  return { restored: false, reason: PHOTO_IDENTITY_UNBOUND };
}

// Classic capabilities that Lightroom cloud simply cannot express. Each reason
// is surfaced to the caller instead of a bare "unsupported_command", so the app
// can show why a tool is missing rather than looking broken.
export const UNSUPPORTED = {
  create_virtual_copy:
    'Lightroom cloud has no virtual copies, so ToneRelay\'s copy policy cannot be expressed',
  apply_mask:
    'the controller API exposes masking verbs, but this adapter does not implement them yet',
  update_xmp_sidecar:
    'cloud keeps develop settings inside its own catalog; there are no sidecar files to write',
  export_preview:
    'external controllers get no export/render verb, so previews cannot be produced from here',
  import_photo: 'cloud import is not exposed to external controllers',
};

function unsupportedReason(name) {
  return UNSUPPORTED[name] ?? null;
}

async function readSettingsFor(session) {
  const { settings, defaults } = await session.withLr((lr) =>
    readSettings(lr, { defaults: session.defaults }),
  );
  session.defaults = defaults;
  return settings;
}

// --- loop ------------------------------------------------------------------

async function runJob(job, session) {
  const name = job.command;
  const handler = commands[name];
  if (!handler) {
    const reason = unsupportedReason(name);
    const err = reason
      ? `unsupported_command: ${name} (${reason})`
      : `unsupported_command: ${name} (this adapter speaks the develop-settings subset)`;
    await postResult(job.job_id, { ok: false, error: err });
    log(`✗ ${name} — ${err}`);
    return false;
  }
  const started = Date.now();
  try {
    const data = await handler(job.params ?? {}, session);
    if (data && data.ok === false) {
      await postResult(job.job_id, { ok: false, error: data.error, failed_at_index: data.failed_at_index, results_so_far: data.results_so_far });
      log(`✗ ${name} — ${data.error}`);
      return false;
    }
    await postResult(job.job_id, { ok: true, data });
    log(`✓ ${name} in ${Date.now() - started} ms`);
    return true;
  } catch (err) {
    await postResult(job.job_id, { ok: false, error: err.message });
    log(`✗ ${name} — ${err.message}`);
    return false;
  }
}

async function main() {
  if (usesClassicBridge(SERVICE)) {
    log(`refusing ${SERVICE.href}: port ${CLASSIC_BRIDGE_PORT} is the Lightroom Classic bridge`);
    process.exit(2);
  }
  const once = process.argv.includes('--once');
  const session = new Session();
  for (const signal of ['SIGINT', 'SIGTERM']) {
    process.on(signal, () => {
      shutdown(signal, session);
    });
  }
  log(`adapter up: service=${SERVICE.href} token=${TOKEN ? 'yes' : 'no'}`);

  let idle = 0;
  for (;;) {
    let job = null;
    try {
      job = await nextJob();
      idle = 0;
    } catch (err) {
      log(`service unreachable (${err.message}); retrying in 3s`);
      await sleep(3000);
      if (once) process.exit(2);
      continue;
    }
    if (!job) {
      if (stopping) {
        await sleep(POLL_MS);
        continue;
      }
      if (once) {
        log('no queued job');
        await session.drop();
        return;
      }
      idle += 1;
      if (idle === 1) log('polling…');
      await sleep(POLL_MS);
      continue;
    }
    await runJob(job, session);
    if (once && job) {
      await session.drop();
      return;
    }
  }
}

let stopping = false;

async function shutdown(signal, session) {
  if (stopping) return;
  stopping = true;
  log(`${signal} — shutting down`);
  await session.drop();
  process.exit(0);
}

// Only poll when run directly: importing this module (tests) must not start it.
if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main().catch((err) => {
    console.error('adapter failed:', err);
    process.exit(1);
  });
}
