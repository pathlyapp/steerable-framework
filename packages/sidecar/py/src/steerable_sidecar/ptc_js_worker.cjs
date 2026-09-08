// ptc_js_worker — persistent Node worker for conversational JS PTC (run_js/wait_js).
//
// The Python sidecar spawns this process once and speaks one JSON object per
// line on stdin/stdout (the same framing as run_code's driver). Each line is
// a frame with "v": 1. This process must not hold provider credentials: the
// parent scrubs the environment to an allowlist before spawning, and usually
// wraps the process in the layer-2 OS sandbox (Seatbelt/bwrap/Landlock,
// network off). node:vm is defense in depth on top of that boundary, not the
// boundary itself.
//
// Frame protocol
//   in  {type:"exec", cellId, sessionId, code, yieldTimeMs, maxOutputChars,
//        maxCellTimeMs, maxToolCalls, tools: [{name, description}]}
//   in  {type:"wait", cellId, yieldTimeMs, maxOutputChars, terminate?}
//   in  {type:"close_session", sessionId}
//   in  {type:"tool_result", callId, ok, value?, error?}   (value is a JSON string)
//   in  {type:"shutdown"}
//   out {type:"ready"}
//   out {type:"tool_call", cellId, callId, tool, arguments}
//   out {type:"cell_yield", cellId, output, logs, truncated}
//   out {type:"cell_done", cellId, ok, value?, error?, output, logs, truncated, terminated?}
//   out {type:"log", text}
//
// A cell is one exec's running JS (an async-function body, so `await` and
// `return` work). Cells keep running after a yield; `wait` returns the output
// accumulated since the last response, or the terminal result. Session KV
// (store/load) lives in this process's memory and is shared by every cell of
// the session.

'use strict';

const vm = require('node:vm');

const PROTOCOL_VERSION = 1;

function envInt(name, fallback) {
  const raw = (process.env[name] || '').trim();
  if (!raw) return fallback;
  const parsed = Number.parseInt(raw, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

// Sessions idle longer than this are dropped (their cells terminated, the KV
// discarded). The sidecar has no chat-end signal, so idle reaping is the
// teardown path; the cap bounds total live sessions (LRU).
const SESSION_TTL_MS = envInt('STEERABLE_PTC_JS_SESSION_TTL_MS', 30 * 60 * 1000);
const MAX_SESSIONS = envInt('STEERABLE_PTC_JS_MAX_SESSIONS', 32);
const REAPER_INTERVAL_MS = Math.min(SESSION_TTL_MS, 60 * 1000);

const DEFAULT_YIELD_TIME_MS = 10_000;
const DEFAULT_MAX_OUTPUT_CHARS = 40_000;
const DEFAULT_MAX_CELL_TIME_MS = 10 * 60 * 1000;
const DEFAULT_MAX_TOOL_CALLS = 32;

// ---------------------------------------------------------------------------
// stdio framing
// ---------------------------------------------------------------------------

function send(frame) {
  process.stdout.write(JSON.stringify({ v: PROTOCOL_VERSION, ...frame }) + '\n');
}

function workerLog(text) {
  send({ type: 'log', text: String(text) });
}

// ---------------------------------------------------------------------------
// Sessions and cells
// ---------------------------------------------------------------------------

/** @type {Map<string, {store: Map<string, string>, cells: Set<string>, lastTouched: number}> */
const sessions = new Map();
/** @type {Map<string, Cell>} */
const cells = new Map();

class Cell {
  constructor(id, sessionId, limits) {
    this.id = id;
    this.sessionId = sessionId;
    this.output = [];
    this.logs = [];
    this.outputChars = 0;
    this.done = false;
    this.terminated = false;
    this.pendingToolCalls = new Map(); // callId -> {resolve, reject}
    this.nextCallId = 0;
    this.maxToolCalls = limits.maxToolCalls;
    this.timers = new Set(); // setTimeout ids the cell created
    this.lifetimeTimer = null;
    // The one outstanding exec/wait response. The worker answers at most one
    // request per cell at a time: the sidecar serializes exec -> wait -> wait.
    this.pendingResponse = null; // {timer, maxOutputChars}
  }
}

function touchSession(sessionId) {
  const session = sessions.get(sessionId);
  if (session) {
    session.lastTouched = Date.now();
    // Refresh LRU order.
    sessions.delete(sessionId);
    sessions.set(sessionId, session);
  }
  return session;
}

function getOrCreateSession(sessionId) {
  const existing = touchSession(sessionId);
  if (existing) return existing;
  const session = { store: new Map(), cells: new Set(), lastTouched: Date.now() };
  sessions.set(sessionId, session);
  while (sessions.size > MAX_SESSIONS) {
    const oldest = sessions.keys().next().value;
    closeSession(oldest);
  }
  return session;
}

function closeSession(sessionId) {
  const session = sessions.get(sessionId);
  if (!session) return;
  for (const cellId of [...session.cells]) {
    terminateCell(cellId, 'session closed');
  }
  sessions.delete(sessionId);
}

function reapIdleSessions() {
  const now = Date.now();
  for (const [sessionId, session] of [...sessions]) {
    if (now - session.lastTouched > SESSION_TTL_MS) {
      workerLog(`session ${sessionId} idle for >${SESSION_TTL_MS}ms; closing`);
      closeSession(sessionId);
    }
  }
}

// ---------------------------------------------------------------------------
// Cell responses: yield/done framing with per-response output budget
// ---------------------------------------------------------------------------

function takeOutput(cell, maxOutputChars) {
  let chars = 0;
  let truncated = false;
  const output = [];
  for (const item of cell.output) {
    if (chars + item.length > maxOutputChars) {
      const rest = item.slice(0, Math.max(0, maxOutputChars - chars));
      if (rest) output.push(rest);
      truncated = true;
      break;
    }
    output.push(item);
    chars += item.length;
  }
  if (truncated) output.push(`[output truncated at ${maxOutputChars} chars]`);
  const logs = cell.logs.splice(0);
  cell.output = [];
  return { output, logs, truncated };
}

function respondYield(cell) {
  const pending = cell.pendingResponse;
  if (!pending) return; // no exec/wait outstanding: output keeps accumulating
  clearTimeout(pending.timer);
  cell.pendingResponse = null;
  const { output, logs, truncated } = takeOutput(cell, pending.maxOutputChars);
  send({ type: 'cell_yield', cellId: cell.id, output, logs, truncated });
}

function respondDone(cell) {
  const pending = cell.pendingResponse;
  if (pending) {
    clearTimeout(pending.timer);
    cell.pendingResponse = null;
  }
  const maxOutputChars = pending ? pending.maxOutputChars : DEFAULT_MAX_OUTPUT_CHARS;
  const { output, logs, truncated } = takeOutput(cell, maxOutputChars);
  send({
    type: 'cell_done',
    cellId: cell.id,
    ok: cell.error == null,
    value: cell.serializedValue === undefined ? null : cell.serializedValue,
    error: cell.error,
    output,
    logs,
    truncated,
    terminated: cell.terminated || undefined,
  });
  // Terminal: the cell is gone from the runtime's point of view. A later wait
  // on this id answers "unknown cell" (codex closes finished cells on read).
  cells.delete(cell.id);
  const session = sessions.get(cell.sessionId);
  if (session) session.cells.delete(cell.id);
}

function finishCell(cell, serializedValue, errorText) {
  if (cell.done) return;
  cell.done = true;
  cell.serializedValue = serializedValue;
  cell.error = errorText == null ? null : String(errorText);
  if (cell.lifetimeTimer) clearTimeout(cell.lifetimeTimer);
  for (const timer of cell.timers) clearTimeout(timer);
  cell.timers.clear();
  for (const { reject } of cell.pendingToolCalls.values()) {
    reject(new Error('cell finished with a tool call in flight'));
  }
  cell.pendingToolCalls.clear();
  respondDone(cell);
}

function terminateCell(cellId, reason) {
  const cell = cells.get(cellId);
  if (!cell || cell.done) return;
  cell.terminated = true;
  for (const { reject } of cell.pendingToolCalls.values()) {
    reject(new Error(`cell terminated: ${reason}`));
  }
  cell.pendingToolCalls.clear();
  finishCell(cell, undefined, `terminated: ${reason}`);
}

function armResponse(cell, yieldTimeMs, maxOutputChars) {
  // The sidecar serializes one outstanding request per cell; a second arm
  // means the first responder was orphaned (e.g. its wait was cancelled
  // Python-side) — flush it as a yield before replacing it.
  if (cell.pendingResponse) respondYield(cell);
  cell.pendingResponse = {
    maxOutputChars: maxOutputChars || DEFAULT_MAX_OUTPUT_CHARS,
    timer: setTimeout(() => {
      if (!cell.done) respondYield(cell);
    }, yieldTimeMs > 0 ? yieldTimeMs : DEFAULT_YIELD_TIME_MS),
  };
}

// ---------------------------------------------------------------------------
// Tool bridge: tools.<name>(args) -> Promise, resolved by the sidecar
// ---------------------------------------------------------------------------

function hostCallTool(cell, name, argsJson) {
  return new Promise((resolve, reject) => {
    if (cell.done || cell.terminated) {
      reject(new Error('cell is no longer running'));
      return;
    }
    if (name === 'run_js' || name === 'wait_js') {
      reject(new Error(`nested ${name} is not allowed`));
      return;
    }
    if (cell.nextCallId >= cell.maxToolCalls) {
      reject(new Error(`cell exceeded ${cell.maxToolCalls} nested tool calls`));
      return;
    }
    const callId = `${cell.id}-${++cell.nextCallId}`;
    let args;
    try {
      args = JSON.parse(argsJson);
    } catch (err) {
      reject(new Error(`tool arguments must be JSON-serializable: ${err.message}`));
      return;
    }
    cell.pendingToolCalls.set(callId, { resolve, reject });
    send({ type: 'tool_call', cellId: cell.id, callId, tool: name, arguments: args });
  });
}

function onToolResult(frame) {
  // Tool results route by callId alone: callIds embed their cell id and a
  // per-cell counter, so they are unique across cells and sessions.
  const callId = String(frame.callId || '');
  const sep = callId.lastIndexOf('-');
  const cell = cells.get(callId.slice(0, sep));
  if (!cell) return;
  const pending = cell.pendingToolCalls.get(callId);
  if (!pending) return;
  cell.pendingToolCalls.delete(callId);
  if (frame.ok) {
    pending.resolve(frame.value == null ? 'null' : String(frame.value));
  } else {
    pending.reject(new Error(String(frame.error || 'tool failed')));
  }
}

// ---------------------------------------------------------------------------
// Cell globals. Realm discipline: host functions are only ever referenced
// from closures created inside the vm context; values cross the boundary as
// JSON strings or primitives, so context code can never reach a host-realm
// object (whose .constructor chain would escape the context).
// ---------------------------------------------------------------------------

function buildContext(cell, toolsMetaJson) {
  const session = sessions.get(cell.sessionId);

  const host = {
    callTool: (name, argsJson) => hostCallTool(cell, String(name), String(argsJson)),
    store: (key, valueJson) => {
      session.store.set(String(key), String(valueJson));
    },
    load: (key) => {
      const value = session.store.get(String(key));
      return value === undefined ? undefined : value;
    },
    appendOutput: (text) => {
      cell.output.push(String(text));
    },
    appendLog: (text) => {
      cell.logs.push(String(text));
    },
    yieldControl: () => {
      respondYield(cell);
    },
    setTimeout: (callback, delayMs) => {
      const timer = setTimeout(() => {
        cell.timers.delete(timer);
        if (!cell.done) callback();
      }, Math.max(0, Number(delayMs) || 0));
      cell.timers.add(timer);
      return timer;
    },
    clearTimeout: (timer) => {
      cell.timers.delete(timer);
      clearTimeout(timer);
    },
    toolsMetaJson,
  };

  const sandbox = { __host: host };
  const context = vm.createContext(sandbox, {
    codeGeneration: { strings: false, wasm: false },
  });

  // Installed by a bootstrap evaluated inside the context, so every global the
  // model code touches (tools, store, load, console, ...) is a context-realm
  // function/object whose constructor chain stays inside the context. The
  // host bridge object is deleted from the global before user code runs.
  const bootstrap = `
(() => {
  const host = globalThis.__host;
  delete globalThis.__host;

  class ToolCallError extends Error {
    constructor(message) {
      super(message);
      this.name = 'ToolCallError';
    }
  }

  const callTool = (name, args) => {
    let argsJson;
    try {
      argsJson = JSON.stringify(args === undefined ? {} : args);
    } catch (err) {
      return Promise.reject(new Error('tool arguments must be JSON-serializable: ' + err.message));
    }
    // Promise.resolve here is the context's own Promise, so the promise user
    // code receives is context-realm even though the host promise adopts it.
    // The host resolves with the result's JSON string (failures reject), so
    // the value user code sees is parsed inside the context — context-realm.
    return Promise.resolve(host.callTool(name, argsJson)).then((valueJson) => JSON.parse(valueJson));
  };

  const tools = new Proxy({}, {
    get: (_target, prop) => {
      if (prop === 'call') return callTool;
      if (typeof prop === 'string') return (args) => callTool(prop, args);
      return undefined;
    },
  });

  const stringify = (value) => {
    if (typeof value === 'string') return value;
    try {
      const json = JSON.stringify(value);
      return json === undefined ? String(value) : json;
    } catch {
      return String(value);
    }
  };

  globalThis.tools = tools;
  globalThis.ToolCallError = ToolCallError;
  globalThis.ALL_TOOLS = JSON.parse(host.toolsMetaJson);
  globalThis.store = (key, value) => host.store(key, JSON.stringify(value) ?? 'null');
  globalThis.load = (key) => {
    const json = host.load(key);
    return json === undefined ? undefined : JSON.parse(json);
  };
  globalThis.text = (value) => host.appendOutput(stringify(value));
  globalThis.notify = globalThis.text;
  globalThis.yield_control = () => host.yieldControl();
  globalThis.setTimeout = (callback, delayMs) => host.setTimeout(callback, delayMs);
  globalThis.clearTimeout = (timer) => host.clearTimeout(timer);
  globalThis.exit = () => {
    const err = new Error('exit');
    err.__cellExit = true;
    throw err;
  };
  const consoleMethod = (level) => (...args) =>
    host.appendLog('[' + level + '] ' + args.map(stringify).join(' '));
  globalThis.console = {
    log: consoleMethod('log'),
    info: consoleMethod('info'),
    warn: consoleMethod('warn'),
    error: consoleMethod('error'),
    debug: consoleMethod('debug'),
  };
})();`;

  vm.runInContext(bootstrap, context, { filename: 'ptc-js-bootstrap.js' });
  return context;
}

function runCellCode(cell, code, toolsMeta) {
  let context;
  try {
    context = buildContext(cell, JSON.stringify(toolsMeta || []));
  } catch (err) {
    finishCell(cell, undefined, `failed to set up the cell: ${err.message}`);
    return;
  }
  let promise;
  try {
    promise = vm.runInContext('(async () => {\n' + code + '\n})()', context, {
      filename: `ptc-js-cell-${cell.id}.js`,
    });
  } catch (err) {
    finishCell(cell, undefined, `${err.name}: ${err.message}`);
    return;
  }
  // The cell's return value is serialized inside the context (context-realm
  // JSON.stringify) so host code never touches a context-realm object.
  const serialize = vm.runInContext('(value) => JSON.stringify(value)', context);
  Promise.resolve(promise).then(
    (value) => {
      let serialized;
      try {
        serialized = serialize(value);
      } catch (err) {
        finishCell(cell, undefined, `return value is not JSON-serializable: ${err.message}`);
        return;
      }
      finishCell(cell, serialized === undefined ? undefined : serialized, null);
    },
    (err) => {
      if (err && err.__cellExit) {
        finishCell(cell, undefined, null);
        return;
      }
      const name = err && err.name ? err.name : 'Error';
      const message = err && err.message ? err.message : String(err);
      finishCell(cell, undefined, `${name}: ${message}`);
    },
  );
}

// ---------------------------------------------------------------------------
// Frame handlers
// ---------------------------------------------------------------------------

function onExec(frame) {
  const cellId = String(frame.cellId || '');
  const sessionId = String(frame.sessionId || 'default');
  const code = String(frame.code || '');
  if (!cellId || !code.trim()) {
    send({
      type: 'cell_done',
      cellId,
      ok: false,
      error: cellId ? 'code is empty' : 'cellId is empty',
      output: [],
      logs: [],
      truncated: false,
    });
    return;
  }
  if (cells.has(cellId)) {
    send({ type: 'cell_done', cellId, ok: false, error: 'duplicate cell id', output: [], logs: [], truncated: false });
    return;
  }
  getOrCreateSession(sessionId);
  const cell = new Cell(cellId, sessionId, {
    maxToolCalls: frame.maxToolCalls > 0 ? frame.maxToolCalls : DEFAULT_MAX_TOOL_CALLS,
  });
  cells.set(cellId, cell);
  sessions.get(sessionId).cells.add(cellId);
  armResponse(cell, frame.yieldTimeMs, frame.maxOutputChars);
  const maxCellTimeMs = frame.maxCellTimeMs > 0 ? frame.maxCellTimeMs : DEFAULT_MAX_CELL_TIME_MS;
  cell.lifetimeTimer = setTimeout(() => {
    terminateCell(cellId, `cell exceeded its ${maxCellTimeMs}ms lifetime`);
  }, maxCellTimeMs);
  runCellCode(cell, code, frame.tools);
}

function onWait(frame) {
  const cellId = String(frame.cellId || '');
  const cell = cells.get(cellId);
  if (!cell) {
    send({
      type: 'cell_done',
      cellId,
      ok: false,
      error: `unknown cell: ${cellId} (already finished or never started)`,
      output: [],
      logs: [],
      truncated: false,
    });
    return;
  }
  touchSession(cell.sessionId);
  if (frame.terminate) {
    terminateCell(cellId, 'terminated by wait_js');
    return;
  }
  // A finished cell is answered and deleted immediately (finishCell →
  // respondDone), so a live entry here is always still running.
  armResponse(cell, frame.yieldTimeMs, frame.maxOutputChars);
}

function onFrame(frame) {
  switch (frame.type) {
    case 'exec':
      onExec(frame);
      return;
    case 'wait':
      onWait(frame);
      return;
    case 'close_session':
      closeSession(String(frame.sessionId || 'default'));
      return;
    case 'tool_result':
      onToolResult(frame);
      return;
    case 'shutdown':
      process.exit(0);
      return;
    default:
      workerLog(`ignoring unknown frame type: ${frame.type}`);
  }
}

// ---------------------------------------------------------------------------
// stdin pump + boot
// ---------------------------------------------------------------------------

function main() {
  let buffer = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', (chunk) => {
    buffer += chunk;
    let newline = buffer.indexOf('\n');
    while (newline >= 0) {
      const line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      newline = buffer.indexOf('\n');
      if (!line.trim()) continue;
      let frame;
      try {
        frame = JSON.parse(line);
      } catch {
        workerLog('dropping malformed frame');
        continue;
      }
      try {
        onFrame(frame);
      } catch (err) {
        workerLog(`frame handler failed: ${err && err.stack ? err.stack : err}`);
      }
    }
  });
  process.stdin.on('end', () => {
    // The sidecar closed the pipe: the process's reason to exist is gone.
    process.exit(0);
  });
  const reaper = setInterval(reapIdleSessions, REAPER_INTERVAL_MS);
  reaper.unref();
  send({ type: 'ready' });
}

main();
