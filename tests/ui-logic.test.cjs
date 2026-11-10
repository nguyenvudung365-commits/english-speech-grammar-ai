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

const job = (id, status, attempt, extra = {}) =>
  ({id, kind: 'grammar', status, attempt_no: attempt, max_attempts: 3, stages: {}, retry_of_job_id: null, ...extra});

test('Retry button only on failed, unretried, below-limit jobs', async () => {
  const {context, elements} = load();
  context.report = {
    jobs: [job('j1', 'failed', 1), job('j2', 'failed', 1, {}), job('j2b', 'queued', 2, {retry_of_job_id: 'j2'}),
           job('j3', 'failed', 3), job('j4', 'succeeded', 1), job('j5', 'failed', 2, {max_attempts: 2})],
    grammar: [], pronunciation: [],
  };
  context.calls = [];
  vm.runInContext(`
    current = {id: 's', draft_transcript: ''};
    dirty = true;
    api = async (path, method, body) => { calls.push([path, method, body]); return path.endsWith('/report') ? report : {}; };
  `, context);
  await vm.runInContext('refresh()', context);
  const rows = elements.get('job-list').children;
  const retryButtons = rows.map(p => p.children.filter(c => c && c.textContent === 'Retry'));
  // j1 and j2 (j2 is retried by j2b -> no button). Expect button only on j1.
  assert.deepEqual(rows.map((p, i) => retryButtons[i].length), [1, 0, 0, 0, 0, 0]);
  vm.runInContext('clearTimeout(timer); refresh = async () => {};', context); // queued job schedules a poll
  await retryButtons[0][0].onclick();
  const post = context.calls.find(c => c[1] === 'POST');
  assert.equal(post[0], '/api/jobs/j1/retry');
  assert.equal(typeof post[2].idempotency_key, 'string');
  assert.ok(post[2].idempotency_key.length > 0);
});

function wav(samples) {
  const {context} = load();
  context.samples = samples;
  return vm.runInContext('encodeWav(samples)', context);
}
const view = (b) => new DataView(b.buffer, b.byteOffset, b.byteLength);
const tag = (b, at) => String.fromCharCode(...b.slice(at, at + 4));

test('encodeWav header is 16 kHz mono PCM16', () => {
  const out = wav(new Float32Array(10));
  const v = view(out);
  assert.equal(out.length, 44 + 20);
  assert.equal(tag(out, 0), 'RIFF'); assert.equal(tag(out, 8), 'WAVE');
  assert.equal(tag(out, 12), 'fmt '); assert.equal(tag(out, 36), 'data');
  assert.equal(v.getUint32(4, true), 36 + 20);
  assert.equal(v.getUint32(16, true), 16);
  assert.equal(v.getUint16(20, true), 1);
  assert.equal(v.getUint16(22, true), 1);
  assert.equal(v.getUint32(24, true), 16000);
  assert.equal(v.getUint32(28, true), 32000);
  assert.equal(v.getUint16(32, true), 2);
  assert.equal(v.getUint16(34, true), 16);
  assert.equal(v.getUint32(40, true), 20);
});

test('encodeWav clamps to [-1, 1]', () => {
  const v = view(wav(new Float32Array([2, -2, 0, 1, -1, 0.5])));
  const s = (i) => v.getInt16(44 + 2 * i, true);
  assert.equal(s(0), 32767);
  assert.ok(s(1) === -32768 || s(1) === -32767);
  assert.equal(s(2), 0);
  assert.equal(s(3), 32767);
  assert.ok(s(4) <= -32767);
  assert.ok(Math.abs(s(5) - 16383) <= 1);
});

test('encodeWav truncates at 480000 frames and handles empty input', () => {
  for (const [n, expected] of [[480000, 480000], [480001, 480000], [0, 0]]) {
    const out = wav(new Float32Array(n));
    assert.equal(out.length, 44 + 2 * expected);
    assert.equal(view(out).getUint32(40, true), 2 * expected);
  }
});

// --- recording flow (mocked Web Audio) ---
function recEnv({getUserMedia, failContext = false, resume} = {}) {
  const {context, elements} = load();
  const env = {tracks: [], contexts: [], calls: [], nodes: []};
  const stream = {getTracks: () => env.tracks};
  env.tracks.push({stopped: 0, stop() { this.stopped++; }});
  class FakeAudioContext {
    constructor() { if (failContext) throw new Error('no audio'); this.sampleRate = 16000; this.closed = 0; this.destination = {}; env.contexts.push(this); }
    async resume() { if (resume) await resume(); }
    close() { this.closed++; return Promise.resolve(); }
    createScriptProcessor() { const n = {connect() {}}; env.nodes.push(n); return n; }
    createMediaStreamSource() { return {connect() {}}; }
  }
  class FakeOffline {
    constructor() { this.destination = {}; }
    createBuffer() { return {copyToChannel() {}}; }
    createBufferSource() { return {connect() {}, start() {}}; }
    async startRendering() { return {getChannelData: () => new Float32Array(10)}; }
  }
  Object.assign(context, {
    navigator: {mediaDevices: {getUserMedia: getUserMedia || (async () => stream)}},
    AudioContext: FakeAudioContext, OfflineAudioContext: FakeOffline, env,
  });
  vm.runInContext(`
    current = {id: 's1', draft_transcript: '', mode: 'm', transcript_revision: 1};
    api = async (path, method) => { env.calls.push([path, method]); return {}; };
    refresh = async () => {};
  `, context);
  return {context, elements, env};
}
const sample = () => ({inputBuffer: {getChannelData: () => new Float32Array(4096)}});

test('mic permission denied -> error status, no /audio request', async () => {
  const {context, elements, env} = recEnv({getUserMedia: async () => { throw Object.assign(new Error('x'), {name: 'NotAllowedError'}); }});
  await elements.get('record') ?? vm.runInContext('$("record")', context);
  await vm.runInContext('$("record").onclick()', context);
  assert.match(elements.get('status').textContent, /permission denied/i);
  assert.ok(env.calls.every(c => !c[0].endsWith('/audio')));
  assert.equal(vm.runInContext('rec', context), null);
});

test('Record is disabled while recording', async () => {
  const {context, elements} = recEnv();
  await vm.runInContext('$("record").onclick()', context);
  assert.equal(elements.get('record').disabled, true);
  vm.runInContext('updateButtons()', context);
  assert.equal(elements.get('record').disabled, true);
});

test('switching to another practice while recording aborts without upload', async () => {
  const {context, env} = recEnv();
  await vm.runInContext('$("record").onclick()', context);
  vm.runInContext(`showPractice({id: 's2', mode: 'm', transcript_revision: 1}, false)`, context);
  assert.equal(env.tracks[0].stopped, 1);
  assert.equal(env.contexts[0].closed, 1);
  assert.equal(vm.runInContext('rec', context), null);
  assert.ok(env.calls.every(c => !c[0].endsWith('/audio')));
});

test('re-showing the same practice keeps recording', async () => {
  const {context, env} = recEnv();
  await vm.runInContext('$("record").onclick()', context);
  vm.runInContext(`showPractice({id: 's1', mode: 'm', transcript_revision: 1}, false)`, context);
  assert.equal(env.tracks[0].stopped, 0);
  assert.notEqual(vm.runInContext('rec', context), null);
});

test('logout while recording stops track, closes context, no upload', async () => {
  const {context, env} = recEnv();
  await vm.runInContext('$("record").onclick()', context);
  vm.runInContext('loggedOut()', context);
  assert.equal(env.tracks[0].stopped, 1);
  assert.equal(env.contexts[0].closed, 1);
  assert.equal(vm.runInContext('rec', context), null);
  assert.ok(env.calls.every(c => !c[0].endsWith('/audio')));
});

test('AudioContext creation failure stops the mic track', async () => {
  const {context, elements, env} = recEnv({failContext: true});
  await vm.runInContext('$("record").onclick()', context);
  assert.equal(env.tracks[0].stopped, 1);
  assert.equal(elements.get('status').textContent, 'no audio');
  assert.equal(vm.runInContext('rec', context), null);
});

test('Stop uploads to the session recording started in', async () => {
  const {context, env} = recEnv();
  await vm.runInContext('$("record").onclick()', context);
  env.nodes[0].onaudioprocess(sample());
  vm.runInContext(`current = {id: 's2'}`, context); // bypass showPractice to prove sid is captured at start
  await vm.runInContext('$("stop-record").onclick()', context);
  const uploads = env.calls.filter(c => c[0].endsWith('/audio'));
  assert.equal(uploads.length, 1); assert.equal(uploads[0][0], '/api/sessions/s1/audio'); assert.equal(uploads[0][1], 'POST');
  assert.equal(env.tracks[0].stopped, 1);
});

// --- session lost/changed while awaiting mic permission or context.resume() ---
const deferred = () => { let resolve; const promise = new Promise((r) => { resolve = r; }); return {promise, resolve}; };
const noUpload = (env) => assert.ok(env.calls.every(c => !c[0].endsWith('/audio')));

for (const [name, change] of [
  ['logout', 'loggedOut()'],
  ['current=null', 'current = null'],
  ['different practice', `current = {id: 's2', draft_transcript: '', mode: 'm', transcript_revision: 1}`],
]) {
  test(`${name} while awaiting getUserMedia releases the mic`, async () => {
    const gum = deferred();
    const {context, elements, env} = recEnv({getUserMedia: () => gum.promise});
    const pending = vm.runInContext('$("record").onclick()', context);
    vm.runInContext(change, context);
    gum.resolve({getTracks: () => env.tracks});
    await pending;
    assert.equal(env.tracks[0].stopped, 1);
    assert.equal(env.contexts.length, 0);
    assert.equal(vm.runInContext('rec', context), null);
    assert.match(elements.get('status').textContent, /Recording cancelled/);
    assert.doesNotMatch(elements.get('status').textContent, /TypeError|null/);
    noUpload(env);
  });

  test(`${name} while awaiting context.resume() stops track and closes context`, async () => {
    const res = deferred();
    const {context, elements, env} = recEnv({resume: () => res.promise});
    const pending = vm.runInContext('$("record").onclick()', context);
    await new Promise((r) => setImmediate(r)); // let it reach resume()
    assert.equal(env.contexts.length, 1);
    vm.runInContext(change, context);
    res.resolve();
    await pending;
    assert.equal(env.tracks[0].stopped, 1);
    assert.equal(env.contexts[0].closed, 1);
    assert.equal(env.nodes.length, 0);
    assert.equal(vm.runInContext('rec', context), null);
    assert.match(elements.get('status').textContent, /Recording cancelled/);
    noUpload(env);
  });
}

// --- `starting` guard: Record must not be re-entered while mic permission is pending ---
test('Record stays disabled by updateButtons() while getUserMedia is pending', async () => {
  const gum = deferred();
  const {context, elements, env} = recEnv({getUserMedia: () => gum.promise});
  const pending = vm.runInContext('$("record").onclick()', context);
  assert.equal(elements.get('record').disabled, true);
  vm.runInContext('updateButtons()', context);
  assert.equal(elements.get('record').disabled, true);
  gum.resolve({getTracks: () => env.tracks});
  await pending;
});

test('second Record click while first is pending does not call getUserMedia again; no leaked tracks', async () => {
  const gum = deferred();
  let n = 0;
  const streams = [];
  const {context, env} = recEnv({getUserMedia: () => { n++; return gum.promise; }});
  const first = vm.runInContext('$("record").onclick()', context);
  await vm.runInContext('$("record").onclick()', context);
  assert.equal(n, 1);
  gum.resolve({getTracks: () => env.tracks});
  vm.runInContext('loggedOut()', context);
  await first;
  assert.equal(n, 1);
  assert.ok(env.tracks.length > 0 && env.tracks.every((t) => t.stopped === 1));
  assert.equal(vm.runInContext('rec', context), null);
  noUpload(env);
});

for (const [name, opts, change] of [
  ['getUserMedia error', {getUserMedia: async () => { throw new Error('x'); }}, null],
  ['cancel by switching practice', null, `current = {id: 's2', draft_transcript: '', mode: 'm', transcript_revision: 1}`],
]) {
  test(`after ${name}, starting is cleared and Record is usable again`, async () => {
    const gum = deferred();
    const {context, elements, env} = recEnv(opts || {getUserMedia: () => gum.promise});
    const pending = vm.runInContext('$("record").onclick()', context);
    if (change) { vm.runInContext(change, context); gum.resolve({getTracks: () => env.tracks}); }
    await pending;
    assert.equal(vm.runInContext('starting', context), false);
    assert.ok(vm.runInContext('current', context));
    assert.equal(elements.get('record').disabled, false);
    vm.runInContext('updateButtons()', context);
    assert.equal(elements.get('record').disabled, false);
  });
}
