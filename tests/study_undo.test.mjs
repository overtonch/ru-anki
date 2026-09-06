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

function harness() {
  const state = {
    undoCalls: 0,
    STUDY: {
      _busy: false, done: 5, practice: false,
      history: [
        { card: { id: 1 }, rating: 3 },
        { card: { id: 2 }, rating: 3 },
      ],
      queue: [{ id: 3 }, { id: 4 }],
      i: 0,
    },
  };
  const env = {
    STUDY: state.STUDY,
    Date,
    $: () => ({ disabled: false }),
    api: async (path) => {
      if (/\/undo$/.test(path)) { state.undoCalls++; return { card: { id: 2 } }; }
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
  const make = new Function(...Object.keys(env),
    body + '\n;return studyUndo;');
  return { studyUndo: make(...Object.values(env)), state };
}

test('two rapid undo taps revert exactly one card', async () => {
  const { studyUndo, state } = harness();
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
  // the time debounce is ~600ms; simulate a later, deliberate tap
  await new Promise(r => setTimeout(r, 650));
  await studyUndo();
  assert.equal(state.STUDY.history.length, 0, 'second deliberate undo went through');
  assert.equal(state.STUDY.done, 3);
});
