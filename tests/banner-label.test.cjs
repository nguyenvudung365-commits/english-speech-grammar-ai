const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

async function boot(backend) {
  const elements = new Map();
  const element = () => ({hidden: false, value: '', textContent: '', children: [], className: '',
    replaceChildren() { this.children = []; }, append(...c) { this.children.push(...c); }, focus() {}, classList: {toggle() {}}});
  const fetch = async (path) => path === '/api/auth/csrf'
    ? {ok: true, status: 200, json: async () => ({csrf_token: 't', backend})}
    : {ok: false, status: 401, json: async () => ({detail: 'x'})};
  const document = {getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element};
  const context = vm.createContext({
    document,
    fetch, setTimeout, clearTimeout, console, crypto: globalThis.crypto,
    Uint8Array, DataView, Float32Array, Set, Math, Blob,
  });
  vm.runInContext(readFileSync('src/api/static/app.js', 'utf8'), context);
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
  return ['banner-title', 'banner-text', 'status'].map((id) => document.getElementById(id).textContent).join('\n');
}

test('real backend banner/status do not claim fake or canned', async () => {
  const text = await boot('real');
  assert.doesNotMatch(text, /No real models are connected|canned/i);
  assert.match(text, /REAL ASR\/GRAMMAR/);
});

test('fake backend keeps original status text and does not override banner', async () => {
  const text = await boot('fake');
  assert.match(text, /No real models are connected/);
  assert.doesNotMatch(text, /REAL ASR\/GRAMMAR/);
});
