// Develop-settings read/write on top of the controller API.
//
// Two facts drive this file, both measured on Lightroom 10.0 (macOS):
//
//  1. getValue() returns null for a parameter that sits at its default — it
//     means "unset", not "zero". Every read is therefore normalised against
//     getDefault().
//  2. setImageSettingsXMP() replaces the whole develop state of the open
//     photo: any crs: attribute you omit is reset to its default. A "patch"
//     must be read-modify-write over the full settings set, never a partial
//     document. (Verified: sending only Exposure+Contrast zeroed Clarity.)

import { settingsToXmp } from './xmp.mjs';

const LOCAL = /^Local/;
const DOTTED = /\./;

export class UnsupportedSetting extends Error {}

export function isGlobalParam(name) {
  return !LOCAL.test(name) && !DOTTED.test(name);
}

/** All parameter names the controller exposes, split into global/local. */
export async function parameterNames(lr) {
  const [names] = await lr.params();
  return {
    all: names,
    global: names.filter(isGlobalParam),
    local: names.filter((n) => LOCAL.test(n)),
  };
}

/**
 * Read every global parameter, normalising "null means default".
 * ~150 getValue round-trips, ~150 ms locally.
 */
export async function readSettings(lr, { defaults = new Map() } = {}) {
  const { global } = await parameterNames(lr);
  const settings = {};
  for (const name of global) {
    let value = await lr.get(name);
    if (value === null || value === undefined) {
      if (!defaults.has(name)) {
        try {
          const [def] = await lr.request('getDefault', [name]);
          defaults.set(name, def);
        } catch {
          defaults.set(name, null);
        }
      }
      value = defaults.get(name);
    }
    if (value === null || value === undefined) continue;
    settings[name] = value;
  }
  return { settings, defaults };
}

/** Which of the settings can be written back through an XMP document. */
export function serialisable(settings) {
  const ok = {};
  const skipped = [];
  for (const [name, value] of Object.entries(settings)) {
    if (!isGlobalParam(name) || typeof value === 'boolean') {
      skipped.push(name);
      continue;
    }
    if (typeof value !== 'number' && typeof value !== 'string') {
      skipped.push(name);
      continue;
    }
    ok[name] = value;
  }
  return { ok, skipped };
}

/** Validate a patch against the live parameter set and its ranges. */
export async function validatePatch(lr, patch) {
  const { all } = await parameterNames(lr);
  const known = new Set(all);
  const clean = {};
  const rejected = [];
  for (const [name, value] of Object.entries(patch || {})) {
    if (!known.has(name)) {
      rejected.push([name, 'unknown_parameter']);
      continue;
    }
    if (!isGlobalParam(name)) {
      rejected.push([name, 'local_mask_setting_not_supported']);
      continue;
    }
    if (typeof value !== 'number' && typeof value !== 'string') {
      rejected.push([name, 'value_must_be_number_or_string']);
      continue;
    }
    if (typeof value === 'number') {
      const [min, max] = await lr.range(name);
      if (!Number.isFinite(value) || value < min || value > max) {
        rejected.push([name, `out_of_range(${min}..${max})`]);
        continue;
      }
    }
    clean[name] = value;
  }
  return { clean, rejected };
}

/**
 * Read-modify-write: overlay `patch` on the current settings, push the result
 * as one complete XMP document, then read back and report the diff.
 */
export async function applySettings(
  lr,
  patch,
  { settleTimeoutMs = 6000, pollMs = 250, defaults } = {},
) {
  const state = await readSettings(lr, defaults ? { defaults } : {});
  const before = state.settings;
  const merged = { ...before, ...patch };
  const { ok, skipped } = serialisable(merged);
  const xml = settingsToXmp(ok);

  const started = performance.now();
  await lr.applyXmp(xml);

  // The write is applied asynchronously inside the develop session: a readback
  // straight after the call still returns the previous values (measured). Poll
  // the patched parameters until they land, so "applied" is never a guess.
  const wanted = Object.entries(patch);
  let after;
  const deadline = started + settleTimeoutMs;
  for (;;) {
    after = (await readSettings(lr, { defaults: state.defaults })).settings;
    const landed = wanted.every(([name, value]) => sameValue(after[name], value));
    if (landed || performance.now() > deadline) break;
    await new Promise((r) => setTimeout(r, pollMs));
  }

  const changed = {};
  for (const [name, value] of Object.entries(patch)) {
    changed[name] = { requested: value, readback: after[name] ?? null };
  }
  const drift = Object.keys(before).filter(
    (name) => name in patch === false && String(after[name]) !== String(before[name]),
  );

  return {
    settings: after,
    before,
    changed,
    drift,
    skipped,
    xml,
    xmpBytes: xml.length,
    elapsedMs: Math.round(performance.now() - started),
  };
}

function sameValue(a, b) {
  if (typeof a === 'number' && typeof b === 'number') return Math.abs(a - b) < 1e-6;
  return String(a) === String(b);
}
