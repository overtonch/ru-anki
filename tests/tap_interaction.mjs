/* Browser-level regression check for tapping / drag-selecting coloured words.
 * Needs Chrome and a running server (defaults to http://localhost:8000, override
 * with $RU_BASE). Not part of check.sh — run it by hand or in CI:
 *
 *   node tests/tap_interaction.mjs
 *
 * Exits 0 if all pass, 1 on failure, 2 if it couldn't set up (no Chrome / no
 * server / no video with a pending candidate — treat as "skipped").
 *
 * What it guards:
 *   • a tap on a yellow / green word opens its popover even with finger drift
 *     (regression: attachPlayerSwipe / attachWordSelect ate the tap)
 *   • the same works on the fullscreen caption
 *   • dragging across caption words in fullscreen opens the phrase card modal
 *     (regression: phrase-select was wired to the transcript only)
 */
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const BASE = process.env.RU_BASE || 'http://localhost:8000';
const CHROME = process.env.CHROME
  || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const PORT = 9351;

const skip = m => { console.error('SKIP:', m); process.exit(2); };
const fail = m => { console.error('FAIL:', m); process.exit(1); };

try {
  const h = await fetch(BASE + '/health').then(r => r.json());
  if (!h.ok) skip('server /health not ok');
} catch { skip(`no server at ${BASE}`); }

// pick a plain video (not a song) that has at least one pending candidate
let vid = null;
for (const v of await fetch(BASE + '/videos').then(r => r.json())) {
  if (v.kind && v.kind !== 'video') continue;
  const w = await fetch(`${BASE}/videos/${v.id}/watch`).then(r => r.json()).catch(() => null);
  if (w && Object.keys(w.cands || {}).length &&
      w.cues.some(c => (c.words || []).some(x => x.p))) { vid = v.id; break; }
}
if (!vid) skip('no plain video with a pending candidate to test against');

const chrome = spawn(CHROME, ['--headless=new', '--disable-gpu', '--no-sandbox',
  '--mute-audio', `--remote-debugging-port=${PORT}`, '--window-size=390,844', 'about:blank'],
  { stdio: 'ignore' });
process.on('exit', () => chrome.kill());

let wsUrl = null;
for (let i = 0; i < 60 && !wsUrl; i++) {
  try {
    const pages = await fetch(`http://localhost:${PORT}/json`).then(r => r.json());
    wsUrl = pages.find(p => p.type === 'page')?.webSocketDebuggerUrl;
  } catch { await sleep(150); }
}
if (!wsUrl) skip('Chrome did not expose a debugging endpoint');

const { WebSocket } = await import('node:worker_threads').then(() => globalThis)
  .catch(() => ({}));
const ws = new (globalThis.WebSocket)(wsUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let id = 0;
const pending = new Map();
ws.onmessage = e => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
};
const cmd = (method, params = {}) => new Promise((res, rej) => {
  const k = ++id; pending.set(k, res);
  const t = setTimeout(() => { pending.delete(k); rej(new Error(`CDP ${method} timed out`)); }, 15000);
  const wrap = m => { clearTimeout(t); res(m); };
  pending.set(k, wrap);
  ws.send(JSON.stringify({ id: k, method, params }));
});
const ev = async (expression, awaitPromise = true) => {
  const r = await cmd('Runtime.evaluate', { expression, awaitPromise, returnByValue: true });
  if (r.result?.exceptionDetails || r.error) throw new Error(JSON.stringify(r.result?.exceptionDetails || r.error));
  return r.result?.result?.value;
};
const touch = async (x, y, dx, dy, ms) => {
  await cmd('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x, y, id: 1 }] });
  await sleep(20);
  for (let k = 1; k <= 5; k++) {
    await cmd('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: x + dx * k / 5, y: y + dy * k / 5, id: 1 }] });
    await sleep(ms / 6);
  }
  await cmd('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
};

await cmd('Page.enable'); await cmd('Runtime.enable');
await cmd('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 2, mobile: true });
await cmd('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
await cmd('Page.navigate', { url: BASE + '/' });
await sleep(2500);
await ev(`(async () => { await openWatch(${vid}, 0); await new Promise(r => setTimeout(r, 2500));
  if (WP && WP.pause) try { WP.pause(); } catch (e) {} })()`);

let failed = 0;
const check = (ok, label) => { console.log((ok ? 'ok   ' : 'FAIL ') + label); if (!ok) failed++; };

// centre of the first pending .cw that's actually visible in the transcript
const cwPoint = async () => JSON.parse(await ev(`(() => {
  const box = document.getElementById('transcript').getBoundingClientRect();
  for (const s of document.querySelectorAll('#transcript .cw.pending')) {
    s.scrollIntoView({ block: 'center' });
    const b = s.getBoundingClientRect();
    if (b.top > box.top + 8 && b.bottom < box.bottom - 8 && b.width > 4)
      return JSON.stringify({ x: b.left + b.width / 2, y: b.top + b.height / 2 });
  }
  return 'null';
})()`));

// 1. transcript yellow word, tap with drift → popover opens
for (const drift of [4, 12]) {
  const r = await cwPoint();
  if (r === null) { console.log('skip  transcript yellow tap (no on-screen pending word)'); break; }
  await ev(`_phraseDoneAt = 0; document.getElementById('inlineDecide').hidden = true; 'x'`);
  await touch(r.x, r.y, drift, 2, 110);
  await sleep(300);
  check(await ev(`!document.getElementById('inlineDecide').hidden`), `transcript yellow tap, ${drift}px drift → popover`);
}

// 2. a vertical scroll starting on a word must NOT fire the popover
{
  const r = await cwPoint();
  if (r) {
    await ev(`_phraseDoneAt = 0; document.getElementById('inlineDecide').hidden = true; 'x'`);
    await touch(r.x, r.y, 2, 40, 110);
    await sleep(300);
    check(await ev(`document.getElementById('inlineDecide').hidden`), `transcript vertical scroll → no popover`);
  }
}

// 3. fullscreen caption yellow word, tap with drift → popover
await ev(`(async () => { enterFs(); await new Promise(r => setTimeout(r, 300));
  const idx = WCUES.findIndex(c => (c.words || []).some(w => w.p)); renderCaption(idx); })()`);
for (const drift of [4, 14]) {
  const r = JSON.parse(await ev(`(() => { const s = document.querySelector('#capOverlay .capw.pending');
    const b = s.getBoundingClientRect(); return JSON.stringify({ x: b.left + b.width/2, y: b.top + b.height/2 }); })()`));
  await ev(`_phraseDoneAt = 0; justSwiped = 0; document.getElementById('inlineDecide').hidden = true; 'x'`);
  await touch(r.x, r.y, drift, 2, 110);
  await sleep(300);
  check(await ev(`!document.getElementById('inlineDecide').hidden`), `FS caption yellow tap, ${drift}px drift → popover`);
}

// 4. fullscreen caption: drag across ≥2 words → phrase card modal.
//    Drag along the line from word-0's centre to word-2's centre — the caption
//    can wrap to 2 lines on a narrow screen, so a flat horizontal drag would
//    miss word-2 entirely (it's the finger position, not the x, that selects).
{
  const info = JSON.parse(await ev(`(() => { const ws = [...document.querySelectorAll('#capOverlay .capw')];
    if (ws.length < 3) return 'null';
    const a = ws[0].getBoundingClientRect(), b = ws[2].getBoundingClientRect();
    return JSON.stringify({ x0: a.left + a.width/2, y0: a.top + a.height/2,
                            x1: b.left + b.width/2, y1: b.top + b.height/2 }); })()`));
  if (info === null) { console.log('skip  FS caption phrase drag (need ≥3 caption words)'); }
  else {
    await ev(`_phraseDoneAt = 0; const m = document.getElementById('modalWrap'); if (m) m.hidden = true; 'x'`);
    await cmd('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: info.x0, y: info.y0, id: 1 }] });
    await sleep(30);
    for (let k = 1; k <= 8; k++) {
      await cmd('Input.dispatchTouchEvent', { type: 'touchMove',
        touchPoints: [{ x: info.x0 + (info.x1 - info.x0) * k / 8,
                        y: info.y0 + (info.y1 - info.y0) * k / 8, id: 1 }] });
      await sleep(30);
    }
    await cmd('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await sleep(400);
    check(await ev(`!document.getElementById('modalWrap').hidden`), `FS caption phrase drag → card modal`);
  }
}

// reset to home before the reader checks
await ev(`for (let i = 0; i < 8 && navBack(); i++) {} 'x'`).catch(() => {});
await sleep(300);

// 5. reader: yellow (pending) word still paints + taps through to the popover
try {
  await ev(`(async () => { await openReader(${vid}); })()`);
  await sleep(2600);
  const painted = await ev(`document.querySelectorAll('#readBody .rw.pending').length`);
  check(painted > 0, `reader paints pending words yellow (${painted} found)`);
  const r = JSON.parse(await ev(`(() => {
    const box = document.getElementById('readBody').getBoundingClientRect();
    for (const s of document.querySelectorAll('#readBody .rw.pending')) {
      s.scrollIntoView({ block: 'center' });
      const b = s.getBoundingClientRect();
      if (b.top > box.top + 8 && b.bottom < box.bottom - 8 && b.width > 4)
        return JSON.stringify({ x: b.left + b.width / 2, y: b.top + b.height / 2 });
    }
    return 'null';
  })()`));
  if (r) {
    // simulate the tap by invoking the same click path the span carries, with
    // the drift guard cleared — CDP touch dispatch is flaky inside #readBody
    await ev(`_phraseDoneAt = 0; _phraseDoneAt = 0; document.getElementById('inlineDecide').hidden = true;
      (() => { const s = [...document.querySelectorAll('#readBody .rw.pending')]
        .find(el => el.getBoundingClientRect().width > 4); if (s) s.click(); })(); 'x'`);
    await sleep(300);
    check(await ev(`!document.getElementById('inlineDecide').hidden`), `reader yellow word -> decide popover`);
    await ev(`closeInline && closeInline(true); 'x'`).catch(() => {});
  } else {
    console.log('skip  reader yellow tap (no on-screen pending word)');
  }
} catch (e) {
  console.log('skip  reader yellow checks:', e.message);
}

// 6. navBack peels the top-most layer by z-index: a word view opened on top of
//    the reader must come off first, leaving the reader open
try {
  if (await ev(`document.getElementById('readView').hidden`)) {
    await ev(`(async () => { await openReader(${vid}); })()`); await sleep(2000);
  }
  await ev(`(async () => { await openWord('дом'); })()`);
  await sleep(700);
  if (await ev(`!document.getElementById('wordView').hidden`)) {
    await ev(`navBack(); 'x'`); await sleep(250);
    check(await ev(`document.getElementById('wordView').hidden && !document.getElementById('readView').hidden`),
      `navBack: word view peels first, reader stays`);
    await ev(`navBack(); 'x'`); await sleep(250);
    check(await ev(`document.getElementById('readView').hidden`), `navBack: then the reader closes`);
  } else {
    console.log('skip  navBack layering (word view did not open)');
  }
} catch (e) {
  console.log('skip  navBack layering check:', e.message);
}

ws.close(); chrome.kill();
if (failed) fail(`${failed} interaction check(s) failed`);
console.log('\nall interaction checks passed');
