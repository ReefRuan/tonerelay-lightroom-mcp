#!/usr/bin/env node
// Acceptance test for the Lightroom cloud adapter.
//
// Talks to a running `lr_ops serve` (LR_OPS_URL) and asserts the commands the
// ToneRelay engine depends on. It captures the photo's real develop settings
// first and always writes them back at the end, so a run is safe on a real
// library — even if an assertion fails midway.
//
//   node verify.mjs
//   LR_OPS_URL=http://127.0.0.1:17778 node verify.mjs

const SERVICE = new URL(process.env.LR_OPS_URL ?? 'http://127.0.0.1:17778');
const TOKEN = process.env.LR_XMP_MIMIC_LOCAL_TOKEN ?? '';
const PHOTO = 'cloud:active';

let failures = 0;
let passes = 0;

function check(label, condition, detail) {
  if (condition) {
    passes += 1;
    console.log(`  ok   ${label}`);
  } else {
    failures += 1;
    console.log(`  FAIL ${label}${detail ? ` — ${detail}` : ''}`);
  }
}

function headers() {
  const h = { 'Content-Type': 'application/json' };
  if (TOKEN) h.Authorization = `Bearer ${TOKEN}`;
  return h;
}

async function submit(command, params = {}) {
  const res = await fetch(new URL('/jobs', SERVICE), {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ command, params }),
  });
  if (!res.ok) throw new Error(`POST /jobs -> ${res.status} ${await res.text()}`);
  const { job_id } = await res.json();

  const deadline = Date.now() + 120_000;
  for (;;) {
    const job = await (await fetch(new URL(`/jobs/${job_id}`, SERVICE), { headers: headers() })).json();
    if (job.status === 'done' || job.status === 'failed') {
      const result = job.result ?? job;
      return { ok: result.ok === true, data: result.data, error: result.error, raw: result };
    }
    if (Date.now() > deadline) throw new Error(`job ${job_id} (${command}) timed out`);
    await new Promise((r) => setTimeout(r, 200));
  }
}

const state = async () => {
  const out = await submit('get_develop_state', { photo_uuid: PHOTO });
  if (!out.ok) throw new Error(`get_develop_state failed: ${out.error}`);
  return out.data.settings;
};

const close = (a, b, tolerance = 1e-6) =>
  typeof a === 'number' && typeof b === 'number'
    ? Math.abs(a - b) <= tolerance
    : String(a) === String(b);

async function main() {
  console.log(`service ${SERVICE.href}`);

  console.log('\n1. probe_client');
  const probe = await submit('probe_client');
  check('probe ok', probe.ok, probe.error);
  const p = probe.data ?? {};
  check('adapter build id', String(p.build_id ?? '').includes('lightroom-cloud-adapter'), p.build_id);
  check('catalog binding', p.access_mode === 'full' && p.catalog_authorized === true, `${p.access_mode}/${p.catalog_authorized}`);
  check('develop_apply capability', p.capabilities?.develop_apply === true);
  check('parameter count > 140', (p.parameter_count ?? 0) > 140, String(p.parameter_count));
  console.log(`       Lightroom ${p.lr_version}, ${p.parameter_count} global + ${p.local_parameter_count} local params`);

  console.log('\n2. get_selection');
  const selection = await submit('get_selection');
  check('selection ok', selection.ok, selection.error);
  check('target uuid', selection.data?.target?.uuid === PHOTO, selection.data?.target?.uuid);

  console.log('\n3. get_develop_state');
  const original = await state();
  // Parameters that sit at their default with no getDefault() answer come back
  // unset and are omitted — 99 of 163 on a fresh photo, not an error.
  check(
    'settings returned',
    Object.keys(original).length > 60 && 'Exposure2012' in original,
    `${Object.keys(original).length} keys`,
  );

  // The photo is left exactly as found, whatever happens below.
  const originalExposure = original.Exposure2012 ?? 0;
  const originalClarity = original.Clarity2012 ?? 0;
  let captured = false;

  try {
    console.log('\n4. capture_baseline');
    const baseline = await submit('capture_baseline', { photo_uuid: PHOTO });
    captured = baseline.ok;
    check('baseline ok', baseline.ok, baseline.error);

    console.log('\n5. apply_candidate (write + readback)');
    const target = {
      Exposure2012: Math.round((originalExposure + 0.5) * 100) / 100,
      Clarity2012: Math.min(100, originalClarity + 10),
    };
    const applied = await submit('apply_candidate', { photo_uuid: PHOTO, settings: target });
    check('status applied', applied.data?.status === 'applied', applied.data?.status ?? applied.error);
    check('no mismatched params', (applied.data?.mismatched ?? []).length === 0, JSON.stringify(applied.data?.mismatched));
    check('no develop drift', (applied.data?.drift ?? []).length === 0, JSON.stringify(applied.data?.drift));
    const after = await state();
    check(
      'live readback matches request',
      close(after.Exposure2012, target.Exposure2012) && close(after.Clarity2012, target.Clarity2012),
      `Exposure ${after.Exposure2012} vs ${target.Exposure2012}, Clarity ${after.Clarity2012} vs ${target.Clarity2012}`,
    );
    check('untouched params unchanged', close(after.Contrast2012, original.Contrast2012), `${after.Contrast2012} vs ${original.Contrast2012}`);

    console.log('\n6. in-place guard');
    const guarded = await submit('apply_candidate', {
      photo_uuid: PHOTO,
      settings: { Contrast2012: 5 },
      require_in_place_guard: true,
      expected_settings_snapshot: { ...original, Exposure2012: (originalExposure ?? 0) + 9 },
    });
    check('stale snapshot rejected', guarded.data?.status === 'conflict' && guarded.data?.code === 'settings_changed', guarded.data?.status);
    check('guard wrote nothing', close((await state()).Contrast2012, original.Contrast2012));

    console.log('\n7. reset_to_baseline');
    const reset = await submit('reset_to_baseline', { photo_uuid: PHOTO });
    check('reset ok', reset.ok, reset.error);
    const restored = await state();
    check('exposure restored', close(restored.Exposure2012, originalExposure), `${restored.Exposure2012} vs ${originalExposure}`);
    check('clarity restored', close(restored.Clarity2012, originalClarity), `${restored.Clarity2012} vs ${originalClarity}`);

    console.log('\n8. command_batch');
    const batch = await submit('command_batch', {
      commands: [
        { command: 'get_develop_state', params: { photo_uuid: PHOTO } },
        { command: 'capture_baseline', params: { photo_uuid: PHOTO } },
      ],
    });
    check('batch ok', batch.ok && (batch.data?.results ?? []).length === 2, batch.error);
    check('batch steps ok', (batch.data?.results ?? []).every((r) => r.ok === true));

    console.log('\n9. unsupported commands explain themselves');
    const mkcopy = await submit('create_virtual_copy', { photo_uuid: PHOTO });
    check('create_virtual_copy refused', mkcopy.ok === false, 'expected failure');
    check('reason mentions virtual copies', /virtual cop/i.test(mkcopy.error ?? ''), mkcopy.error);
  } finally {
    console.log('\n10. restore original settings');
    const back = await submit('apply_candidate', {
      photo_uuid: PHOTO,
      settings: { Exposure2012: originalExposure, Clarity2012: originalClarity },
    });
    const finalState = await state();
    const ok = close(finalState.Exposure2012, originalExposure) && close(finalState.Clarity2012, originalClarity);
    check('photo left as found', ok, `Exposure ${finalState.Exposure2012}, Clarity ${finalState.Clarity2012}`);
    if (!ok && !back.ok) console.log(`       restore job failed: ${back.error}`);
    if (!captured) console.log('       (baseline was never captured; restored by explicit values)');
  }

  console.log(`\n${passes} passed, ${failures} failed`);
  process.exit(failures === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error(`verify failed: ${err.message}`);
  process.exit(1);
});
