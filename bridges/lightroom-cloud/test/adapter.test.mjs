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

import { commands, restoreBaselines, UNSUPPORTED } from '../adapter.mjs';

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
  assert.equal(probe.catalog_authorized, true);
  assert.ok(probe.catalog_identity.length > 0);
  assert.equal(probe.capabilities.expected_access_mode, true);
  assert.equal(probe.capabilities.expected_catalog_identity, true);
  assert.equal(probe.capabilities.develop_apply, true);
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

test('apply_candidate writes and verifies in one round trip', async () => {
  const { lr, session } = setup({ Exposure2012: -0.5, Contrast2012: 15 });
  const result = await commands.apply_candidate(
    { photo_uuid: 'cloud:active', settings: { Exposure2012: 0.35, Clarity2012: 20 } },
    session,
  );

  assert.equal(result.status, 'applied');
  assert.equal(result.applied, true);
  assert.deepEqual(result.mismatched, []);
  assert.deepEqual(result.changed.Exposure2012, { requested: 0.35, readback: 0.35 });
  assert.equal(lr.state().Clarity2012, 20);
  // Untouched parameters survive the whole-document rewrite.
  assert.equal(lr.state().Contrast2012, 15);
});

test('the XMP document prefixes every attribute with crs:', async () => {
  // Regression: unprefixed attributes make Lightroom ignore the whole document
  // while still reporting success.
  const { lr, session } = setup({ Exposure2012: -0.5 });
  await commands.apply_candidate({ settings: { Exposure2012: 0.1 } }, session);

  const xml = lr.lastXmp();
  const attributes = [...xml.matchAll(/<rdf:Description[\s\S]*?\/>/g)][0][0].match(/[A-Za-z0-9.:]+="/g) ?? [];
  const develop = attributes.filter((a) => !a.startsWith('xmlns') && !a.startsWith('rdf:'));
  assert.ok(develop.length > 0, 'document carries develop attributes');
  for (const attribute of develop) {
    assert.ok(attribute.startsWith('crs:'), `attribute not namespaced: ${attribute}`);
  }
});

test('apply_candidate rejects unknown, local and out-of-range settings', async () => {
  const { session } = setup({});

  const unknown = await commands.apply_candidate({ settings: { Nope2012: 5 } }, session);
  assert.equal(unknown.status, 'conflict');
  assert.equal(unknown.code, 'unsupported_settings');
  assert.deepEqual(unknown.rejected, [['Nope2012', 'unknown_parameter']]);

  const local = await commands.apply_candidate({ settings: { LocalExposure2012: 0.2 } }, session);
  assert.equal(local.code, 'unsupported_settings');
  assert.deepEqual(local.rejected, [['LocalExposure2012', 'local_mask_setting_not_supported']]);

  const range = await commands.apply_candidate({ settings: { Exposure2012: 99 } }, session);
  assert.equal(range.code, 'unsupported_settings');
  assert.match(range.rejected[0][1], /out_of_range/);
});

test('the in-place guard refuses a stale snapshot and writes nothing', async () => {
  const { lr, session } = setup({ Exposure2012: -0.5, Contrast2012: 15 });
  const guarded = await commands.apply_candidate(
    {
      settings: { Contrast2012: 5 },
      require_in_place_guard: true,
      expected_settings_snapshot: { Exposure2012: 9, Contrast2012: 15 },
    },
    session,
  );

  assert.equal(guarded.status, 'conflict');
  assert.equal(guarded.code, 'settings_changed');
  assert.equal(lr.state().Contrast2012, 15);
});

test('reset_to_baseline restores the captured set and explains a missing one', async () => {
  const { lr, session } = setup({ Exposure2012: -0.54, Contrast2012: 15, Clarity2012: 10 });

  await assert.rejects(
    () => commands.reset_to_baseline({ photo_uuid: 'cloud:active' }, session),
    /No captured baseline for this photo/,
  );

  const baseline = await commands.capture_baseline({ photo_uuid: 'cloud:active' }, session);
  assert.equal(baseline.baseline.Clarity2012, 10);

  await commands.apply_candidate({ settings: { Exposure2012: 0.8, Clarity2012: 25 } }, session);
  const reset = await commands.reset_to_baseline({ photo_uuid: 'cloud:active' }, session);

  assert.ok(reset.restored > 0);
  assert.equal(lr.state().Exposure2012, -0.54);
  assert.equal(lr.state().Clarity2012, 10);
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

test('restoreBaselines puts the photo back and is idempotent', async () => {
  const { lr, session } = setup({ Exposure2012: -0.54, Contrast2012: 15, Clarity2012: 10 });
  await commands.capture_baseline({ photo_uuid: 'cloud:active' }, session);

  // Nothing changed yet: the helper must not touch the photo.
  const noop = await restoreBaselines(session);
  assert.equal(noop.restored, false);
  assert.match(noop.reason, /already matches/);

  await commands.apply_candidate({ settings: { Exposure2012: 0.8, Clarity2012: 25 } }, session);
  const restored = await restoreBaselines(session);

  assert.equal(restored.restored, true);
  assert.equal(lr.state().Exposure2012, -0.54);
  assert.equal(lr.state().Clarity2012, 10);
  assert.equal(lr.state().Contrast2012, 15);
});

test('restoreBaselines explains when there is nothing to restore', async () => {
  const { session } = setup({});
  const out = await restoreBaselines(session);
  assert.equal(out.restored, false);
  assert.match(out.reason, /no baseline/);
});
