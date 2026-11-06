const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');

for (const scenario of ['typing', 'switching', 'edit-then-save']) {
  test(`refresh preserves state after ${scenario}`, async () => {
    let resolve;
    const response = new Promise(done => { resolve = done; });
    const elements = new Map();
    const element = () => ({hidden: false, value: '', textContent: '', replaceChildren() {}, append() {}, focus() {}, classList: {toggle() {}}});
    const context = vm.createContext({
      document: {getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element},
      fetch: () => new Promise(() => {}), setTimeout, clearTimeout, console,
    });
    vm.runInContext(readFileSync('src/api/static/app.js', 'utf8'), context);
    context.response = response;
    vm.runInContext(`
      current = {id: 'old', draft_transcript: 'saved'};
      api = async (path) => path.endsWith('/report') ? {jobs: [], grammar: [], pronunciation: []} : response;
      pending = refresh();
    `, context);
    // Let refresh reach its second request.
    await Promise.resolve();
    if (scenario === 'typing') vm.runInContext(`$('transcript').value = 'unsaved'; $('transcript').oninput();`, context);
    if (scenario === 'switching') vm.runInContext(`showPractice({id: 'new', draft_transcript: 'new session'});`, context);
    if (scenario === 'edit-then-save') vm.runInContext(`$('transcript').oninput(); showPractice({id: 'old', draft_transcript: 'new saved'});`, context);
    resolve({id: 'old', draft_transcript: 'stale'});
    await context.pending;
    assert.notEqual(elements.get('transcript').value, 'stale');
  });
}

for (const action of ['save', 'confirm', 'reload']) {
  test(`${action} preserves edits typed while the response is pending`, async () => {
    let resolve;
    const response = new Promise(done => { resolve = done; });
    const elements = new Map();
    const element = () => ({hidden: false, value: '', textContent: '', replaceChildren() {}, append() {}, focus() {}, classList: {toggle() {}}});
    const context = vm.createContext({
      document: {getElementById(id) { if (!elements.has(id)) elements.set(id, element()); return elements.get(id); }, createElement: element},
      fetch: () => new Promise(() => {}), setTimeout, clearTimeout, console,
    });
    vm.runInContext(readFileSync('src/api/static/app.js', 'utf8'), context);
    context.response = response;
    context.action = action;
    vm.runInContext(`
      current = {id: 'old', transcript_revision: 1, draft_transcript: 'submitted'};
      $('transcript').value = 'submitted';
      api = async () => response;
      refresh = async () => {};
      pending = $(action).onclick();
      $('transcript').value = 'newer unsaved'; $('transcript').oninput();
    `, context);
    resolve({id: 'old', transcript_revision: 2, draft_transcript: 'submitted'});
    await context.pending;
    assert.equal(elements.get('transcript').value, 'newer unsaved');
    assert.equal(vm.runInContext('dirty', context), true);
    assert.equal(vm.runInContext('current.transcript_revision', context), 2);
  });
}
