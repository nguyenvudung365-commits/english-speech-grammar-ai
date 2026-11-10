const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

async function render(fake) {
  const elements = new Map();
  const element = () => ({hidden: false, value: '', textContent: '', children: [], className: '',
    replaceChildren() { this.children = []; }, append(...c) { this.children.push(...c); }, focus() {}, classList: {toggle() {}}});
  const context = vm.createContext({
    document: {getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element},
    fetch: () => new Promise(() => {}), setTimeout, clearTimeout, console, crypto: globalThis.crypto,
    Uint8Array, DataView, Float32Array, Set, Math, Blob,
  });
  vm.runInContext(readFileSync('src/api/static/app.js', 'utf8'), context);
  context.report = {jobs: [], pronunciation: [],
    grammar: [{fake, revision: 1, is_current: true, source_text: 'He go.', corrected_text: 'He goes.', warnings: []}]};
  vm.runInContext(`current = {id: 's', draft_transcript: ''}; dirty = true; api = async () => report;`, context);
  await vm.runInContext('refresh()', context);
  return elements.get('feedback').children.map((c) => c.children.map((x) => x.textContent).join('|')).join('\n');
}

test('"Canned suggestion" appears only for fake results', async () => {
  assert.match(await render(true), /Canned suggestion: He goes\./);
  const real = await render(false);
  assert.doesNotMatch(real, /Canned/);
  assert.match(real, /Suggestion: He goes\./);
});
