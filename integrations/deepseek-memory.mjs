import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { isAbsolute } from 'node:path';

export const name = 'akb-deepseek-memory';
export const inject = ['systemPrompt'];
const bridgePath = fileURLToPath(new URL('../scripts/deepseek_lifecycle_bridge.py', import.meta.url));

export function runBridge(action, payload, timeout = 8000) {
  return new Promise((resolve, reject) => {
    const child = spawn('/usr/bin/python3', [bridgePath, action], {
      stdio: ['pipe', 'pipe', 'pipe'], env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
    });
    let output = '';
    let diagnostic = '';
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (error) reject(error); else resolve(value);
    };
    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      finish(new Error('bridge_timeout'));
    }, timeout);
    child.on('error', () => finish(new Error('bridge_spawn')));
    child.stdin.on('error', () => finish(new Error('bridge_stdin')));
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      output += chunk;
      if (output.length > 65536) {
        child.kill('SIGKILL');
        finish(new Error('bridge_output_limit'));
      }
    });
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', (chunk) => { diagnostic = (diagnostic + chunk).slice(-4096); });
    child.on('close', (code) => {
      if (code !== 0) {
        // Preserve only the bridge's known exception category, never raw stderr.
        let category = '';
        try {
          const value = JSON.parse(diagnostic).error;
          if (['ValueError', 'JSONDecodeError', 'OSError', 'PermissionError',
            'FileNotFoundError', 'OperationalError', 'RuntimeError'].includes(value)) category = `:${value}`;
        } catch { /* Arbitrary child diagnostics are private. */ }
        return finish(new Error(`bridge_failed${category}`));
      }
      try { finish(null, JSON.parse(output)); }
      catch { finish(new Error('bridge_response')); }
    });
    child.stdin.end(JSON.stringify(payload));
  });
}

export function publicEvent(event) {
  let message;
  if (event?.type === 'user/message' && event.data?.source?.kind === 'user') {
    message = { role: 'user', content: event.data.content };
  } else if (event?.type === 'assistant/message') {
    message = event.data?.message;
    if (message?.role !== 'assistant') return null;
  } else return null;
  const parts = message?.content;
  const text = typeof parts === 'string' ? parts : Array.isArray(parts)
    ? parts.filter((part) => part?.type === 'text' && typeof part.text === 'string')
      .map((part) => part.text).join('\n') : '';
  if (!text.trim()) return null;
  return { role: message.role, text: text.slice(-18000),
    ...(Number.isSafeInteger(event.seq) ? { id: `seq:${event.seq}` } : {}) };
}

// The optional dependency is only for isolated tests; Cordis calls apply(ctx).
export function apply(ctx, _config = {}, bridge = runBridge) {
  const states = new WeakMap();
  const warn = (error) => {
    const category = /^bridge_(?:timeout|spawn|stdin|output_limit|response|failed(?::[A-Za-z]+)?)$/.test(error?.message)
      ? error.message : 'bridge_operation';
    ctx.logger.warn(`AKB lifecycle ${category} (private diagnostics suppressed)`);
  };
  function stateOf(session) {
    const cwd = session?.header?.cwd;
    if (!session || !isAbsolute(cwd ?? '') || typeof session.id !== 'string') return null;
    if (!states.has(session)) states.set(session, {
      cwd, session_id: session.id, events: [], chars: 0, version: 0, saved: 0,
      chain: Promise.resolve(), recall: null,
    });
    return states.get(session);
  }
  function schedule(session) {
    const state = stateOf(session);
    if (!state) return Promise.resolve();
    state.chain = state.chain.then(async () => {
      if (state.saved === state.version) return;
      const version = state.version;
      const result = await bridge('capture', {
        cwd: state.cwd, session_id: state.session_id, events: state.events.slice(),
      });
      if (!(result?.captured && result?.queued) && !['duplicate', 'no_new_dialogue'].includes(result?.reason)) {
        throw new Error('capture_not_queued');
      }
      state.saved = version;
    }).catch(warn);
    return state.chain;
  }
  ctx.on('system-prompt/assemble', async (assembly, render, next) => {
    const result = await next();
    const state = stateOf(render?.agent?.session);
    if (!state) return result;
    if (!state.recall) state.recall = bridge('recall', { cwd: state.cwd })
      .then((value) => typeof value?.context === 'string' ? value.context : '')
      .catch((error) => { warn(error); state.recall = null; return ''; });
    const text = await state.recall;
    if (!text) return result;
    return { ...result, contexts: [...result.contexts.filter((x) => x.name !== name),
      { name, text, interpolate: false }] };
  });
  ctx.on('session/event', (session, event) => {
    const state = stateOf(session);
    if (!state) return;
    const row = publicEvent(event);
    if (row) {
      state.events.push(row);
      state.chars += row.text.length;
      while (state.chars > 18000 && state.events.length > 1) state.chars -= state.events.shift().text.length;
      state.version++;
    }
    // Session observers must not synchronously append or wait on model/cloud calls.
    if (event?.type === 'turn/end') void schedule(session);
  });
  ctx.on('session/disposed', (session) => { void schedule(session); });
  ctx.on('session/flush', (session) => schedule(session));
}
