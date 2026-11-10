const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

function load() {
  const elements = new Map();
  const element = () => ({
    hidden: false, value: '', textContent: '', children: [], className: '',
    replaceChildren() { this.children = []; }, append(...c) { this.children.push(...c); },
    focus() {}, classList: {toggle() {}},
  });
  const context = vm.createContext({
    document: {getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element},
    fetch: () => new Promise(() => {}), setTimeout, clearTimeout, console, crypto: globalThis.crypto,
    Uint8Array, DataView, Float32Array, Set, Math, Blob,
  });
  vm.runInContext(readFileSync('src/api/static/app.js', 'utf8'), context);
  return {context, elements};
}

async function render(fake) {
  const {context, elements} = load();
  context.report = {
    jobs: [],
    grammar: [{fake, revision: 1, is_current: true, source_text: 'a', corrected_text: 'b', warnings: ['w']}],
    pronunciation: [{fake, score: null}],
  };
  vm.runInContext(`current = {id: 's', draft_transcript: ''}; dirty = true;
    api = async () => report;`, context);
  await vm.runInContext('refresh()', context);
  const text = elements.get('feedback').children.map((c) => c.textContent || c.children.map((x) => x.textContent).join('|')).join('\n');
  return text;
}

test('FAKE label shown when result.fake is true', async () => {
  const text = await render(true);
  assert.match(text, /FAKE grammar/);
  assert.match(text, /FAKE pronunciation/);
});

test('FAKE label absent when result.fake is false', async () => {
  const text = await render(false);
  assert.doesNotMatch(text, /FAKE/);
  assert.match(text, /Grammar · revision 1/);
  assert.match(text, /Pronunciation: no score/);
});

test('report-state text does not claim FAKE for a non-fake report', async () => {
  const {context, elements} = load();
  context.report = {jobs: [], grammar: [], pronunciation: [], fake: false, report_complete: true};
  vm.runInContext(`current = {id: 's', draft_transcript: ''}; dirty = true; api = async () => report;`, context);
  await vm.runInContext('refresh()', context);
  assert.doesNotMatch(elements.get('report-state').textContent, /FAKE/);
});
