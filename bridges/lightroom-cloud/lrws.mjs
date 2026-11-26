// Minimal client for Adobe Lightroom desktop's (undocumented) External
// Controller API: a loopback WebSocket at ws://127.0.0.1:7682.
//
// Requires Preferences > Interface > "Enable external controllers" + restart.
// The port Lightroom actually bound is in
//   ~/Library/Application Support/Adobe/Lightroom CC/Connections/connections.json
// First register() shows a Pair dialog inside Lightroom; the granted GUID is
// cached next to this file so later runs pair silently.

import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { randomUUID } from 'node:crypto';

const HERE = dirname(fileURLToPath(import.meta.url));
const GUID_FILE = process.env.LR_CLIENT_ID_FILE || join(HERE, '.lr-client.json');
const CONNECTIONS = join(homedir(), 'Library/Application Support/Adobe/Lightroom CC/Connections/connections.json');

export const APP_NAME = 'tonerelay-cloud';
export const APP_VERSION = '0.1.0';

function port() {
  const override = process.env.LR_CLOUD_WS_URL;
  if (override) return Number(new URL(override).port) || 7682;
  try {
    return JSON.parse(readFileSync(CONNECTIONS, 'utf8')).port || 7682;
  } catch {
    return Number(process.env.LR_PORT) || 7682;
  }
}

function cachedGuid() {
  try {
    return JSON.parse(readFileSync(GUID_FILE, 'utf8')).guid || null;
  } catch {
    return null;
  }
}

function rememberGuid(guid) {
  try {
    mkdirSync(HERE, { recursive: true });
    writeFileSync(GUID_FILE, JSON.stringify({ guid }));
  } catch {
    /* read-only sandbox: pairing prompt will reappear, not fatal */
  }
}

export const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Open the socket and register. Resolves to a client object. */
export async function connect({ name = APP_NAME, version = APP_VERSION, registerTimeoutMs = 90_000 } = {}) {
  const p = port();
  const ws = new WebSocket(`ws://127.0.0.1:${p}`);

  const pending = new Map();
  const observers = [];

  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`connect timeout to ws://127.0.0.1:${p}`)), 10_000);
    ws.addEventListener('open', () => {
      clearTimeout(timer);
      resolve();
    });
    ws.addEventListener('error', () =>
      reject(
        new Error(
          `websocket error on ws://127.0.0.1:${p} — is Lightroom running with external controllers enabled?`,
        ),
      ),
    );
  });

  ws.addEventListener('message', (event) => {
    let msg;
    try {
      msg = JSON.parse(event.data);
    } catch {
      return;
    }
    const entry = pending.get(msg.requestId);
    if (entry) {
      pending.delete(msg.requestId);
      entry.resolve(msg);
      return;
    }
    for (const fn of observers) fn(msg);
  });

  ws.addEventListener('close', () => {
    for (const [, entry] of pending) entry.reject(new Error('websocket closed'));
    pending.clear();
  });

  /** Send one request; resolves to the full response message. */
  function call(message, params = [], object = null, timeoutMs = 30_000) {
    const requestId = randomUUID();
    const payload = JSON.stringify({ requestId, object, message, params });
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        pending.delete(requestId);
        reject(new Error(`${message} timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      pending.set(requestId, {
        resolve: (msg) => {
          clearTimeout(timer);
          resolve(msg);
        },
        reject: (err) => {
          clearTimeout(timer);
          reject(err);
        },
      });
      ws.send(payload);
    });
  }

  /** Send one request, return `response` or throw on success:false. */
  async function request(message, params = [], timeoutMs = 30_000) {
    const msg = await call(message, params, null, timeoutMs);
    if (!msg.success) throw new Error(`${message} failed: ${JSON.stringify(msg.response)}`);
    return msg.response;
  }

  const guid = cachedGuid();
  let registered;
  try {
    registered = await call('register', [name, version, guid], null, registerTimeoutMs);
  } catch (err) {
    ws.close();
    throw err;
  }
  if (!registered.success || !Array.isArray(registered.response)) {
    ws.close();
    throw new Error(`register failed: ${JSON.stringify(registered.response)}`);
  }
  const [clientGuid] = registered.response;
  if (clientGuid && clientGuid !== guid) rememberGuid(clientGuid);

  const client = {
    guid: clientGuid,
    version: registered.response[1],
    call,
    request,
    close: () => ws.close(),
    /** Numeric develop parameter, in controller units. */
    async get(param) {
      const r = await request('getValue', [param]);
      return r[0];
    },
    async set(param, value) {
      await request('setValue', [param, value]);
      return value;
    },
    async range(param) {
      const r = await request('getRange', [param]);
      return r;
    },
    async params() {
      const r = await request('getParameterNames');
      return r;
    },
    /** Apply a develop-settings XMP document (string) to the open photo. */
    async applyXmp(xml) {
      await request('setImageSettingsXMP', [xml], 60_000);
    },
    on(fn) {
      observers.push(fn);
    },
  };
  return client;
}
