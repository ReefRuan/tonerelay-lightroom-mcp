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
//       LR_OPS_URL=http://127.0.0.1:17777 node adapter.mjs

import { writeSync } from 'node:fs';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { connect } from './lrws.mjs';
import {
  applySettings,
  parameterNames,
  readSettings,
  validatePatch,
} from './develop.mjs';

const SERVICE = new URL(process.env.LR_OPS_URL ?? 'http://127.0.0.1:17778');
const TOKEN = process.env.LR_XMP_MIMIC_LOCAL_TOKEN ?? null;
const POLL_MS = Number(process.env.LR_CLOUD_POLL_MS ?? 500);
const BUILD_ID = 'lightroom-cloud-adapter-0.1.0';
const PLUGIN_VERSION = '0.8.0.0-cloud';
const ACTIVE_UUID = 'cloud:active';
const CATALOG_IDENTITY = 'lightroom-cloud:active-library';
// Cloud cannot edit a copy, so an opt-in safety net matters: with this set the
// adapter rewrites the captured baseline on the way out.
const RESTORE_ON_EXIT = /^(1|true|yes)$/i.test(process.env.LR_CLOUD_RESTORE_ON_EXIT ?? '');

// Synchronous on purpose: process.exit() truncates piped stderr, and the two
// lines that matter most are the ones written while shutting down.
const log = (...args) =>
  writeSync(2, `${new Date().toISOString().slice(11, 19)} ${args.join(' ')}\n`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
      catalog_authorized: true,
      capabilities: {
        // The lr-ops CatalogBinding handshake (catalog_binding.py) refuses to
        // talk to a plugin that does not advertise these two.
        expected_access_mode: true,
        expected_catalog_identity: true,
        // True where this adapter really delivers the Classic capability.
        develop_state: true,
        develop_snapshot: true,
        develop_apply: true,
        xmp_roundtrip: true,
        in_place_settings_v1: true,
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

  async apply_candidate(params, session) {
    const { clean, rejected } = await session.withLr((lr) => validatePatch(lr, params?.settings));
    if (rejected.length) {
      return { photo: photoInfo(), status: 'conflict', code: 'unsupported_settings', applied: false, rejected };
    }
    if (Object.keys(clean).length === 0) {
      throw new Error('apply_candidate requires a settings object.');
    }
    if (params?.require_in_place_guard) {
      const snapshot = params?.expected_settings_snapshot;
      const current = await readSettingsFor(session);
      if (!snapshot || !sameSettings(current, snapshot)) {
        return { photo: photoInfo(), status: 'conflict', code: 'settings_changed', applied: false };
      }
    }

    if (process.env.LR_DEBUG) log('apply patch', JSON.stringify(clean), 'params keys', Object.keys(params ?? {}).join(','));
    const result = await session.withLr((lr) =>
      applySettings(lr, clean, { defaults: session.defaults }),
    );
    if (process.env.LR_DEBUG) log('apply result', JSON.stringify({ xmp: result.xmpBytes, changed: result.changed }));
    const mismatched = Object.entries(result.changed).filter(
      ([, { requested, readback }]) => !sameValue(requested, readback),
    );
    return {
      photo: photoInfo(),
      before: result.before,
      after: result.settings,
      applied: mismatched.length === 0,
      status: mismatched.length === 0 ? 'applied' : 'readback_mismatch',
      changed: result.changed,
      mismatched: mismatched.map(([name]) => name),
      drift: result.drift,
      elapsed_ms: result.elapsedMs,
      xmp_bytes: result.xmpBytes,
    };
  },

  async reset_to_baseline(params, session) {
    const uuid = params?.photo_uuid ?? ACTIVE_UUID;
    const baseline = session.baselines.get(uuid);
    if (!baseline) {
      throw new Error('No captured baseline for this photo. Run capture_baseline first.');
    }
    // The baseline is a full settings set, so applying it restores everything
    // the photo had — including parameters the caller never touched.
    const result = await session.withLr((lr) =>
      applySettings(lr, baseline, { defaults: session.defaults }),
    );
    return {
      photo: photoInfo(),
      settings: result.settings,
      restored: Object.keys(baseline).length,
      elapsed_ms: result.elapsedMs,
    };
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
        results.push({ command: entry.command, ok: true, data: await handler(entry.params ?? {}, session) });
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
export async function restoreBaselines(session, { onlyIfChanged = true } = {}) {
  const uuid = ACTIVE_UUID;
  const baseline = session.baselines.get(uuid);
  if (!baseline) return { restored: false, reason: 'no baseline captured for this photo' };

  const current = await readSettingsFor(session);
  if (onlyIfChanged && sameSettings(current, baseline)) {
    return { restored: false, reason: 'photo already matches its baseline' };
  }

  const result = await session.withLr((lr) =>
    applySettings(lr, baseline, { defaults: session.defaults }),
  );
  const mismatched = Object.keys(baseline).filter(
    (name) => !sameValue(result.settings[name], baseline[name]),
  );
  return {
    restored: mismatched.length === 0,
    photo: photoInfo(),
    keys: Object.keys(baseline).length,
    mismatched,
    elapsed_ms: result.elapsedMs,
  };
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

function sameValue(a, b) {
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) < 1e-6;
  return String(a) === String(b);
}

function sameSettings(a, b) {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const key of keys) if (!sameValue(a[key], b[key])) return false;
  return true;
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
  if (RESTORE_ON_EXIT) {
    try {
      const out = await restoreBaselines(session);
      log(out.restored ? `restored baseline (${out.keys} settings)` : `no restore: ${out.reason}`);
    } catch (err) {
      log(`restore failed: ${err.message}`);
    }
  }
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
