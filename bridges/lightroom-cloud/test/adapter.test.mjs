// Unit tests for the job-executor commands, without Lightroom.
//
// `verify.mjs` proves the adapter against a real app; this file proves the parts
// that do not need one, so they can run in CI and catch the regressions that CI
// can catch. The fake Lightroom below mirrors two behaviours learned the hard
// way on 10.0: getValue() returns null for a parameter sitting at its default,
// and setImageSettingsXMP() replaces the whole settings set.
//
//   node --test test/

import test from 'node:test';
import assert from 'node:assert/strict';

import { commands, restoreBaselines, UNSUPPORTED, usesClassicBridge } from '../adapter.mjs';

const PARAMS = ['Exposure2012', 'Contrast2012', 'Clarity2012', 'Temperature', 'WhiteBalance', 'LocalExposure2012'];
const DEFAULTS = {
  Exposure2012: 0,
  Contrast2012: 0,
  Clarity2012: 0,
  Temperature: 6500,
  WhiteBalance: 'As Shot',
};

function createFakeLr(initial = {}) {
  const state = { ...initial };
  let lastXmp = null;
  return {
    version: '10.0',
    lastXmp: () => lastXmp,
    state: () => ({ ...state }),
    async params() {
      return [PARAMS];
    },
    async get(name) {
      const value = state[name];
      if (value === undefined) return null;
      // Real Lightroom reports a parameter at its default as null ("unset").
      if (DEFAULTS[name] !== undefined && value === DEFAULTS[name]) return null;
      return value;
    },
    async request(message, params = []) {
      if (message === 'getDefault') return [DEFAULTS[params[0]] ?? null];
      throw new Error(`unexpected controller message ${message}`);
    },
    async range(name) {
      return name === 'Exposure2012' || name === 'Temperature' ? [-5, 5, 2, 0.1, 0.33] : [-100, 100, 0, 10, 20];
    },
    async applyXmp(xml) {
      lastXmp = xml;
      const next = {};
      for (const match of xml.matchAll(/crs:([A-Za-z0-9]+)="([^"]*)"/g)) {
        const [, name, raw] = match;
        const numeric = Number(raw);
        next[name] = raw.trim() !== '' && Number.isFinite(numeric) ? numeric : raw;
      }
      // Whole-state replacement: anything the document omits goes back to default.
      for (const key of Object.keys(state)) delete state[key];
      Object.assign(state, next);
    },
  };
}

function createSession(lr) {
  return {
    defaults: new Map(),
    baselines: new Map(),
    names: {
      global: PARAMS.filter((n) => !n.startsWith('Local')),
      local: ['LocalExposure2012'],
    },
    async ensure() {},
    async withLr(fn) {
      return fn(lr);
    },
  };
}

function setup(initial) {
  const lr = createFakeLr(initial);
  return { lr, session: createSession(lr) };
}

test('probe_client satisfies the lr-ops CatalogBinding contract', async () => {
  const { session } = setup({});
  const probe = await commands.probe_client({}, session);

  assert.equal(probe.access_mode, 'full');
  assert.equal(probe.catalog_authorized, false);
  assert.equal(probe.capabilities.develop_apply, false);
  assert.equal(probe.capabilities.photo_identity_bound, false);
  assert.equal(probe.capabilities.expected_access_mode, true);
  assert.equal(probe.capabilities.expected_catalog_identity, true);
  assert.match(probe.build_id, /lightroom-cloud-adapter/);
  assert.equal(probe.lr_version, '10.0');
  assert.equal(probe.parameter_count, 5);
  assert.equal(probe.local_parameter_count, 1);
});

test('get_selection returns the open photo under a stable handle', async () => {
  const { session } = setup({});
  const selection = await commands.get_selection({}, session);
  assert.equal(selection.target.uuid, 'cloud:active');
  assert.equal(selection.target.is_virtual_copy, false);
});

test('get_develop_state normalises "null means default"', async () => {
  const { session } = setup({ Exposure2012: 0.35 });
  const state = await commands.get_develop_state({}, session);
  // Not in the fake state at all: read as its default, not dropped.
  assert.equal(state.settings.Contrast2012, 0);
  assert.equal(state.settings.Exposure2012, 0.35);
  assert.equal(state.settings.WhiteBalance, 'As Shot');
});

test('writes are refused and leave the open photo unchanged', async () => {
  const { lr, session } = setup({ Exposure2012: -0.5, Contrast2012: 15 });
  const before = lr.state();

  const applied = await commands.apply_candidate(
    { photo_uuid: 'cloud:active', settings: { Exposure2012: 0.35, Clarity2012: 20 } },
    session,
  );
  const reset = await commands.reset_to_baseline({ photo_uuid: 'cloud:active' }, session);
  const restored = await restoreBaselines(session);

  assert.equal(applied.ok, false);
  assert.equal(applied.code, 'photo_identity_unbound');
  assert.equal(reset.code, 'photo_identity_unbound');
  assert.equal(restored.restored, false);
  assert.deepEqual(lr.state(), before);
  assert.equal(lr.lastXmp(), null);
});

test('a batch stops when a write is refused', async () => {
  const { lr, session } = setup({ Exposure2012: -0.5 });
  const batch = await commands.command_batch(
    {
      commands: [
        { command: 'get_selection', params: {} },
        { command: 'apply_candidate', params: { settings: { Exposure2012: 0.2 } } },
        { command: 'get_selection', params: {} },
      ],
    },
    session,
  );
  assert.equal(batch.ok, false);
  assert.equal(batch.failed_at_index, 1);
  assert.equal(batch.results.length, 2);
  assert.equal(lr.state().Exposure2012, -0.5);
});

test('the Classic bridge port is rejected', () => {
  assert.equal(usesClassicBridge('http://127.0.0.1:17777'), true);
  assert.equal(usesClassicBridge('http://127.0.0.1:17778'), false);
});

test('command_batch runs in order and reports where a batch stopped', async () => {
  const { session } = setup({ Exposure2012: -0.5 });

  const ok = await commands.command_batch(
    {
      commands: [
        { command: 'get_develop_state', params: { photo_uuid: 'cloud:active' } },
        { command: 'capture_baseline', params: { photo_uuid: 'cloud:active' } },
      ],
    },
    session,
  );
  assert.equal(ok.results.length, 2);
  assert.ok(ok.results.every((step) => step.ok));
  assert.equal(ok.failed_at_index, undefined);

  const partial = await commands.command_batch(
    {
      commands: [
        { command: 'get_selection', params: {} },
        { command: 'not_a_command', params: {} },
        { command: 'get_selection', params: {} },
      ],
    },
    session,
  );
  assert.equal(partial.ok, false);
  assert.equal(partial.failed_at_index, 1);
  assert.equal(partial.results_so_far.length, 2);
  assert.match(partial.error, /unsupported_command: not_a_command/);
});

test('cloud-impossible commands carry a human reason', () => {
  assert.match(UNSUPPORTED.create_virtual_copy, /virtual copies/i);
  assert.match(UNSUPPORTED.update_xmp_sidecar, /sidecar/i);
  assert.match(UNSUPPORTED.export_preview, /export/i);
});

test('restoreBaselines never writes', async () => {
  const { lr, session } = setup({ Exposure2012: -0.54, Contrast2012: 15 });
  await commands.capture_baseline({ photo_uuid: 'cloud:active' }, session);
  const out = await restoreBaselines(session);
  assert.equal(out.restored, false);
  assert.match(out.reason, /photo_identity_unbound/);
  assert.equal(lr.state().Exposure2012, -0.54);
});
