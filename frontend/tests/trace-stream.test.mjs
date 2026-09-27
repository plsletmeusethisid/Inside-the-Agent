import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import ts from 'typescript';

const source = ts.transpileModule(fs.readFileSync('lib/trace-stream.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;

function client(body, options = {}) {
  const exports = {};
  vm.runInNewContext(source, { exports, TextDecoder, DOMException,
    fetch: async () => new Response(body, { headers: { 'Content-Type': 'text/event-stream' }, ...options }),
  });
  return exports;
}
function event(name, sequence, data = {}) {
  return `id: run:${sequence}\r\nevent: ${name}\r\ndata: ${JSON.stringify({ event: name, sequence, run_id: 'run', elapsed_ms: sequence, data })}\r\n\r\n`;
}

test('SSE parser accepts one-byte UTF-8 reads, CRLF, comments, and completed result', async () => {
  const bytes = new TextEncoder().encode(': heartbeat\r\n\r\n' + event('token', 1, { text: '한글 😀' }) + event('completed', 2, { result: { answer: '한글 😀' } }));
  let position = 0;
  const body = new ReadableStream({ pull(controller) { if (position < bytes.length) controller.enqueue(bytes.slice(position, ++position)); else controller.close(); } });
  const seen = [];
  const result = await client(body).traceRequest('/stream', {}, e => seen.push(e));
  assert.equal(result.answer, '한글 😀');
  assert.equal(seen[0].data.text, '한글 😀');
});

test('EOF, safe server errors, and sequence gaps cannot look completed', async () => {
  for (const [body, pattern] of [
    [event('generation_started', 1), /before completion/],
    [event('error', 1, { message: 'Provider unavailable' }), /Provider unavailable/],
    [event('completed', 2, { result: {} }), /interrupted/],
  ]) await assert.rejects(client(body).traceRequest('/stream', {}, () => {}), pattern);
});

test('abort prevents stale events and reader cleanup cancels its source', async () => {
  let cancelled = false, delivered = false;
  const abort = new AbortController();
  abort.abort();
  const body = new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode(event('completed', 1))); }, cancel() { cancelled = true; } });
  await assert.rejects(client(body).traceRequest('/stream', { signal: abort.signal }, () => { delivered = true; }), /Cancelled/);
  assert.equal(delivered, false);
  assert.equal(cancelled, true);
});
