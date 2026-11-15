// The adapter's exit path, without Lightroom: a fake job queue answers "no
// jobs", then the process is asked to stop. Guards two things that are easy to
// break silently — that the signal is handled at all, and that the log lines
// written while stopping actually survive process exit (piped stderr is
// truncated by process.exit unless writes are synchronous).
//
//   node --test test/

import test from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createServer } from 'node:http';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ADAPTER = join(dirname(fileURLToPath(import.meta.url)), '..', 'adapter.mjs');

function fakeQueue() {
  const server = createServer((req, res) => {
    if (req.method === 'GET' && req.url === '/jobs/next') {
      res.writeHead(204).end();
      return;
    }
    res.writeHead(200, { 'Content-Type': 'application/json' }).end('{}');
  });
  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({ server, url: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

function runAdapter(url) {
  const child = spawn(process.execPath, [ADAPTER], {
    env: { ...process.env, LR_OPS_URL: url, LR_CLOUD_POLL_MS: '50', LR_CLOUD_RESTORE_ON_EXIT: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  child.stderr.on('data', (chunk) => {
    stderr += chunk;
  });
  const exited = new Promise((resolve) => child.on('exit', (code) => resolve(code)));
  return { child, exited, stderr: () => stderr };
}

test('SIGTERM stops the loop cleanly and keeps the shutdown log', async () => {
  const { server, url } = await fakeQueue();
  const { child, exited, stderr } = runAdapter(url);

  try {
    // Let it connect and enter the poll loop.
    await new Promise((r) => setTimeout(r, 600));
    child.kill('SIGTERM');

    let timer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error('adapter did not exit')), 10_000);
    });
    const code = await Promise.race([exited, timeout]);
    clearTimeout(timer);

    assert.equal(code, 0);
    assert.match(stderr(), /SIGTERM — shutting down/);
  } finally {
    if (child.exitCode === null) child.kill('SIGKILL');
    server.close();
  }
});

test('an unreachable queue does not kill the adapter', async () => {
  const { child, exited, stderr } = runAdapter('http://127.0.0.1:9');

  try {
    await new Promise((r) => setTimeout(r, 700));
    assert.equal(child.exitCode, null, 'still running');
    assert.match(stderr(), /service unreachable/);
    child.kill('SIGTERM');
    const code = await exited;
    assert.equal(code, 0);
  } finally {
    if (child.exitCode === null) child.kill('SIGKILL');
  }
});
