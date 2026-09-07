/* Regression: the review Undo button must roll back exactly ONE card per tap.
 * A double-tap (easy on mobile) or a ghost click used to revert two.
 *
 * Runs the real `studyUndo` from index.html with every dependency mocked, and
 * fires it twice in quick succession the way a fumbled tap does.
 *
 * Run:  node --test tests/study_undo.test.mjs
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const HERE = dirname(fileURLToPath(import.meta.url));
const HTML = readFileSync(join(HERE, '..', 'app', 'static', 'index.html'), 'utf8');

function extractFn(name) {
  const start = HTML.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `function ${name} not found`);
  // include a leading `async ` / `let x = ` line if present
  const lineStart = HTML.lastIndexOf('\n', start) + 1;
  let i = HTML.indexOf('{', start), depth = 0;
  for (; i < HTML.length; i++) {
    const ch = HTML[i];
    if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) { i++; break; } }
  }
  return HTML.slice(lineStart, i);
}

function harness(history, opts = {}) {
  const hist = history || [{ card: { id: 1 }, rating: 3 }, { card: { id: 2 }, rating: 3 }];
  const state = {
    undoCalls: 0, undoDelay: opts.undoDelay || 0,
    STUDY: {
      _busy: false, done: hist.length + 3, practice: !!opts.practice,
      history: hist,
      queue: [{ id: 3 }, { id: 4 }],
      i: 0,
    },
  };
  const env = {
    STUDY: state.STUDY,
    Date,
    $: () => ({ disabled: false }),
    api: async (path) => {
      const m = /\/srs\/cards\/(\d+)\/undo$/.exec(path);
      if (m) {
        if (state.undoDelay) await new Promise(r => setTimeout(r, state.undoDelay));
        state.undoCalls++; return { card: { id: Number(m[1]) } };
      }
      return {};
    },
    idbAll: async () => [],
    idbDel: async () => {},
    toast: () => {},
    renderStudyCard: () => {},
    studyReveal: () => {},
    refreshStudyBanner: () => {},
    refreshQueuePill: () => {},
  };
  const body = 'let _lastUndoAt = 0;\n' + extractFn('studyUndo');
  const make = new Function(...Object.keys(env), body + '\n;return studyUndo;');
  return { studyUndo: make(...Object.values(env)), state };
}

test('two rapid undo taps revert exactly one card', async () => {
  const { studyUndo, state } = harness(null, { undoDelay: 5 });
  await Promise.all([studyUndo(), studyUndo(), studyUndo()]);
  assert.equal(state.undoCalls, 1, 'server /undo hit once');
  assert.equal(state.STUDY.history.length, 1, 'one history entry popped');
  assert.equal(state.STUDY.done, 4, 'done decremented by one');
  assert.equal(state.STUDY.queue.filter(c => c.id === 2).length, 1, 'card re-queued once');
});

test('undo works again on a deliberate second tap', async () => {
  const { studyUndo, state } = harness();
  await studyUndo();
  assert.equal(state.STUDY.history.length, 1);
  await new Promise(r => setTimeout(r, 550));
  await studyUndo();
  assert.equal(state.STUDY.history.length, 0, 'second deliberate undo went through');
  assert.equal(state.STUDY.done, 3);
});

test('a failed-then-passed card is one card back, not two taps', async () => {
  // history: card 7 failed (Again), re-queued, then passed (Good) — two rows,
  // one logical card. One undo tap should peel BOTH and land on card 7.
  const { studyUndo, state } = harness([
    { card: { id: 5 }, rating: 3 },
    { card: { id: 7 }, rating: 1 },
    { card: { id: 7 }, rating: 3 },
  ], { undoDelay: 5 });
  await studyUndo();
  assert.equal(state.undoCalls, 2, 'both of card 7’s reviews undone');
  assert.deepEqual(state.STUDY.history.map(h => h.card.id), [5], 'only card 5 left in history');
  assert.equal(state.STUDY.queue[0].id, 7, 'card 7 is the current card again');
  assert.equal(state.STUDY.done, 4, 'done went back by the two grades');
});

test('a slow undo request still can’t be double-fired', async () => {
  const { studyUndo, state } = harness(null, { undoDelay: 40 });
  const a = studyUndo();
  await new Promise(r => setTimeout(r, 5));
  const b = studyUndo();                 // fired while the first is mid-request
  await Promise.all([a, b]);
  assert.equal(state.undoCalls, 1);
  assert.equal(state.STUDY.history.length, 1);
});
