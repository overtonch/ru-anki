/* Regression cover for how a coloured word is painted and what a tap on it does.
 *
 * Every screen that shows tappable words — the watch transcript (.cw), the
 * fullscreen caption (.capw), the reader (.rw) — routes through the SAME two
 * pure functions in index.html: `wordClasses` (paint) and `wordActionKind`
 * (tap). This pins their full truth table so a change to one screen can't
 * silently break word handling on the others.
 *
 * Run:  node --test tests/word_render.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const HTML = readFileSync(join(HERE, '..', 'app', 'static', 'index.html'), 'utf8');

/* pull a top-level `function name(...) { ... }` out of the page by brace-matching */
function extractFn(name) {
  const start = HTML.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} not found in index.html`);
  let i = HTML.indexOf('{', start), depth = 0;
  for (; i < HTML.length; i++) {
    const ch = HTML[i];
    if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  return HTML.slice(start, i);
}

const src = ['wordStateClass', 'wordClasses', 'wordActionKind'].map(extractFn).join('\n\n');
const { wordClasses, wordActionKind } = new Function(src + '\n;return { wordClasses, wordActionKind };')();

/* build a real `wordAction` with every downstream handler replaced by a spy, so
 * we test the actual dispatch body (not just the kind function it delegates to) */
function buildWordAction() {
  const calls = [];
  const spy = name => (...args) => calls.push({ name, args });
  const env = {
    inlineDecide: spy('inlineDecide'),
    inlineUndo: spy('inlineUndo'),
    inlineGreen: spy('inlineGreen'),
    watchTapWord: spy('watchTapWord'),
  };
  const body = [extractFn('wordStateClass'), extractFn('wordActionKind'), extractFn('wordAction')].join('\n\n');
  const make = new Function(...Object.keys(env), body + '\n;return wordAction;');
  return { wordAction: make(...Object.values(env)), calls };
}

/* w shapes straight from /watch + /read:
 *   c  carded (green) anywhere      p  pending suggestion here (cand id)
 *   cc carded via a cand here       dd skipped via a cand here          */
const CASES = [
  { name: 'plain word',                w: {},                 kind: 'card',      cls: 'cw' },
  { name: 'pending (yellow)',          w: { p: 5 },            kind: 'decide',    cls: 'cw pending' },
  { name: 'carded (green)',            w: { c: true },         kind: 'green',     cls: 'cw has-card' },
  { name: 'carded + also pending',     w: { c: true, p: 5 },   kind: 'green',     cls: 'cw has-card' },
  { name: 'carded here (undo)',        w: { cc: 9 },           kind: 'undo-card', cls: 'cw' },
  { name: 'carded here + green',       w: { cc: 9, c: true },  kind: 'undo-card', cls: 'cw has-card' },
  { name: 'skipped here (un-skip)',    w: { dd: 3 },           kind: 'undo-skip', cls: 'cw' },
  { name: 'skipped here but green',    w: { dd: 3, c: true },  kind: 'green',     cls: 'cw has-card' },
  { name: 'pending wins over undo',    w: { p: 5, cc: 9 },     kind: 'decide',    cls: 'cw pending' },
  { name: 'null / missing word obj',   w: null,               kind: 'card',      cls: 'cw' },
];

test('wordActionKind — tap routing truth table', () => {
  for (const c of CASES) {
    assert.equal(wordActionKind(c.w), c.kind, `${c.name} → tap`);
  }
});

test('wordClasses — paint truth table, every base class', () => {
  for (const base of ['cw', 'capw', 'rw', 'ly-w']) {
    for (const c of CASES) {
      const want = c.cls.replace(/^cw/, base);
      assert.equal(wordClasses(base, c.w), want, `${c.name} → ${base}`);
    }
  }
});

test('a carded word never routes to the plain card-modal', () => {
  // the bug this guards: a coloured word falling through to watchTapWord()
  assert.notEqual(wordActionKind({ c: true }), 'card');
  assert.notEqual(wordActionKind({ p: 1 }), 'card');
  assert.notEqual(wordActionKind({ cc: 1 }), 'card');
  assert.notEqual(wordActionKind({ dd: 1 }), 'card');
});

test('wordAction dispatches each colour to its handler (real body, spied deps)', () => {
  const want = {
    'plain word':             'watchTapWord',
    'pending (yellow)':       'inlineDecide',
    'carded (green)':         'inlineGreen',
    'carded + also pending':  'inlineGreen',
    'carded here (undo)':     'inlineUndo',
    'carded here + green':    'inlineUndo',
    'skipped here (un-skip)': 'inlineUndo',
    'skipped here but green': 'inlineGreen',
    'pending wins over undo': 'inlineDecide',
    'null / missing word obj':'watchTapWord',
  };
  for (const c of CASES) {
    const { wordAction, calls } = buildWordAction();
    wordAction(c.w, { textContent: 'x' }, 2, 1);
    assert.equal(calls.length, 1, `${c.name}: exactly one handler`);
    assert.equal(calls[0].name, want[c.name], `${c.name} → handler`);
  }
});

test('the render sites all call wordClasses / route via wordAction', () => {
  // renderCaption (.capw), renderTranscript (.cw), renderReaderBody (.rw)
  for (const base of ['capw', 'cw', 'rw']) {
    assert.match(HTML, new RegExp(`wordClasses\\('${base}', w\\)`),
      `${base} render site must paint via wordClasses`);
  }
  // and every tap on those spans goes through wordAction()
  assert.match(HTML, /switch \(wordActionKind\(w\)\)/, 'wordAction must dispatch on wordActionKind');
});
