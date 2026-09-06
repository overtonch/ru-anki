"""The extraction/translation engine, invoked headlessly.

We are Claude, invoked via the `claude -p` CLI as a subprocess — this uses the
user's existing Pro/Max login, not a metered API key.

Speed matters a lot here, so every call:
  * overrides the system prompt (`--system-prompt`) so we skip Claude Code's
    large agent scaffold — input drops from ~25k tokens to ~1.5k;
  * disables tools (`--tools ""`), MCP, and session persistence;
  * sets MAX_THINKING_TOKENS=0 — extended "thinking" was the entire bottleneck
    (70-140s/chunk of invisible tokens; ~5s/chunk without it).
The CLI's `--output-format json` wraps the real answer in a `result` string
field — parse the envelope, then parse `result`.
"""
import json
import os
import queue
import random
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

CLAUDE = "claude"
DEFAULT_MODEL = os.environ.get("RU_EXTRACT_MODEL", "sonnet")
WORKERS = int(os.environ.get("RU_EXTRACT_WORKERS", "8"))
THINKING = os.environ.get("RU_EXTRACT_THINKING", "0")  # "0" = off (fast); e.g. "4000" to re-enable


class LLMError(RuntimeError):
    pass


# list prices $/1M tokens (input, output) — for cost *estimates* only; actual
# runs are billed against the Pro/Max subscription, not metered.
PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (15.0, 75.0)}


def _price(model, usage):
    key = next((k for k in PRICES if k in (model or "")), None)
    if not key or not usage:
        return 0.0
    pin, pout = PRICES[key]
    return (usage.get("in", 0) / 1e6) * pin + (usage.get("out", 0) / 1e6) * pout


def run_claude(prompt, system, model=DEFAULT_MODEL, timeout=180):
    """One headless turn with a custom system prompt, no tools, no thinking.
    Returns (text, usage) where usage = {"in","out","think"}. Raises LLMError."""
    env = {**os.environ, "MAX_THINKING_TOKENS": THINKING}
    try:
        proc = subprocess.run(
            [CLAUDE, "-p", prompt,
             "--system-prompt", system,
             "--tools", "",
             "--no-session-persistence", "--strict-mcp-config",
             "--output-format", "json", "--model", model, "--max-turns", "1"],
            capture_output=True, text=True, timeout=timeout,
            stdin=subprocess.DEVNULL, env=env,
        )
    except FileNotFoundError:
        raise LLMError("`claude` CLI not found on PATH")
    except subprocess.TimeoutExpired:
        raise LLMError(f"claude -p timed out after {timeout}s")
    if proc.returncode != 0:
        raise LLMError(f"claude -p exited {proc.returncode}: {proc.stderr.strip()[:400]}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise LLMError(f"claude -p gave non-JSON envelope: {proc.stdout[:400]}")
    if data.get("is_error"):
        raise LLMError(f"claude -p reported error: {data.get('result') or data}")
    u = data.get("usage") or {}
    usage = {
        "in": u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0)
              + u.get("cache_creation_input_tokens", 0),
        "out": u.get("output_tokens", 0),
        "think": (u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
    }
    return data.get("result", ""), usage


# ------------------------------------------------------------------ extraction

EXTRACT_SYSTEM = """You build a Russian vocabulary study list for ONE specific learner: a native English speaker at a strong B2–C1 level in Russian. They already know all common everyday vocabulary and most intermediate vocabulary. Only flag things that would genuinely be new or uncertain to such a learner and worth a flashcard.

You are given an excerpt of a de-overlapped ASR transcript, lines prefixed with a [HH:MM:SS] tag.

Output ONLY pipe-delimited lines, one item per line, and NOTHING else:
SPAN|TRANSLATION|SENTENCE|HH:MM:SS
- TRANSLATION: concise English gloss; "a / b" if ambiguous; gloss idioms by meaning.
- SENTENCE: the single line/utterance where the span occurs, lightly cleaned for a flashcard — restore capitalization and punctuation, and fix ONLY unambiguous ASR mishearings (wrong word boundaries, a clearly wrong homophone). Keep it faithful and short: one sentence, at most ~20 words, no added information, no paraphrase. It MUST still contain an inflected form of SPAN. Never use the "|" character inside SENTENCE.
- HH:MM:SS: copy the tag of the line where the span occurs.

FLAG a word/phrase only if it clears ALL of these:
1. A strong B2–C1 learner would plausibly NOT know it, or would be unsure of its exact meaning.
2. It is worth memorising — i.e. it carries real meaning (not grammatical glue) and could recur.
3. It is NOT a transparent cognate/borrowing an English speaker recognises on sight (стрим, контент, анонс, спонсор, менеджер, эмулировать, логистика, тренд, дедлайн, фейк, etc.). Keep a borrowing only if its Russian sense is genuinely non-obvious.

Good candidates: bookish or literary words (тщетный, сетовать, зиждиться), precise/technical terms (изъян, подлог, вменяемый), vivid colloquialisms and slang (втюхать, движуха, кринж), set idioms (как ни в чём не бывало, спустя рукава), verbs with non-obvious meaning (обеспечить, усугубить, лукавить).

Do NOT flag:
- common or mid-frequency words the learner surely knows (сделать, важный, поэтому, компания, деньги, работать, страна, проблема, друг);
- ordinary adjective+noun / verb+object combinations that are just two normal words together (NOT "критические последствия", NOT "научный журналист", NOT "высокая зарплата", NOT "получить деньги") — if one word is advanced, flag that ONE word;
- proper nouns, names, place names;
- numbers, dates, filler ("ну", "вот", "типа", "как бы").

SPAN is almost always a single word in citation form. Use a multi-word span ONLY for a genuine fixed idiom / set phrase whose meaning is not the sum of its parts (как раз, по большому счёту, иметь в виду, сойти с ума). Multi-word spans should be rare.

If a token looks like an ASR mistake but might be real vocabulary, include it with translation prefixed "(SUSPECT ASR) ".

Be strict. It is fine — good, even — for a simple or repetitive passage to yield nothing. Quality over quantity: a shorter list of genuinely useful items is the goal.
No header, no numbering, no commentary, no code fence. Output nothing if nothing qualifies."""

CHUNK_LINES = 80  # grouped transcript lines per headless call
_TS = re.compile(r"(\d\d):(\d\d):(\d\d)")


def _chunks(transcript, n=CHUNK_LINES):
    lines = [ln for ln in transcript.splitlines() if ln.strip()]
    return ["\n".join(lines[i:i + n]) for i in range(0, len(lines), n)]


def parse_items(text):
    """Parse the model's pipe-delimited output. Handles both the current
    SPAN|TRANSLATION|SENTENCE|HH:MM:SS and the older SPAN|TRANSLATION|HH:MM:SS.
    Ignores any stray prose lines."""
    out = []
    for ln in text.splitlines():
        ln = ln.strip().strip("`").strip()
        if ln.count("|") < 2:
            continue
        parts = [p.strip() for p in ln.split("|")]
        span, tr = parts[0], parts[1]
        if not span or not tr or span.lower() in ("span", "span_text"):
            continue
        # the timestamp is the last field that looks like one
        tsi = next((i for i in range(len(parts) - 1, 1, -1)
                    if _TS.search(parts[i])), None)
        ts, sentence = None, None
        if tsi is not None:
            m = _TS.search(parts[tsi])
            ts = f"{m.group(1)}:{m.group(2)}:{m.group(3)}"
            if tsi >= 3:                       # SPAN|TR|SENTENCE|TS
                sentence = " ".join(parts[2:tsi]).strip() or None
        out.append({
            "span_text": span,
            "is_phrase": 1 if " " in span else 0,
            "translation": tr,
            "sentence": sentence,
            "timestamp_start": ts,
        })
    return out


def _extract_chunk(title, part, decided, discards, recurring, model):
    prompt = f"Video: {title}\n\n{part}" if title else part
    if recurring:
        prompt += ("\n\nRECURRING — these words are said many times across this "
                   "video (count in parens). If one appears in the excerpt above "
                   "and is real vocabulary a B2/C1 learner might not know, INCLUDE "
                   "it even if it reads like a name — but still skip pure proper "
                   f"nouns / character names:\n{recurring}")
    if discards:
        prompt += ("\n\nCALIBRATION — the learner recently REJECTED these as too "
                   "easy or not worth a card. Keep your bar clearly above this "
                   f"level; do not suggest words of comparable difficulty:\n{discards}")
    if decided:
        prompt += f"\n\n(Already covered — do not output these: {decided})"
    text, usage = run_claude(prompt, EXTRACT_SYSTEM, model=model, timeout=180)
    return parse_items(text), usage


def extract_candidates(title, transcript, already_decided, model=DEFAULT_MODEL,
                       progress=None, on_chunk=None, workers=WORKERS, discards=(),
                       recurring=()):
    """Extract over the whole transcript, one headless call per chunk, `workers`
    at a time. A failed chunk is logged and skipped, not fatal.

    progress(done, total, errors)  — once at start and after each chunk.
    on_chunk(items)                — each completed chunk's fresh items, for
                                     incremental persistence.
    Returns (merged_items, errors, usage) where usage = {"in","out","think",
    "cost_est","calls"}. Items have span_text / is_phrase / translation /
    timestamp_start; the sentence is reconstructed downstream.
    """
    decided = ", ".join(list(already_decided)[:120]) if already_decided else ""
    disc = ", ".join(list(discards)[:50]) if discards else ""
    rec = ", ".join(f"{w} ({n}×)" for w, n in recurring) if recurring else ""
    parts = _chunks(transcript)
    total = len(parts)
    merged, seen, errors, done = [], set(), [], 0
    usage = {"in": 0, "out": 0, "think": 0, "cost_est": 0.0, "calls": 0}
    if progress:
        progress(0, total, errors)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_extract_chunk, title, part, decided, disc, rec, model): i
                for i, part in enumerate(parts, 1)}
        for fut in as_completed(futs):
            done += 1
            try:
                items, u = fut.result()
                usage["in"] += u["in"]; usage["out"] += u["out"]
                usage["think"] += u["think"]; usage["calls"] += 1
                usage["cost_est"] += _price(model, u)
            except Exception as e:  # noqa: BLE001
                errors.append(f"chunk {futs[fut]}: {e}")
                items = []
            fresh = []
            for it in items:
                key = it["span_text"].strip().lower()
                if key and key not in seen:
                    seen.add(key)
                    merged.append(it)
                    fresh.append(it)
            if on_chunk and fresh:
                try:
                    on_chunk(fresh)
                except Exception as e:  # noqa: BLE001
                    print(f"[extract] on_chunk failed: {e}")
            if progress:
                progress(done, total, errors)

    usage["cost_est"] = round(usage["cost_est"], 5)
    return merged, errors, usage


# ------------------------------------------------------------------ live lookup

TRANSLATE_SYSTEM = """You gloss ONE Russian word or phrase as used in one specific line, for a B2/C1 learner building a flashcard. The line may be truncated or contain glitches.

Output ONLY one raw JSON object, no fence:
{"span_text": "...", "is_phrase": true/false, "translation": "...", "sentence": "...", "stressed": "...", "dict_form": "..."}
- span_text: clean citation (dictionary) form of what you glossed.
- translation: best contextual English gloss; "a / b" if ambiguous; idioms by meaning.
- sentence: the line lightly cleaned into a short readable Russian sentence containing an inflected form of span_text; if too fragmentary, write a minimal natural one.
- stressed: the word/phrase in the EXACT form it appears in the sentence (not the citation form), with a combining acute accent (U+0301) after the stressed vowel and ё written with its dots. One-syllable words and ё get no accent mark. Use the context for mobile-stress words (голова́ → го́ловы).
- dict_form: the citation/dictionary form (infinitive for verbs, nominative singular for nouns, nominative masculine singular for adjectives) written WITH the U+0301 stress mark and ё-dots. e.g. from "печале́н" → "печа́льный", from "зол" → "злой", from "нужны" → "ну́жный", from "затупи́вшийся" → "затупи́ться". Same as `stressed` only when the word already is its dictionary form. For a fixed phrase, the phrase in its dictionary form."""


TRANSLATE_MODEL = os.environ.get("RU_TRANSLATE_MODEL", "sonnet")


class WarmClaude:
    """A persistent `claude` process fed via stream-json. Repeated small calls
    skip the ~1s spawn+init cost (measured ~2.0s -> ~1.0s per call). One request
    at a time (locked); recycled after `max_calls` or `idle` seconds, and
    respawned on any failure."""

    def __init__(self, system, model, max_calls=60, idle=600):
        self.system, self.model = system, model
        self.max_calls, self.idle = max_calls, idle
        self.proc = None
        self._q = None
        self.calls = 0
        self.last = 0.0
        self.lock = threading.Lock()

    def _spawn(self):
        self.proc = subprocess.Popen(
            [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
             "--verbose", "--system-prompt", self.system, "--tools", "",
             "--strict-mcp-config", "--no-session-persistence", "--model", self.model],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1, env={**os.environ, "MAX_THINKING_TOKENS": THINKING})
        self._q = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc, self._q), daemon=True).start()
        self.calls = 0

    @staticmethod
    def _reader(proc, q):
        try:
            for line in proc.stdout:
                q.put(line)
        finally:
            q.put(None)

    def _kill(self):
        if self.proc:
            for f in (self.proc.stdin, self.proc.stdout):
                try:
                    f.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                self.proc.terminate()
            except Exception:  # noqa: BLE001
                pass
        self.proc = None

    def _stale(self):
        return (self.proc is None or self.proc.poll() is not None
                or self.calls >= self.max_calls
                or (self.last and time.time() - self.last > self.idle))

    def ask(self, content, timeout=45):
        with self.lock:
            for attempt in (1, 2):
                if self._stale():
                    self._kill()
                    self._spawn()
                try:
                    return self._ask_once(content, timeout)
                except LLMError:
                    self._kill()
                    if attempt == 2:
                        raise

    def _ask_once(self, content, timeout):
        p, q = self.proc, self._q
        try:
            p.stdin.write(json.dumps(
                {"type": "user", "message": {"role": "user", "content": content}}) + "\n")
            p.stdin.flush()
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"warm claude write failed: {e}")
        text, end = None, time.time() + timeout
        while True:
            try:
                line = q.get(timeout=max(0.05, end - time.time()))
            except queue.Empty:
                raise LLMError("warm claude timed out")
            if line is None:
                raise LLMError("warm claude stream closed")
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") == "assistant":
                for b in d.get("message", {}).get("content", []):
                    if b.get("type") == "text":
                        text = b["text"]
            elif d.get("type") == "result":
                self.calls += 1
                self.last = time.time()
                if d.get("is_error"):
                    raise LLMError(f"warm claude error: {d.get('result') or d}")
                return text or d.get("result", "")


_WARM_POOL = {}
_WARM_POOL_LOCK = threading.Lock()


def _warm(system, model):
    """A shared persistent `claude` process per (system prompt, model). All the
    small one-shot calls — translate, sentence-clean, word-family, accent — reuse
    one instead of paying spawn+init (~1s) every time."""
    key = (system, model)
    with _WARM_POOL_LOCK:
        w = _WARM_POOL.get(key)
        if w is None:
            w = _WARM_POOL[key] = WarmClaude(system, model)
        return w


def _warm_translator():
    return _warm(TRANSLATE_SYSTEM, TRANSLATE_MODEL)


def prewarm():
    """Spawn the translate process now so the first real lookup is fast."""
    try:
        _warm_translator().ask("Line: Это простой тест.\nWord: простой", timeout=30)
    except Exception as e:  # noqa: BLE001
        print(f"[warm] prewarm failed: {e}")


def _json_repair(s):
    """Best-effort fixes for the ways LLMs mangle JSON: trailing commas, a
    missing comma between two adjacent values (very common across a newline),
    smart quotes used as delimiters, code fences."""
    s = s.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\n?|\n?```$", "", s).strip()
    s = re.sub(r",\s*([}\]])", r"\1", s)                       # trailing comma
    # a value immediately followed by the start of the next one with no comma:
    #   "...."\n  "key":   |   }\n  {   |   true\n  "k"
    s = re.sub(r'("|\d|\btrue\b|\bfalse\b|\bnull\b|[}\]])\s*\n(\s*)(?=["{\[])',
               r'\1,\n\2', s)
    return s


# strict=False tolerates raw newlines / tabs inside string values — LLMs routinely
# put literal line breaks in a multi-paragraph field instead of escaping them.
_DECODER = json.JSONDecoder(strict=False)


def _parse_obj(text):
    i = text.find("{")
    if i < 0:
        raise LLMError(f"no JSON object in model output: {text[:300]}")
    for candidate in (text, text[i:], _json_repair(text[i:])):
        j = candidate.find("{")
        if j < 0:
            continue
        try:
            return _DECODER.raw_decode(candidate, j)[0]
        except json.JSONDecodeError:
            continue
    # the model sometimes emits two objects back-to-back (a "wait, actually…"
    # second attempt). Take the first complete one from every '{' in order.
    for k in range(i, len(text)):
        if text[k] != "{":
            continue
        try:
            return _DECODER.raw_decode(text, k)[0]
        except json.JSONDecodeError:
            continue
    m = re.search(r"\{.*\}", text, re.S)   # last resort: greedy span, repaired
    try:
        return _DECODER.decode(_json_repair(m.group(0)))
    except (json.JSONDecodeError, AttributeError) as e:
        raise LLMError(f"bad JSON object from model ({e}): {text[i:i + 400]}")


def translate_span(sentence, span, model=None):
    """Live-lookup gloss. Uses the warm process; falls back to a one-shot."""
    prompt = f"Line: {sentence}\nThe learner tapped on / typed: {span}"
    try:
        return _parse_obj(_warm_translator().ask(prompt))
    except LLMError:
        text, _ = run_claude(prompt, TRANSLATE_SYSTEM,
                             model=model or TRANSLATE_MODEL, timeout=60)
        return _parse_obj(text)


_CLEAN_SYSTEM = """You are given several Russian ASR excerpts (rough auto-caption text). Each contains the word or phrase the learner is studying, possibly in an inflected form.

For EACH excerpt, output ONE line: the single natural sentence that contains that word, lightly cleaned — restore capitalization and punctuation, fix obvious ASR mis-hearings and word-boundary errors, drop stray filler, keep it faithful and at most ~16 words. It MUST still contain a form of the target word. Do not translate, do not add information, do not merge excerpts.

Output exactly one cleaned sentence per input line, in the same order, numbered "1. ", "2. " … and nothing else."""


_FAMILY_SYSTEM = """Given ONE Russian word in its dictionary form, list the OTHER Russian words a learner who already knows this word would understand WITHOUT a dictionary — i.e. the SAME core meaning, just a different part of speech or an aspect / transparent-nuance prefix.

INCLUDE:
- the noun ⇄ verb ⇄ adjective ⇄ adverb of the same idea (работа / работать / рабочий; красивый / красота / красиво; быстрый / быстро)
- the aspect partner (решить / решать, делать / сделать)
- a prefixed form only if its meaning is still "obviously the same word" (поработать, попробовать)

EXCLUDE (this is the important part):
- prefixed forms whose prefix CHANGES the meaning, so the learner would have to look them up: работать → заработать «to earn», обработать «to process», разработать «to develop»; писать → подписать «to sign», списать «to copy off»
- look-alikes with an unrelated meaning (стать «become» vs статья «article»; мир «peace» vs мириады)
- rare, archaic, bookish or technical derivatives
When unsure, EXCLUDE.

Output ONE raw JSON object, no code fence, nothing else:
{"root": "<root>", "members": ["<dict form>", ...]}
Dictionary forms, lowercase, ё written as е. Include the input word. Usually 3-8 members. Never invent words."""


def _warm_or_oneshot(prompt, system, model, timeout):
    """Warm process first; fall back to a fresh `claude -p` on any warm failure."""
    try:
        return _warm(system, model).ask(prompt, timeout=timeout)
    except LLMError:
        text, _ = run_claude(prompt, system, model=model, timeout=timeout + 20)
        return text


_CARD_MEANING_SYSTEM = """You are refining ONE Russian vocabulary flashcard for an
advanced (B2/C1) English-speaking learner. You get: the target word, its
dictionary form, the sentence where the learner met it, and the current
(often messy, slash-separated) translation.

Output ONE raw JSON object, no code fence, nothing else:
{
  "primary": "the ONE English word or short phrase the learner should recall. The single cleanest translation that fits the VAST MAJORITY of the contexts they will meet this word in. NOT a list, no slashes, no parenthetical alternatives.",
  "primary_is_contextual": false,
  "alt": "other senses of the word, concise, separated by '; ' — plus any set phrase / idiom it commonly appears in (write those as 'фраза — meaning'). Empty string only if the word genuinely has one sense.",
  "context": "the ONE clause or sentence from the given sentence that actually contains the target word, trimmed to at most ~14 words, punctuation/casing cleaned, still natural Russian. If the given sentence is already short, return it unchanged. NEVER invent or translate text — it must be a substring-faithful trim of what you were given (a form of the target word must remain)."
}

`primary`:
- Default: the general, most-frequent meaning (спор -> "argument", not "debate / dispute / controversy"; злоба -> "malice").
- EXCEPTION (rare) — set "primary_is_contextual": true and make `primary` the narrow sense ONLY when the word is used here in a marked / idiomatic / slang / technical way the general meaning would not convey. Then `alt` MUST begin with the general meaning.
- Match register: a bookish word gets a bookish gloss; slang gets slang.

Keep `alt` short — the learner skims it. 2-5 senses maximum."""


def card_meaning(word, dict_form, sentence, current="", model=None):
    """Refine a card's back per the card-format spec: one clean primary meaning,
    a short list of alternatives, and a trimmed context clause.
    -> {"primary", "primary_is_contextual", "alt", "context"}."""
    prompt = (f"Target word: {word}\nDictionary form: {dict_form}\n"
              f"Sentence: {(sentence or '').strip()}\n"
              f"Current translation: {current or '(none)'}")
    return _parse_obj(_warm_or_oneshot(prompt, _CARD_MEANING_SYSTEM,
                                       model or TRANSLATE_MODEL, timeout=60))


_CARD_MEANINGS_SYSTEM = _CARD_MEANING_SYSTEM.replace(
    "You are refining ONE Russian vocabulary flashcard",
    "You are refining a BATCH of Russian vocabulary flashcards, one at a time",
).replace(
    "Output ONE raw JSON object, no code fence, nothing else:\n{",
    'Output ONE raw JSON ARRAY, no code fence, nothing else. One object per card, '
    'in the given order, each shaped:\n{\n  "n": <the card number>,',
)


def card_meanings(items, model=None):
    """Batched card_meaning. items: [(word, dict_form, sentence, current), …].
    -> list aligned to items; an unparsable entry is None."""
    if not items:
        return []
    blocks = []
    for i, (w, df, s, cur) in enumerate(items, 1):
        blocks.append(f"CARD {i}\nword: {w}   dict: {df}   current: {cur or '(none)'}\n"
                      f"sentence: {(s or '').strip()}")
    text = _warm_or_oneshot("\n\n".join(blocks), _CARD_MEANINGS_SYSTEM,
                            model or TRANSLATE_MODEL, timeout=120)
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise LLMError(f"no JSON array in card_meanings output: {text[:300]}")
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError(f"bad card_meanings JSON ({e}): {m.group(0)[:300]}")
    by_n = {int(o["n"]): o for o in arr if isinstance(o, dict) and "n" in o}
    return [by_n.get(i + 1) for i in range(len(items))]


_GLOSS_OPTIONS_SYSTEM = """You are helping a learner pick the best English gloss
for the front-of-recall side of a Russian flashcard.

Given the target word, its dictionary form, the sentence it was met in, and the
gloss currently on the card, output 4-6 SHORT alternative English glosses they
might prefer instead — the single word or 2-3 word phrase they'd want to recall.

- Ordinary, natural English. No slashes, no parentheticals, no part-of-speech
  labels, no example sentences.
- Cover the spread: the plainest general translation, a more idiomatic one, a
  register-matched one, and the narrow in-context sense if it differs.
- Do NOT repeat the current gloss. Keep each under ~4 words.

Output ONE raw JSON array of strings, nothing else: ["…", "…", …]"""


def gloss_options(word, dict_form="", sentence="", current="", model=None):
    """A few alternative short English glosses for a flashcard, best-first.
    -> [str]; [] on failure."""
    prompt = (f"Target word: {word}\nDictionary form: {dict_form or word}\n"
              f"Sentence: {(sentence or '').strip() or '(none)'}\n"
              f"Current gloss: {current or '(none)'}")
    try:
        text = _warm_or_oneshot(prompt, _GLOSS_OPTIONS_SYSTEM,
                                model or TRANSLATE_MODEL, timeout=40)
        m = re.search(r"\[.*\]", text, re.S)
        arr = json.loads(m.group(0)) if m else []
    except (LLMError, json.JSONDecodeError, AttributeError):
        return []
    out, seen = [], {(current or "").strip().lower()}
    for x in arr:
        s = str(x).strip().strip('".')
        if s and s.lower() not in seen and len(s) <= 40:
            seen.add(s.lower())
            out.append(s)
    return out[:6]


def word_family(word, model=None):
    """-> (root, [member lemmas]) for a Russian word. Tries the warm process
    first (fast); if its response doesn't parse (the warm channel occasionally
    hands back a stale/doubled reply under load), a single fresh one-shot
    `claude -p` call — which never shares state with anything else — settles
    it."""
    m = model or TRANSLATE_MODEL
    prompt = f"Word: {word}"
    try:
        obj = _parse_obj(_warm_or_oneshot(prompt, _FAMILY_SYSTEM, m, timeout=40))
    except LLMError:
        text, _ = run_claude(prompt, _FAMILY_SYSTEM, model=m, timeout=45)
        obj = _parse_obj(text)
    fold = lambda s: (s or "").strip().lower().replace("ё", "е").replace("́", "")
    members = [fold(mm) for mm in (obj.get("members") or []) if isinstance(mm, str) and mm.strip()]
    members = [mm for mm in members if all("а" <= ch <= "я" or ch == "-" for ch in mm)]
    return fold(obj.get("root")), sorted(set(members) | {fold(word)})


def clean_sentences(word, excerpts, model=None):
    """Clean each raw excerpt into one flashcard sentence containing `word`.
    Returns a list aligned to `excerpts` (best-effort; missing -> '')."""
    if not excerpts:
        return []
    numbered = "\n".join(f"{i + 1}. {e}" for i, e in enumerate(excerpts))
    prompt = f"Target word: {word}\n\nExcerpts:\n{numbered}"
    text = _warm_or_oneshot(prompt, _CLEAN_SYSTEM, model or TRANSLATE_MODEL,
                            timeout=45)
    by_num = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", ln.strip())
        if m:
            by_num[int(m.group(1))] = m.group(2).strip().strip('"')
    return [by_num.get(i + 1, "") for i in range(len(excerpts))]


_PASSAGE_SYSTEM = """Translate the Russian passage into natural, fluent English. Output ONLY the English translation — no preamble, no notes, no the original text, no quotation marks."""


def translate_passage(text, model=None):
    """Straight RU->EN translation of a sentence / paragraph (comprehension check)."""
    return _warm_or_oneshot(text.strip(), _PASSAGE_SYSTEM,
                            model or TRANSLATE_MODEL, timeout=60).strip().strip('"')


_LYRIC_SYSTEM = """You explain ONE line of a Russian song to a B2/C1 learner who is studying the lyrics. You get the whole song for context and one TARGET LINE to explain.

Songs pack meaning tightly and use words in marked, poetic or slangy ways, so go beyond a dictionary translation. Explain what THIS line is really doing.

Output ONE raw JSON object, no code fence, nothing else:
{
  "translation": "a natural, faithful English rendering of the target line",
  "gist": "1-3 sentences: what the speaker is actually saying/feeling here, the register and undertone (bragging, longing, defiance, irony, tenderness, threat, self-pity...), what they're boasting about or lamenting, who 'ты'/'они' refers to if the song makes it clear",
  "notes": ["each string = ONE concrete callout about THIS line: a double/triple entendre or pun and both readings; an idiom or set phrase and its literal vs real meaning; slang / prison-slang / obscenity and its force; a word used in an unusual or archaic sense; a cultural, historical, literary or musical reference; a grammatical quirk that changes the meaning. Omit anything obvious. Empty list if the line is plain."]
}

Be specific and concise. Quote the Russian fragment you're discussing inside a note. Never pad. If the line is genuinely plain, give the translation, a one-line gist, and "notes": []."""


def explain_lyric(target_line, full_lyrics, title="", artist="", model=None):
    """Deep read of one lyric line in the context of the whole song ->
    {"translation","gist","notes":[...]}. One headless call, memoised by caller."""
    head = " — ".join(x for x in (artist, title) if x)
    prompt = (f"SONG: {head}\n\nFULL LYRICS:\n{full_lyrics.strip()[:6000]}\n\n"
              f"TARGET LINE:\n{target_line.strip()}")
    text = _warm_or_oneshot(prompt, _LYRIC_SYSTEM, model or TRANSLATE_MODEL,
                            timeout=90)
    obj = _parse_obj(text)
    notes = [str(n).strip() for n in (obj.get("notes") or []) if str(n).strip()]
    return {
        "translation": (obj.get("translation") or "").strip(),
        "gist": (obj.get("gist") or "").strip(),
        "notes": notes,
    }


# ------------------------------------------------------------------ stress marks

_ACCENT_SYSTEM = """You mark Russian lexical stress for a learner's flashcard hint.

Input: numbered lines, each "WORD — context sentence" (context may be blank).
For each line output "N. FORM" where FORM is exactly WORD (the token before the
dash) — SAME lemma, SAME ending, do NOT re-inflect it to match the sentence —
with only these changes:
- a combining acute accent (U+0301) right after the stressed vowel (a
  one-syllable word gets none; ё is never additionally accented)
- ё written with its dots where it belongs

Use the context ONLY to choose between stress positions of a homograph
(за́мок «castle» vs замо́к «lock»; бо́льшая vs больша́я). Output only the numbered
lines, nothing else."""


def accent_words(items, model=None):
    """items: [(word, sentence), …]. -> list of stressed forms aligned to items
    (best-effort; an entry that can't be parsed comes back as '')."""
    if not items:
        return []
    numbered = "\n".join(
        f"{i + 1}. {w} — {(s or '').strip()}".rstrip(" —")
        for i, (w, s) in enumerate(items))
    text = _warm_or_oneshot(numbered, _ACCENT_SYSTEM, model or TRANSLATE_MODEL,
                            timeout=40)
    by_num = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", ln.strip())
        if m:
            by_num[int(m.group(1))] = m.group(2).strip().strip('"')
    return [by_num.get(i + 1, "") for i in range(len(items))]


def accent_word(word, sentence="", model=None):
    return (accent_words([(word, sentence)], model=model) or [""])[0]


_DICT_FORM_SYSTEM = """For each numbered line "WORD — context" output "N. FORM".

FORM is the Russian DICTIONARY / citation form of WORD:
- verbs (incl. participles and gerunds): the infinitive
- nouns: nominative singular
- adjectives (incl. short forms, comparatives): nominative masculine singular
- adverbs, particles, pronouns: their normal headword form
- a fixed multi-word phrase: the phrase in its dictionary form

Write FORM WITH a combining acute accent (U+0301) right after the stressed vowel
and ё spelled with its dots. A one-syllable form and ё take no added mark.

Use the context ONLY to disambiguate: a homograph (за́мок «castle» / замо́к
«lock»), or a short adjective vs an unrelated word (зол → злой, NOT the noun
зло; на́чал → нача́ть). Output only the numbered lines, nothing else."""


def dict_forms(items, model=None):
    """items: [(word, sentence), …] -> the stressed dictionary/citation form of
    each, aligned to items (best-effort; unparsable -> '')."""
    if not items:
        return []
    numbered = "\n".join(
        f"{i + 1}. {w} — {(s or '').strip()}".rstrip(" —")
        for i, (w, s) in enumerate(items))
    text = _warm_or_oneshot(numbered, _DICT_FORM_SYSTEM, model or TRANSLATE_MODEL,
                            timeout=45)
    by_num = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", ln.strip())
        if m:
            by_num[int(m.group(1))] = m.group(2).strip().strip('"')
    return [by_num.get(i + 1, "") for i in range(len(items))]


def dict_form(word, sentence="", model=None):
    return (dict_forms([(word, sentence)], model=model) or [""])[0]


_STRESS_FORMS_SYSTEM = """For each numbered line "WORD — sentence" output "N. SURFACE | DICT".

SURFACE = WORD in the EXACT inflected form it takes in the sentence.
DICT     = its dictionary / citation form (infinitive for verbs incl. participles
           and gerunds; nominative singular for nouns; nominative masculine
           singular for adjectives incl. short forms and comparatives; headword
           form for everything else). For a fixed phrase, the phrase's dict form.

Write BOTH with a combining acute accent (U+0301) right after the stressed vowel
and ё spelled with its dots. A one-syllable form and ё take no added mark. If
SURFACE already is the dictionary form, repeat it.

Use the sentence to place mobile stress (голова́ → го́ловы) and to disambiguate
homographs / short-adjective-vs-noun (зол → зол | злой, NOT зло). When a line
ends with "[means: …]", that gloss is authoritative for which word it is
(косой [means: scythe] → косо́й | коса́, NOT the adjective). Output only the
numbered lines, "SURFACE | DICT" separated by a pipe."""


_LEARN_ORDER_SYSTEM = """You are ordering Russian vocabulary for a learner by how
EARLY they should learn each item. For each numbered "WORD — meaning" line, output
"N. SCORE" where SCORE is 0-100:

 90-100  core survival vocabulary — a beginner needs it in the first weeks
         (быть, хотеть, говорить, день, вода, большой, хорошо, потому что)
 70-89   very common, everyday A2 words used constantly in speech
 50-69   solid, useful intermediate (B1) vocabulary
 30-49   less frequent B2 words — known by fluent speakers, not daily
 10-29   uncommon: bookish, formal, technical, regional
  0-9    rare, archaic, poetic, slang, or highly specialised

Judge by real spoken-and-written frequency and usefulness for communication, NOT
by how the word looks. A transparent-looking cognate can still be rare; a short
plain word can be advanced. Multi-word phrases: score the phrase as a unit.

Judge a word by its WHOLE FAMILY, not this one part of speech. The verb, noun,
adjective and adverb of one idea (решать / решение / решительный; презирать /
презрение / презрительный) all mean the same thing — a learner meets the idea
as often as its COMMONEST form appears. Score them at the level of that
commonest form, not lower just because this particular derived form is rarer.

Output ONLY the numbered "N. SCORE" lines, nothing else."""


def learn_priority(items, model=None):
    """items: [(word, meaning), …] -> an int 0-100 per item (higher = a learner
    should meet it sooner). Absolute scale, so batches are independent. Missing /
    unparsable -> None."""
    if not items:
        return []
    numbered = "\n".join(
        f"{i + 1}. {w} — {(g or '').strip()}".rstrip(" —")
        for i, (w, g) in enumerate(items))
    text = _warm_or_oneshot(numbered, _LEARN_ORDER_SYSTEM, model or TRANSLATE_MODEL,
                            timeout=60)
    by_num = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(-?\d+)", ln.strip())
        if m:
            by_num[int(m.group(1))] = max(0, min(100, int(m.group(2))))
    return [by_num.get(i + 1) for i in range(len(items))]


_VERB_ASPECT_SYSTEM = """For each numbered line "INFINITIVE — meaning" give the
verb's aspect and the OTHER member of its aspect pair.

Output ONE line per input: "N. ASPECT | PARTNER | NOTE"

ASPECT:
  impf  imperfective
  pf    perfective
  both  genuinely bi-aspectual (использовать, велеть, женить, казнить, обещать,
        ранить, исследовать …)
  none  NOT a verb, or only looks verbal (a noun, an adjective, a standalone
        participle) — give "none | — | —"

PARTNER — the dictionary form of the counterpart in the other aspect, WITH a
combining acute accent (U+0301) after the stressed vowel and ё spelled with dots
(one syllable / ё take no mark):
  делать→сде́лать, сделать→де́лать, рассказать→расска́зывать,
  купить→покупа́ть, встретиться→встреча́ться
  Use "—" when there is no single clean partner: bi-aspectual verbs; verbs with
  no real pair (стоить, зависеть, значить, принадлежать, нуждаться); and the
  verbs of motion идти/ходить/ехать/ездить/бежать/… and their prefixed forms
  (those have their own trainer) → "—".

NOTE — a SHORT qualifier (≤6 words) or "—". Use it for things like
  "no common perfective", "no common imperfective", "colloquial",
  "or <second partner>" when a doublet is common. Most lines are "—".

Judge from the infinitive and its meaning. Output ONLY the numbered lines."""


def verb_aspect(items, model=None):
    """items: [(infinitive, meaning), …] -> a dict per item aligned to items:
    {"aspect": 'impf'|'pf'|'both', "partner": str|None, "note": str|None}, or
    None when the model says it is not a verb / could not parse."""
    if not items:
        return []
    numbered = "\n".join(
        f"{i + 1}. {w} — {(g or '').strip()}".rstrip(" —")
        for i, (w, g) in enumerate(items))
    text = _warm_or_oneshot(numbered, _VERB_ASPECT_SYSTEM, model or TRANSLATE_MODEL,
                            timeout=60)
    by_num = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", ln.strip())
        if not m:
            continue
        parts = [p.strip().strip('"') for p in m.group(2).split("|")]
        asp = (parts[0] if parts else "").lower()
        if asp not in ("impf", "pf", "both"):
            by_num[int(m.group(1))] = None
            continue
        partner = (parts[1].strip() if len(parts) > 1 else "")
        note = (parts[2].strip() if len(parts) > 2 else "")
        by_num[int(m.group(1))] = {
            "aspect": asp,
            "partner": None if partner in ("", "—", "-") else partner,
            "note": None if note in ("", "—", "-") else note,
        }
    return [by_num.get(i + 1) for i in range(len(items))]


def stress_forms(items, model=None):
    """items: [(word, sentence)] or [(word, sentence, gloss)] ->
    [(surface_stressed, dict_stressed), …] aligned to items. The optional gloss
    disambiguates homographs (косо́й «slanting» vs коса́ «scythe»). Unparsable
    entries come back as ('', '')."""
    if not items:
        return []
    lines = []
    for i, it in enumerate(items):
        w, s = it[0], it[1] if len(it) > 1 else ""
        g = it[2] if len(it) > 2 else ""
        ln = f"{i + 1}. {w} — {(s or '').strip()}".rstrip(" —")
        if g:
            ln += f"   [means: {g.strip()}]"
        lines.append(ln)
    numbered = "\n".join(lines)
    text = _warm_or_oneshot(numbered, _STRESS_FORMS_SYSTEM, model or TRANSLATE_MODEL,
                            timeout=50)
    by_num = {}
    for ln in text.splitlines():
        m = re.match(r"\s*(\d+)[.)]\s*(.+)", ln.strip())
        if not m:
            continue
        parts = [p.strip().strip('"') for p in m.group(2).split("|")]
        by_num[int(m.group(1))] = (parts[0], parts[1] if len(parts) > 1 else parts[0])
    return [by_num.get(i + 1, ("", "")) for i in range(len(items))]


# ---------------------------------------------------------- stress over a passage

_STRESS_RESOLVE_SYSTEM = """You place the stress mark on Russian words that a
dictionary could not resolve on its own — either because the spelling is a
homograph (за́мок castle / замо́к lock; до́ма at home / дома́ houses; на́чал
started / начала́ she started; вре́мени; по́сле) or because the word is not in the
dictionary (a name, a rare or foreign word).

You get a numbered list of "word — the sentence it appears in". For EACH, give
that word with the correct stress FOR THAT SENTENCE.

The mark is U+0301 (combining acute) immediately AFTER the stressed vowel:
на́чал, дире́ктора, письмо́. One-syllable words and ё take no mark. Keep the
word's exact spelling and case; only add the mark.

Output ONE raw JSON object, nothing else:
{"marks": ["<word with mark>", "<word with mark>", ...]}
one entry per input line, same order. If a word genuinely has one syllable,
return it unchanged."""


def stress_resolve(items):
    """items: [(word, sentence), …] -> [accented_word, …] aligned to items.
    Only for homographs / out-of-dictionary words; the caller keeps every other
    word exactly as the dictionary marked it."""
    if not items:
        return []
    numbered = "\n".join(f"{i + 1}. {w} — {(s or '').strip()}"
                         for i, (w, s) in enumerate(items))
    out = [w for w, _ in items]
    for attempt in range(2):
        body = numbered if attempt == 0 else numbered + "\n\nReturn ONLY the JSON object."
        try:
            obj = _parse_obj(run_claude(body, _STRESS_RESOLVE_SYSTEM,
                                        model=TRANSLATE_MODEL, timeout=90)[0])
        except LLMError:
            continue
        marks = obj.get("marks") if isinstance(obj, dict) else None
        if isinstance(marks, list) and len(marks) == len(items):
            for i, m in enumerate(marks):
                m = (m or "").strip()
                if m and m.replace("́", "").lower() == items[i][0].replace("́", "").lower():
                    out[i] = m
            return out
    return out


# ---------------------------------------------- reformulation speaking practice

_SPEAK_PROMPT_SYSTEM = """You generate ONE prompt for a Russian speaking-practice drill.

WHO IT'S FOR: a native English speaker in the US who speaks Russian with his
girlfriend's Russian family and friends. His reading is advanced; his SPEAKING is
well behind that, so the thought must stay easy to produce out loud. The drill:
he reads a short concrete THOUGHT in English, then says it in Russian from memory.

You'll get a SCENE SEED — a rough place / moment / topic. It's raw material, not
a script. Turn it into ONE specific real thought a person would actually have or
say in that situation. Make it vivid and personal: a name, a detail, a number, a
time. If the seed says "surprise me", invent something fresh and concrete of your
own — an ordinary slice of life, not necessarily any of the usual categories.

Range of life to draw on (not a checklist — just so you don't get stuck):
noticing something on a walk, a small thing that happened today, a plan forming,
a mild annoyance, asking someone about their life, changing your mind, comparing
two things, a memory surfacing, being polite about something awkward, weekend
stuff, a recommendation, catching up, an honest opinion, recounting a small
mishap, asking to change something, reacting to what someone said, the weather,
a purchase, a habit, someone's kid, an old apartment, a photo you took, getting
older, a stranger's kindness, transit, sleep, a show, prices, a coworker, AI,
money, a trip. Wander widely. Do NOT default to "I bought X", "the meeting was
useless", or "my friend flaked" — those are overused.

THE ENGLISH MUST SOUND LIKE A REAL AMERICAN THOUGHT. Do NOT reverse-engineer it
from a Russian construction. Write what a native English speaker would actually
think or say — casual, contractions, the rhythm of speech. Read it back: if it
sounds stilted or "translated", rewrite it. Never bend the English to smuggle in
a grammar point — the drill's feedback step handles the Russian rephrasing.

Keep it SHORT and PRODUCIBLE — one idea, said from memory. Follow the LEVEL for
how much to pack in. Don't reuse a recent prompt's situation or vocabulary.

Output ONE raw JSON object, no code fence, nothing else:
{
  "text": "the thought, in natural idiomatic English",
  "hint": "OPTIONAL, may be empty. One short line naming the single thing a Russian speaker would most likely build differently here (structure, aspect, a set phrase, reported speech) — shown only AFTER the attempt. \"\" if nothing stands out."
}"""

# raw material sampled per call so no two prompts start from the same place
_SPK_PLACES = (
    "walking around your neighborhood", "at a café", "on the metro", "in a park",
    "stuck in a long line", "at the grocery store", "in the car in traffic",
    "on an aimless walk", "at a friend's kitchen table", "in a museum",
    "at the gym", "on the balcony in the evening", "at the airport",
    "in a bookshop", "at an outdoor market", "walking home late",
    "at a birthday dinner", "on a video call with family", "in a taxi",
    "in the doctor's waiting room", "getting a haircut", "in the stairwell of your building",
    "at a wedding", "at a bar with one friend", "picking someone up from the station",
    "at the pharmacy", "on a train between cities", "in your kitchen cooking",
    "at a playground with a kid", "on a rooftop", "in line for coffee",
    "at a hardware store", "on the phone with a repairman", "at a house party",
    "on a hike", "at the post office", "in a hotel room on a trip",
    "at a dacha / country house", "waiting for a table",
)
_SPK_MOMENTS = (
    "you notice something and want to point it out",
    "you're telling a small story from earlier today",
    "you're thinking a plan out loud",
    "you're a little annoyed and venting",
    "you're asking the other person about their life",
    "you just changed your mind about something",
    "you're comparing two things",
    "a memory surfaced and you want to share it",
    "you're smoothing over something slightly awkward",
    "you're genuinely excited about something small",
    "you're unsure and thinking it through",
    "you're reacting to what they just said",
    "you're giving your honest take",
    "you're explaining why you can't do something",
    "you're describing how the weekend went",
    "you're recommending something",
    "you realize you've changed since last year",
    "something went a bit wrong and you're recounting it",
    "you want to change or return something",
    "you're catching up after a long time",
    "you're making an offer to help",
    "you're wondering out loud whether it's worth it",
    "you're gently disagreeing",
    "you're describing someone you saw",
)
_SPK_TOPICS = (
    "food", "the weather turning", "a purchase", "your phone or a gadget",
    "a neighbor", "public transit", "your sleep", "a show or book",
    "prices going up", "a coworker", "your Russian", "a trip you're planning",
    "a habit you're building or breaking", "someone's kid", "an old apartment",
    "a photo you took", "getting older", "a stranger's small kindness",
    "being swamped", "a disagreement with someone", "the news", "AI",
    "a hobby", "money", "a family tradition", "exercise", "a mistake you made",
    "a place that closed down", "a gift", "a smell that reminded you of something",
    "a noise in the building", "your commute", "a plant you're keeping alive",
    "leftovers", "a text you're not sure how to answer", "the light this time of year",
)


def speak_scene_seed(rng=None):
    """A fresh combination of raw ingredients, so each prompt starts somewhere new."""
    rng = rng or random
    if rng.random() < 0.18:
        return "surprise me — invent an ordinary, specific slice of life"
    bits = []
    if rng.random() < 0.7:
        bits.append(f"place: {rng.choice(_SPK_PLACES)}")
    bits.append(f"moment: {rng.choice(_SPK_MOMENTS)}")
    if rng.random() < 0.75:
        bits.append(f"topic: {rng.choice(_SPK_TOPICS)}")
    return " · ".join(bits)

# how much to pack in / how simple to keep the English, by CEFR speaking level
SPEAK_LEVELS = {
    "a2": ("A2 speaking. ONE very short sentence — usually 4 to 8 words. A single "
           "basic fact, need, or feeling. Present tense, or the simplest past "
           "('I went', 'I bought', 'it was'). Only the most common words. NO "
           "'because', NO two clauses, NO opinions, NO plans with conditions. "
           "A survival phrase or a one-fact statement.\n"
           "Examples of the right size:\n"
           "  \"I'm really tired today.\"\n"
           "  \"This soup is very tasty.\"\n"
           "  \"I bought bread and milk.\"\n"
           "  \"Where is the bathroom?\"\n"
           "  \"We're going home tomorrow.\"\n"
           "  \"It's cold outside.\"\n"
           "The SCENE SEED is only a hint of topic — keep the sentence trivially "
           "simple no matter how rich the seed is."),
    "a2plus": ("A2+ speaking — between A2 and B1. ONE sentence, roughly 8 to 14 "
               "words. One concrete idea, and you MAY add one simple reason or "
               "link: 'because', 'so', 'but', 'and then'. Present and simple past "
               "only. Common everyday words. NO second full sentence, NO opinions "
               "with nuance, NO conditionals or hypotheticals.\n"
               "Examples of the right size:\n"
               "  \"I went to the store, but it was already closed.\"\n"
               "  \"My back hurts because I sat at the computer all day.\"\n"
               "  \"We're going to grandma's on Sunday and staying for dinner.\"\n"
               "  \"I can't come tonight because I have to work late.\"\n"
               "  \"The bakery near us closed, so now we walk to the other one.\"\n"
               "The SCENE SEED is a hint of topic — keep it to one simple "
               "sentence no matter how rich the seed is."),
    "b1": ("B1 speaking. ONE short sentence, occasionally two very short ones. "
           "A single concrete fact or need: what you did, bought, ate, where you "
           "went, a simple plan, a simple feeling, a basic request. Present or "
           "simple past. Everyday high-frequency words only. NO idioms, NO "
           "hypotheticals, NO opinions with nuance, NO subordinate clauses beyond "
           "a plain 'because'. Think: a text message to a friend.\n"
           "Examples of the right size:\n"
           "  \"I went to the store and bought bread and milk.\"\n"
           "  \"I was at work all day. I'm really tired.\"\n"
           "  \"Can I get a coffee and the check?\"\n"
           "  \"We're going to my mom's place on Sunday.\""),
    "b2": ("B2 speaking. One or two connected sentences, one main idea plus maybe "
           "a reason or a small consequence. Still concrete everyday life. A "
           "simple opinion, a plan with a condition, or reacting to news is fine. "
           "A few common set phrases OK. Keep subordinate clauses simple.\n"
           "Examples of the right size:\n"
           "  \"I couldn't come yesterday because I felt sick. I'll definitely come Saturday.\"\n"
           "  \"We bought a new sofa last week. It was expensive, but it's really comfortable.\"\n"
           "  \"How's your knee? Did you go to the doctor?\""),
    "c1": ("C1 speaking. Up to three sentences, a fuller thought — an opinion with "
           "a reason, a mild hypothetical, a small story with a turn. Idiomatic "
           "English is welcome. Still one situation, not an essay."),
}


def speaking_prompt(recent=None, seed=None, level="a2", model=None):
    """-> {"text", "hint"} — one concrete thought to say in Russian. `seed` is a
    scene seed from speak_scene_seed(); a fresh one is drawn if not given."""
    lvl = SPEAK_LEVELS.get((level or "a2").lower(), SPEAK_LEVELS["a2"])
    ask = (f"LEVEL: {lvl}\n\nSCENE SEED: {seed or speak_scene_seed()}\n\n"
           "Write the thought.")
    if recent:
        ask += ("\n\nJust did these — make this clearly different in situation "
                "and vocabulary:\n" + "\n".join(f"- {r}" for r in list(recent)[:10]))
    # a fresh process (not the warm pool) so nothing carries over between calls
    text, _ = run_claude(ask, _SPEAK_PROMPT_SYSTEM, model=model or TRANSLATE_MODEL,
                         timeout=45)
    return _parse_obj(text)


_SPEAK_FEEDBACK_SYSTEM = """You are a patient, precise Russian tutor grading ONE attempt in a speaking drill.

You get:
  THOUGHT  — the English thought the learner was asked to express
  ATTEMPT  — what they wrote/said in Russian (may contain speech-to-text errors)

Do BOTH of these:

A) NATIVE VERSION — write ONE way a native speaker would express the ORIGINAL
   THOUGHT (not a translation of the ATTEMPT — of the THOUGHT). Voice: neutral
   everyday spoken Russian — how an educated native casually says it to family or
   a friend. Not slangy, not bookish. Say it the way a Russian speaker actually
   would: they may split it, reorder it, drop a clause, or build it around a
   different verb than the English suggests — don't stay glued to the English.
   ≤ ~25 words. Also give a literal-ish English `gloss`.

   BE LENIENT — this is spoken-language practice typed on a phone or via
   speech-to-text:
   - IGNORE punctuation, capitalization, ё vs е, and obvious typos / STT
     mishearings. Never make them a correction. If one genuinely creates
     ambiguity or is worth a passing note, mention it in ONE clause of `general`,
     not as a correction.
   - The learner lives in the US and speaks Russian with bilingual family, so
     dropping an English word into a Russian sentence ("снял apartment",
     "у меня appointment в среду") is FINE and deliberate — do NOT correct it.
     At most ONCE per attempt, and only for a genuinely high-value word, you may
     add a "style" correction offering the Russian equivalent. Never "hard".
   Only a real grammatical error or a wrong word gets a correction.

B) CORRECTIONS — compare the ATTEMPT to how it SHOULD read.
   Each correction fixes exactly ONE thing (don't bundle an aspect fix and a
   word-order fix into one). Two severities:
   - "hard": an actual error (wrong case, aspect, conjugation, agreement,
     preposition, sentence-breaking word order, a wrong word) OR a choice the
     native version avoids. Non-negotiable.
   - "style": grammatical and understandable, but not how a native would put it.
     Surface AT MOST 3 of these — the ones most worth fixing. Don't nitpick.
   Three tiers, set `tier`:
   - "lexical": a word/phrase swapped for a more natural or precise one
   - "grammar": case, aspect, conjugation, agreement, preposition, spelling
   - "clarity": the attempt changed or lost the original meaning (not just
     phrasing) — explain what shifted
   `category` (for tracking): one of aspect, case, word-order, agreement,
   conjugation, lexical, preposition, spelling, clarity, other.
   `original` MUST be copied VERBATIM from the ATTEMPT (an exact substring). Keep
   it as SHORT as possible — just the words that change. For a clarity issue or a
   missing word, `original` may be "".
   The `original` spans of different corrections MUST NOT OVERLAP. List
   corrections in the order their `original` appears in the ATTEMPT (empty-
   `original` ones last).
   `explanation`: 1-2 sentences — the rule, or what meaning shifted. Concrete.

C) DIFF — the ATTEMPT as an ordered list, for inline strikethrough→suggestion.
   Each element is either
     {"s": "<a run of the attempt, verbatim>"}   (unchanged)
   or
     {"c": <1-based index of a correction whose `original` is non-empty>}
   Concatenating `s` for text runs and that correction's `original` for `c`
   elements, in order, MUST reproduce the ATTEMPT EXACTLY — same characters, same
   spaces, nothing added or dropped. EVERY correction that has a non-empty
   `original` appears exactly once, in attempt order. This includes every "hard"
   correction. Indices are 1-based in the order you listed them in B.

D) meaning: "ok" if the attempt conveyed the original thought, "drifted" if it
   changed or lost part of it.

E) general: 2-3 sentences on PATTERNS in this specific attempt (e.g. "you reach
   for imperfective by default even when the point is that it's finished"). Short.
   If the attempt was strong, say what was good. No generic advice.

Output ONE raw JSON object, no code fence, nothing else:
{
  "meaning": "ok",
  "native": "...",
  "gloss": "...",
  "corrections": [
    {"tier": "grammar", "category": "aspect", "original": "<verbatim>", "corrected": "...", "explanation": "...", "severity": "hard"}
  ],
  "diff": [ {"s": "..."}, {"c": 1}, {"s": "..."} ],
  "general": "..."
}
If the ATTEMPT is empty or not Russian, return empty corrections/diff, meaning
"drifted", still give a `native` version, and say so in `general`."""


_SPEAK_LEVEL_GUIDE = {
    "a2": "The learner is at A2 speaking and finds this hard. ONLY correct things "
          "that are actually wrong or would confuse a listener. ZERO stylistic "
          "corrections. Explanations one plain sentence, no grammar jargon. Be "
          "encouraging in `general`.",
    "a2plus": "The learner is between A2 and B1. Fix real errors only; at most 1 "
              "stylistic note, and only if it clearly helps. Simple explanations, "
              "little jargon. Encouraging tone.",
    "b1": "The learner is at B1 speaking. Fix real errors, but go EASY on style — "
          "at most 1 stylistic correction, only if it really matters. Keep "
          "explanations simple.",
    "b2": "The learner is at B2 speaking. Normal feedback; at most 2-3 stylistic "
          "corrections.",
    "c1": "The learner is at C1 speaking. Push on naturalness; up to 3 stylistic "
          "corrections.",
}


def speaking_feedback(thought, attempt, level="a2", model=None):
    """The core grading call. -> the raw dict from _SPEAK_FEEDBACK_SYSTEM."""
    guide = _SPEAK_LEVEL_GUIDE.get((level or "a2").lower(), "")
    prompt = (f"{guide}\n\nTHOUGHT:\n{(thought or '').strip()}\n\n"
              f"ATTEMPT:\n{(attempt or '').strip()}")
    m = model or TRANSLATE_MODEL
    last = None
    for attempt_n in range(3):
        p = prompt if attempt_n == 0 else (
            prompt + "\n\nReturn ONLY one valid JSON object — no prose, no code "
            "fence. Every string on one line, all quotes inside text escaped, "
            "a comma between every array element and object field.")
        try:
            text, _ = run_claude(p, _SPEAK_FEEDBACK_SYSTEM, model=m, timeout=170)
            return _parse_obj(text)
        except LLMError as e:
            last = e
    raise last


_SPEAK_SESSION_SYSTEM = """You pick the best flashcards from a whole Russian
speaking-practice session.

You get every correction the learner got across several attempts (each: the
English thought, their attempt, a good native version, and the corrections with
severity hard/style).

Choose EXACTLY 5 cards, ranked by leverage:
  • "high" (aim for 3) — a real error that RECURRED across attempts or that the
    native version clearly avoids; a set phrase / collocation / the right aspect
    for a situation that keeps coming up. Clearly worth owning.
  • "medium" (the other ~2) — useful but more optional: a nicer word, a smoother
    turn of phrase.
  MERGE near-duplicates across attempts into ONE card. SKIP one-offs, rare vocab,
  punctuation/typo noise, and anything tied to a single odd prompt. Dropping an
  English word into Russian (apartment, appointment…) is fine — only card the
  Russian equivalent if it's genuinely high-value.

Each card is a PRODUCTION card — the learner sees the English FRONT and says the
Russian BACK from memory:
  front  — a short, natural English cue for the specific thing to produce. Don't
           give the answer away. A mini-situation or the meaning.
  back   — the natural Russian to say: a real phrase with enough context to be an
           utterance (a few words), fully correct and idiomatic, neutral everyday
           register.
  alternatives — 0-2 OTHER natural ways to say the same thing, when a learner
           could reasonably prefer a different phrasing (a synonym, a more/less
           colloquial turn, keeping vs. dropping a word). Include this ONLY when
           there's a real choice; otherwise use []. Do NOT repeat `back` here.
  why    — one short line: what recurring gap this closes.
  leverage — "high" or "medium".

Output ONE raw JSON object, no code fence, nothing else. Highest-leverage first:
{"cards": [{"front": "...", "back": "...", "alternatives": [], "why": "...", "leverage": "high"}]}"""


def speaking_session_cards(items, model=None):
    """items: [{"thought", "attempt", "reformulation", "corrections": [...]}] for
    the session. -> {"cards": [{"front", "back", "why"}]} (3-5, best first)."""
    blocks = []
    for i, it in enumerate(items, 1):
        lines = [f"ATTEMPT {i}", f"  thought: {it.get('thought', '')}",
                 f"  said: {it.get('attempt', '')}"]
        if it.get("reformulation"):
            lines.append(f"  a good native version: {it['reformulation']}")
        for cr in it.get("corrections", []):
            lines.append(
                f"  - [{cr.get('severity', 'hard')}/{cr.get('category', '?')}] "
                f"{cr.get('was') or '(missing)'} -> {cr.get('now') or ''}"
                f"  ({cr.get('explanation', '')})")
        blocks.append("\n".join(lines))
    prompt = "\n\n".join(blocks) + "\n\nPick exactly 5 cards (about 3 high-leverage)."
    m = model or TRANSLATE_MODEL
    last = None
    for n in range(3):
        p = prompt if n == 0 else prompt + "\n\nReturn ONLY strict JSON."
        try:
            return _parse_obj(run_claude(p, _SPEAK_SESSION_SYSTEM, model=m, timeout=120)[0])
        except LLMError as e:
            last = e
    raise last


# ---------------------------------------------------- grammar drill (forms gym)

_DRILL_SYSTEM = """You write cards for a Russian GRAMMAR drill — a self-graded
flip-through. The learner reads a short English cue plus a few dictionary-form
Russian words, says the whole Russian sentence out loud with the right forms,
then flips to check.

You are given GRAMMAR CONCEPTS to test (each: an id, a CEFR level, a one-line
description of what to make the learner produce, and common traps) and CANDIDATE
WORDS from a frequency band. Make ONE card per concept. Use a candidate word when
it fits the concept naturally; otherwise choose your own vocabulary at roughly
that frequency level (commoner words for a low band, rarer for a high band). Vary
tense / person / number / sentence shape across the batch.

Each card is a JSON object with these fields:
  concept — copy the concept id EXACTLY as given.
  lemma   — the single Russian dictionary-form word this card is really about
            (the verb for a government / aspect concept, the head noun for a case
            ending). One word.
  prompt  — a SHORT, natural English sentence (5-12 words) that makes the tested
            form unambiguous from context. Everyday, concrete.
  given   — the Russian words the learner needs to build the sentence, so the
            ONLY thing tested is FORMS, not word choice. STRICT DICTIONARY FORM:
            nouns → nominative singular ("командир", never "командира"); verbs →
            infinitive; adjectives → masculine nominative singular. A wrong-form
            item gives the answer away — that's a bug.
            INCLUDE every content word for the tested slot, the preposition
            verbatim when a preposition is the point, and any adjective that must
            agree. For an ASPECT concept it is MANDATORY to give BOTH aspect
            partners as ONE string "читать / прочитать" (never a lone verb).
            LEAVE OUT bare subject pronouns (я/ты/он/она/мы/вы/они) and "это"
            unless genuinely load-bearing. Usually 2-4 items.
  answer  — the full natural Russian sentence. Correct, idiomatic, neutral spoken
            register. Short. NO stress marks. May contain a subject pronoun /
            small words not in `given`.
  target  — JSON list of the EXACT answer substring(s) that ARE the graded point
            (the word(s) whose form the learner must get right to pass). 1-2 short
            spans copied verbatim from `answer`. Everything else is just context.
            "Она помогла сестре." → ["сестре"]   "Я прочитал книгу." → ["прочитал"]
  note    — 1-2 tight lines naming the rule and why the form is what it is.
  contrast — ASPECT concepts only: one line with the OTHER aspect in a
            minimally-changed version ("Habitual — 'Я читал книгу каждый вечер'").
            Empty for everything else.

Output ONE raw JSON object, no code fence, nothing else:
{"cards": [{"concept": "...", "lemma": "...", "prompt": "...", "given": ["...","..."], "answer": "...", "target": ["..."], "note": "...", "contrast": ""}]}"""


def _concept_lines(concepts):
    out = []
    for c in concepts:
        line = f"- {c['id']}  [{(c.get('level') or '').upper()}]  {c.get('hint') or c.get('title', '')}"
        for t in (c.get("traps") or [])[:2]:
            line += f"\n    trap: {t}"
        out.append(line)
    return "\n".join(out)


def _drill_call(prompt, model, tries=2, timeout=140):
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(tries):
        p = prompt if i == 0 else prompt + "\n\nReturn ONLY one valid JSON object, strict JSON."
        try:
            return _parse_obj(run_claude(p, _DRILL_SYSTEM, model=m, timeout=timeout)[0])
        except LLMError as e:
            last = e
    raise last


def drill_cards(concepts, rank_lo=1, rank_hi=1000, words=(), model=None):
    """concepts: [{id, level, hint, traps, title}]. -> {"cards": [...]} — one per concept."""
    parts = []
    if words:
        parts.append(f"CANDIDATE WORDS (frequency rank ~{rank_lo}-{rank_hi}): "
                     + ", ".join(words))
    parts.append("GRAMMAR CONCEPTS TO TEST — one card each:\n" + _concept_lines(concepts))
    return _drill_call("\n\n".join(parts), model)


def drill_concept_cards(concept, n=6, words=(), model=None):
    """The learner wants targeted practice on ONE concept — make `n` cards for it,
    each in a different everyday context."""
    parts = [f"Make {n} cards, ALL testing this ONE concept, each a DIFFERENT "
             f"everyday sentence (vary tense, person, number, vocabulary):"]
    parts.append(_concept_lines([concept]))
    if words:
        parts.append("Prefer these words where they fit: " + ", ".join(words))
    return _drill_call("\n\n".join(parts), model, timeout=140)


# back-compat name used by the drill's spaced re-test path
def drill_retest_cards(lemma, gloss, concept, n=3, model=None):
    c = dict(concept) if isinstance(concept, dict) else {"id": str(concept), "hint": ""}
    hint = c.get("hint") or c.get("title") or ""
    c["hint"] = (f"{hint}  — keep the target word '{lemma}'"
                 + (f" ({gloss})" if gloss else "") + " in every card").strip()
    return drill_concept_cards(c, n=n, model=model)


# ------------------------------------------------------------- speaking journal

_JOURNAL_SYSTEM = """You analyse a spoken monologue by an English speaker learning
Russian. They talked freely ("about my day" etc.) and CODE-SWITCHED: Russian
where they could, English words or phrases where they didn't know how to say it.
The text is an automatic transcript, so expect ASR noise — mis-heard words,
missing punctuation, English rendered in Cyrillic ("апартмент" for "apartment").

Do THREE things:

1. SEGMENTS — split the monologue into natural chunks (a sentence or two each).
   For each chunk return:
     ru        — what they actually said, lightly cleaned (punctuation, obvious
                 ASR fixes). Keep their Russian even if wrong. Keep English spans
                 as English (fix "апартмент" back to "apartment").
     en        — a plain English gloss of the whole chunk.
     fix       — "" if the Russian is fine. Otherwise the natural way a native
                 would say that chunk (full corrected Russian).
     issues    — 0-3 short items, each {span, was, now, tier, why}. `tier` is
                 "gap" (they said it in English — `was` is the English, `now` the
                 Russian) or "error" (wrong case/aspect/agreement/word choice) or
                 "awkward" (understandable but not how a native would put it).
                 `span` = the short piece it's about. `why` = one plain line.
   Skip filler-only chunks.

2. general — 2-4 sentences: what went well, the 1-2 patterns worth working on.
   Warm, specific, not a lecture.

3. cards — 5 to 12 flashcards to make, most useful first. Prefer:
     - the ENGLISH GAPS (they clearly wanted to say X and couldn't) → a production
       card: front = the English idea, back = the natural Russian.
     - repeated or high-value ERRORS → a production card: front = the English /
       what they meant, back = the corrected Russian.
   Each card: {front, back, note (1 line why / when), kind ("gap"|"fix"),
   leverage ("high"|"med"), target (the key word(s) in `back`, as a JSON list)}.
   Don't make cards for one-off tiny slips or things clearly below their level.

LEVEL: %(guide)s

Output ONE raw JSON object, nothing else:
{"segments":[{"ru":"...","en":"...","fix":"...","issues":[{"span":"...","was":"...","now":"...","tier":"gap","why":"..."}]}],"general":"...","cards":[{"front":"...","back":"...","note":"...","kind":"gap","leverage":"high","target":["..."]}]}"""


def journal_analysis(transcript, level="b1", model=None):
    """transcript: the raw joined Whisper text. -> dict per _JOURNAL_SYSTEM."""
    guide = _SPEAK_LEVEL_GUIDE.get((level or "b1").lower(), _SPEAK_LEVEL_GUIDE["b1"])
    system = _JOURNAL_SYSTEM % {"guide": guide}
    base = "TRANSCRIPT:\n" + (transcript or "").strip()
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(3):
        p = base if i == 0 else base + (
            "\n\nReturn ONLY one valid JSON object — no prose, no code fence, "
            "a comma between every array element and object field.")
        try:
            return _parse_obj(run_claude(p, system, model=m, timeout=220)[0])
        except LLMError as e:
            last = e
    raise last


# ------------------------------------------------------------- verbs of motion

_MOTION_SYSTEM = """You write cards for a Russian VERBS OF MOTION drill. Each card
shows the learner a SHORT everyday English scene (2-3 sentences) with exactly ONE
clause highlighted; the learner translates only that clause, choosing the right
verb of motion, its aspect / directionality, the right prefix, and the right
preposition + case for the direction or location.

You are given a list of MOTION COMBINATIONS to test — each names a verb pair, an
aspect/directionality, a prefix, a preposition slot and a tense (with short
descriptions). Make ONE card per combination that genuinely forces that exact
choice from the scene's context.

INTERNAL CONSISTENCY — the `answer`, `note`, `contrast` and `dims` must all agree.
If while drafting you realise the scene you wrote points to a different
verb/aspect/prefix than the combination asked for, REWRITE the whole card so the
scene and the answer match the combination — do NOT keep the mismatched answer and
argue with it in the note. Never write "Wait", "Actually", "Correction", "use X
instead", "on second thought", or any self-correction / thinking-out-loud in any
field. The note calmly explains the given answer; it does not second-guess it.

Each card is a JSON object:
  situation — the full English scene, 2-3 short natural sentences. One clause in
              it is the target; write the scene so the correct verb/aspect/prefix/
              preposition is unambiguous (a "just now / in progress" cue for
              unidirectional, "every day / used to" for habitual, "there and
              back" for a round trip, a clear "arrived / left / came up to / got
              all the way to" cue when a prefix IS given, the direction/place for
              the preposition, the tense from the timeframe).
  highlight — the ONE clause the learner must translate, copied verbatim from
              `situation` (subject + motion verb + direction/location only, e.g.
              "the cat walked into the room").
  given     — the subject noun and any place/object noun the clause needs, in
              NOMINATIVE SINGULAR (["кошка", "комната"]). NEVER the verb or the
              preposition — those are the point. Omit bare pronouns.
  answer    — the highlighted clause in natural Russian: the right motion verb,
              conjugated for the tense/person, correct aspect, correct prefix,
              correct preposition + case. NO stress marks. When the given prefix
              is "none", use the BARE verb (иду / ходит / едет …) + preposition —
              do NOT bolt on a directional prefix even if the English says
              "into / out of"; "мы едем в деревню", "она ходит в библиотеку" are
              right. A real prefix (въ-, при-, у- …) only when one is given.
  target    — JSON list of the graded substring(s) of `answer` — the verb form
              plus the direction phrase (["вошла в комнату"]). Copied verbatim.
  note      — 1-2 lines: name the verb, why this aspect/direction, and the
              preposition + case rule ("войти = pf, single entry; в + accusative
              for going into an enclosed space").
  contrast  — one line with a minimally-changed variant that would need a
              DIFFERENT choice ("If she goes in and out regularly: Кошка ходит в
              комнату" or "Out of the room: вышла из комнаты").
  alts      — JSON list of 2-3 near-miss choices the learner might reach for
              INSTEAD, each {"form": "...", "why": "..."}. `form` is the wrong
              rendering of THIS clause (same scene, same tense) — the other
              aspect/directionality, a different prefix, or a different
              preposition/case. `why` is one short line on why it's wrong or
              sounds off HERE ("шла = still on the way, hasn't arrived — the scene
              says she's already inside", "пришла в комнату — при- is for arriving
              somewhere from outside, too heavy for stepping between rooms",
              "в комнате = location, but this is motion → в + accusative").
              Use real forms a learner confuses, not nonsense.
  dims      — echo back {verb, aspect, prefix, prep, tense} for this card exactly
              as given, using the SHORT ID tokens (verb = the "dims.verb id"
              value like "idti"/"bezhat", not the Russian pair).

Everyday vocabulary, concrete scenes. Vary the scenes across the batch. Output
ONE raw JSON object, nothing else:
{"cards":[{"situation":"...","highlight":"...","given":["..."],"answer":"...","target":["..."],"note":"...","contrast":"...","alts":[{"form":"...","why":"..."}],"dims":{"verb":"...","aspect":"...","prefix":"...","prep":"...","tense":"..."}}]}"""


def _motion_combo_lines(combos):
    out = []
    for c in combos:
        out.append(
            f"- verb: {c.get('verb_pair', c.get('verb'))} ({c.get('verb_en', '')}) "
            f"[dims.verb id = {c.get('verb')}] | "
            f"aspect: {c.get('aspect')} — {c.get('aspect_help', '')} | "
            f"prefix: {c.get('prefix')} — {c.get('prefix_help', '')} | "
            f"prep slot: {c.get('prep')} — {c.get('prep_help', '')} | "
            f"tense: {c.get('tense')}")
    return "\n".join(out)


def motion_cards(combos, level=2, model=None):
    prompt = (f"DIFFICULTY LEVEL: {level} (1 easiest … 4 hardest)\n\n"
              f"MOTION COMBINATIONS TO TEST — one card each:\n"
              + _motion_combo_lines(combos))
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(2):
        p = prompt if i == 0 else prompt + "\n\nReturn ONLY strict JSON."
        try:
            return _parse_obj(run_claude(p, _MOTION_SYSTEM, model=m, timeout=150)[0])
        except LLMError as e:
            last = e
    raise last


def motion_focus_cards(combos, n=6, level=2, dim_hint=None, model=None):
    """Targeted burst — `combos` may be one spec repeated or a small varied set."""
    if isinstance(combos, dict):
        combos = [combos] * n
    lead = f"Make {n} cards for targeted practice"
    if dim_hint:
        lead += f" — every card must exercise {dim_hint[0]} = {dim_hint[1]}"
    prompt = (f"DIFFICULTY LEVEL: {level}\n\n{lead}, each a DIFFERENT everyday scene:\n"
              + _motion_combo_lines(combos[:n]))
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(2):
        p = prompt if i == 0 else prompt + "\n\nSTRICT JSON only."
        try:
            return _parse_obj(run_claude(p, _MOTION_SYSTEM, model=m, timeout=150)[0])
        except LLMError as e:
            last = e
    raise last


_MOTION_VERB_SYSTEM = """You produce a reference page for ONE Russian verb-of-motion
pair (unidirectional / multidirectional), for an English-speaking B1–B2 learner.
Accurate, compact, practical. NO stress marks anywhere.

Output ONE raw JSON object:
{
  "summary": "2-3 sentences: what this verb covers, and how the uni/multi split
              plays out for THIS specific verb (any quirks).",
  "uni_use":   "one line — when to reach for the unidirectional verb",
  "multi_use": "one line — when to reach for the multidirectional verb",
  "conj": {
    "present": {           "я":["<uni>","<multi>"], "ты":[...], "он/она":[...],
                            "мы":[...], "вы":[...], "они":[...] },
    "past":    {           "он":["<uni m>","<multi m>"], "она":[...], "оно":[...],
                            "они":[...] },
    "imperative": {         "ты":["<uni>","<multi>"], "вы":[...] },
    "future_note": "one line: how each forms the future (e.g. unidirectional
                    normally uses the perfective по- form пойду/поеду; the multi
                    takes буду + infinitive)."
  },
  "prefixes": [ {"prefix":"по-", "pf":"пойти", "impf":"—",
                 "note":"set off / 'went' — the everyday past"},
                {"prefix":"при-", "pf":"прийти", "impf":"приходить",
                 "note":"arrive; при- + к/в/на"},
                … the 6-9 most common prefixes for THIS verb, with the pf (and
                impf partner if it has one) built on it, and a one-line note.
                Flag any where the prefixed verb has drifted from a literal
                motion meaning (e.g. «подойти» = also 'to suit/fit'). ],
  "mistakes": [ {"wrong":"<a wrong Russian sentence learners really produce>",
                 "right":"<the fix>", "why":"<one line>"}, … 3-4 items ],
  "examples": [ {"ru":"<natural sentence>", "en":"<translation>"}, … 4-6 items,
                covering: uni in progress, multi habitual, multi round-trip
                (past), and a prefixed form or two ]
}
Every present-tense / past / imperative cell is a 2-element array: [uni form,
multi form]. Keep the Russian genuinely idiomatic and the English natural."""


def motion_verb_reference(uni, multi, en, model=None):
    prompt = (f"Verb pair: {uni} (unidirectional) / {multi} (multidirectional) — "
              f"\"{en}\". Produce the reference page.")
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(2):
        p = prompt if i == 0 else prompt + "\n\nReturn ONLY strict JSON."
        try:
            return _parse_obj(run_claude(p, _MOTION_VERB_SYSTEM, model=m, timeout=200)[0])
        except LLMError as e:
            last = e
    raise last


_MOTION_PREFIX_SYSTEM = """You produce a reference page for ONE Russian verb-of-motion
PREFIX, for an English-speaking B1–B2 learner. Accurate, compact, practical. NO
stress marks anywhere.

Output ONE raw JSON object:
{
  "what_it_does": "2-3 sentences: the core spatial/aspectual meaning this prefix
                   adds to a motion verb, and what happens to aspect (a prefixed
                   motion verb is perfective; its imperfective partner is built on
                   the MULTIdirectional stem — приходить, входить).",
  "usage": "1-2 lines: which prepositions + cases it pairs with (в/на + acc, к +
            dat, из/с/от + gen …), and the typical shape of a sentence with it.",
  "exceptions": [ {"verb":"подойти", "meaning":"to suit / to be a good fit",
                   "example":"Это платье тебе очень идёт → Эта работа мне не подходит"},
                  … any prefixed motion verbs built with THIS prefix that carry a
                  non-literal / idiomatic meaning, or behave unusually. 2-4 items;
                  [] if there are none worth flagging. ],
  "mistakes": [ {"wrong":"<real learner error with this prefix>",
                 "right":"<fix>", "why":"<one line>"}, … 3-4 items —
                 e.g. wrong preposition after the prefix, using the prefixed
                 perfective for a repeated action, confusing this prefix with a
                 near neighbour (у- vs вы-, при- vs под-). ],
  "examples": [ {"ru":"<natural sentence using this prefix on a motion verb>",
                 "en":"<translation>"}, … 4-6 items, varying the base verb
                 (идти, ехать, нести …) and the tense ]
}"""


def motion_prefix_reference(prefix, meaning, model=None):
    prompt = (f"Prefix: {prefix} — \"{meaning}\". Produce the reference page for how "
              f"it works on Russian verbs of motion.")
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(2):
        p = prompt if i == 0 else prompt + "\n\nReturn ONLY strict JSON."
        try:
            return _parse_obj(run_claude(p, _MOTION_PREFIX_SYSTEM, model=m, timeout=200)[0])
        except LLMError as e:
            last = e
    raise last


# ------------------------------------------------------------- chunk deck

_CHUNK_SYSTEM = """You write cards for a Russian CHUNK DECK — a drill that burns in
formulaic conversational phrases ("prefabs") by putting them in fresh contexts.

You are given a list of CHUNKS. Each names a Russian prefab, its English gloss,
its conversational function, its register, and a usage note. For EACH chunk, write
ONE card: a short, natural spoken English utterance (1–2 sentences — the kind of
thing a person actually says out loud, not written prose) that uses that chunk,
and its natural Russian.

CRITICAL — TWO SEPARATE LANGUAGES, NOT CODE-SWITCHING. `en` is 100% English with
ZERO Russian in it (no Cyrillic, no transliterated Russian). `ru` is 100% Russian
with ZERO English in it. They are parallel translations of the SAME utterance.
The chunk shows up in `en` in its ordinary English wording and in `ru` in its
Russian wording — never leave the Russian chunk sitting inside the English
sentence. If you catch yourself writing "..., but на самом деле I just..." STOP —
that whole sentence must be English: "..., but actually I just...".

VOICE — hold this for BOTH languages. The speaker is a warm, well-spoken MAN in
his mid-20s who wants to sound like himself with family and friends of any age —
a cousin his age, a parent, a grandparent, a friend of a friend. Every context
is SOCIAL and casual, never professional. Relaxed and natural, never stiff. But
NOT crude, NOT street/criminal slang, NOT heavy youth slang or filler (avoid
«забить / забей», «капец», «жесть», «прикол», «ну такое», «топ», «кринж»). And
equally NOT bookish or officialese (avoid «тем не менее», «следует», «в связи с
этим», «полагаю», «осуществлять», presentation-style enumeration). Contractions
and everyday particles yes; teenage-speak and boardroom-speak no.
- The speaker is MALE: whenever the utterance is first-person, use masculine
  past-tense and agreement forms (я пошёл, я был рад, я сам, согласен, я не знал).
  If the natural scene needs a female "I", rewrite the scene so the MAN is the
  one speaking.
- When the scene is addressed to a grandparent or anyone he'd «вы», use «вы» forms.

Each card is a JSON object:
  chunk_id  — echo the id you were given, exactly.
  en        — the FULL utterance in plain English (no Cyrillic anywhere). Spoken,
              realistic, in the voice above. Enough context that the chunk is
              clearly motivated. 1–2 sentences.
  ru        — the same utterance in natural Russian (no English anywhere). NO
              stress marks. The chunk appears in it, unchanged or minimally
              inflected.
  chunk_en  — the exact slice of `en` (an English phrase) that renders the chunk —
              copied VERBATIM as a substring of `en`, same words, same case.
  chunk_ru  — the exact slice of `ru` (a Russian phrase) that IS the chunk —
              copied VERBATIM as a substring of `ru` (this exact string is blanked
              out in one drill mode, so it must match character-for-character).
  gist      — 3–7 bare English words naming the situation, no full sentence
              ("reacting to surprising news", "starting to explain why you're
              late", "softening a disagreement about money"). English only. Used
              in a mode where the learner gets only this and must produce the
              Russian.
  gloss     — a word-for-word gloss of the chunk (не → "not", всё равно → "all
              the same"), to help it recombine. Keep the given one if it's good.
  note      — 1 line: when to reach for this chunk / what nuance it carries.
              Keep or tighten the given note.

Vary the situations across the batch — different speakers, moods, topics. Keep
the Russian genuinely idiomatic. Output ONE raw JSON object, nothing else:
{"cards":[{"chunk_id":"...","en":"...","ru":"...","chunk_en":"...","chunk_ru":"...","gist":"...","gloss":"...","note":"..."}]}"""


def _chunk_spec_lines(specs):
    out = []
    for s in specs:
        out.append(
            f"- id: {s.get('id')} | function: {s.get('fn_label', s.get('fn'))} | "
            f"chunk: {s.get('ru')} ({s.get('en')}) | register: {s.get('reg', 'casual')} | "
            f"gloss: {s.get('lit', '')} | note: {s.get('note', '')}")
    return "\n".join(out)


def chunk_cards(specs, model=None):
    prompt = ("CHUNKS TO TURN INTO CARDS — one card each, in order:\n"
              + _chunk_spec_lines(specs))
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(2):
        p = prompt if i == 0 else prompt + "\n\nReturn ONLY strict JSON."
        try:
            return _parse_obj(run_claude(p, _CHUNK_SYSTEM, model=m, timeout=150)[0])
        except LLMError as e:
            last = e
    raise last


def chunk_focus_cards(specs, n=6, fn_hint=None, model=None):
    """Targeted burst — `specs` may be one spec repeated or a small varied set."""
    if isinstance(specs, dict):
        specs = [specs] * n
    lead = f"Make {n} cards"
    if fn_hint:
        lead += f" — all for the '{fn_hint}' function"
    prompt = (f"{lead}, each a DIFFERENT casual situation:\n"
              + _chunk_spec_lines(specs[:n]))
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(2):
        p = prompt if i == 0 else prompt + "\n\nSTRICT JSON only."
        try:
            return _parse_obj(run_claude(p, _CHUNK_SYSTEM, model=m, timeout=150)[0])
        except LLMError as e:
            last = e
    raise last


# ------------------------------------------------------------- speech lab

_SPEECH_VOICE = """WHOSE VOICE — the speaker is a warm, well-spoken MAN in his
mid-20s: a software engineer in New York, learning Russian mainly to talk with his
girlfriend's Russian-speaking family. He's explaining something to a friend or an
interested uncle, casually, at a family gathering. So:
- Spoken, relaxed, first person. Contractions and everyday particles (ну, вот,
  как бы, в общем, то есть, короче, на самом деле) where a native would use them.
- NOT formal or textbook, NOT addressed to strangers, NOT lecture-y.
- NOT crude, NOT heavy slang, NOT teenage-speak. The kind of Russian you'd be
  glad to have your girlfriend's mum hear.
- Male forms throughout (я начал, я понял, я сам, я был).
- Real spoken rhythm: some short sentences, some run-on with «и», a rhetorical
  question or two, a little self-deprecation. It should sound like a person
  talking, not an essay read aloud."""

_SPEECH_SYSTEM = f"""You write a short SPEECH for a Russian learner to memorise and
say out loud over and over — a paragraph or two (roughly 90-180 words) of natural,
conversational Russian on the topic given.

{_SPEECH_VOICE}

The point is that the learner internalises real chunks and constructions by
repetition, so favour the phrasing a native actually reaches for over anything
clever or literary. Keep it concrete and personal.

Output ONE raw JSON object. `ru` and `en` are ARRAYS — one string per paragraph
(usually 2), so there are no line breaks inside any JSON string:
{{
  "title": "3-6 word English title for the collection list",
  "ru": ["first paragraph in Russian", "second paragraph"],
  "en": ["natural English of paragraph 1", "…of paragraph 2"],
  "notes": [ {{"ru": "<a chunk or construction worth noticing, e.g. «взялся за»,
              «то есть», «в свободное время»>", "why": "<one line — what it does /
              when you'd use it>"}}, … 4-8 of the highest-value ones ]
}}
Russian: spoken register, NO stress marks."""


def speech_draft(topic, extra="", model=None):
    prompt = f"TOPIC: {topic.strip()}"
    if extra and extra.strip():
        prompt += f"\n\nDETAILS TO WORK IN (loosely — don't just list them):\n{extra.strip()}"
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(2):
        p = prompt if i == 0 else prompt + "\n\nReturn ONLY strict JSON."
        try:
            return _parse_obj(run_claude(p, _SPEECH_SYSTEM, model=m, timeout=220)[0])
        except LLMError as e:
            last = e
    raise last


_SPEECH_ANNOTATE_SYSTEM = f"""You are given a Russian SPEECH the learner pasted in
(their own or from elsewhere). Do NOT rewrite it. Produce ONE raw JSON object;
`en` is an ARRAY, one string per paragraph, so no line breaks inside any string:
{{
  "title": "3-6 word English title",
  "en": ["natural English of paragraph 1", "…paragraph 2", …],
  "notes": [ {{"ru": "<chunk / construction worth noticing>", "why": "<one line>"}},
             … 4-8 of the highest-value ones from THIS text ],
  "register_flags": [ "<optional: any phrase that sounds off for a mid-20s man
     talking casually with family — too formal, too slang, or unnatural — with a
     suggested fix; [] if it all sounds right>" ]
}}
For the voice check use this yardstick: {_SPEECH_VOICE}"""


def speech_annotate(ru, model=None):
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(2):
        p = ru.strip() if i == 0 else ru.strip() + "\n\nReturn ONLY strict JSON."
        try:
            return _parse_obj(run_claude(p, _SPEECH_ANNOTATE_SYSTEM, model=m, timeout=180)[0])
        except LLMError as e:
            last = e
    raise last


# ============================================================ flow reading

_READING_LEVELS = [
    (1400,  "a2",     "Simple, high-frequency words only. Short sentences. Present tense mostly."),
    (2800,  "b1",     "Everyday B1 vocabulary. Some subordinate clauses; past and future fine."),
    (4500,  "b1+/b2", "Natural B1+/B2 prose. Ordinary complex sentences; idioms sparingly."),
    (7000,  "b2",     "Solid B2. Richer vocabulary, some abstract/figurative language."),
    (12000, "b2+/c1", "B2+/C1. Fuller vocabulary and register; a literary or journalistic feel is fine."),
    (99999, "c1+",    "Unrestricted, like something a Russian would actually read."),
]


def _reading_level_line(rank_est):
    for cap, cefr, guide in _READING_LEVELS:
        if rank_est <= cap:
            return cefr, guide
    return _READING_LEVELS[-1][1], _READING_LEVELS[-1][2]


_READING_PLAN_SYSTEM = """You plan a SHORT, self-contained Russian reading piece
for a language learner — {parts} parts of ~130 words each, read one part at a
time. Your job is the SHAPE: give the whole thing a real arc so that something
actually happens and the reader wants to keep going.

FORM — the "FORM" note says what kind of piece this is (a story, a news article,
a popular-science explainer…). Plan the arc that fits it:
- a STORY: a hook / a situation with tension → something changes → a complication
  or turn → the climax → a real resolution. Someone wants something; something
  gets in the way; by the end it's settled (well or badly).
- an ARTICLE / ESSAY: a sharp question or surprising fact → the key background →
  the core tension or disagreement → the author's reading of it → a conclusion
  that lands. Not a list — an argument with a spine.

MUST be intriguing from the first line — a concrete hook, real stakes, a reason
to read on. No vague throat-clearing.

Part {parts} is the ENDING. It resolves things. No cliffhanger, no "to be
continued".

Output ONE raw JSON object, nothing else:
{{"title": "a short Russian title (<= 6 words)",
  "hook": "one sentence, in English, on why this is worth reading",
  "beats": ["what part 1 does", "part 2", … exactly {parts} entries]}}
Beats are your notes to yourself (English is fine) — concrete: what happens / what
gets covered in that part, and how it ends to pull the reader into the next."""


def reading_flow_plan(topic, prompt="", style="", grounding="", cefr="b1",
                      parts=5, sequel_of=None, model=None):
    """-> {"title": str, "hook": str, "beats": [str] * parts}. Raises LLMError."""
    body = [f"TOPIC: {(topic or prompt or 'anything interesting').strip()}"]
    if prompt and prompt.strip() and prompt.strip() != (topic or "").strip():
        body.append(f"WHAT THE READER ASKED FOR: {prompt.strip()}")
    if style and style.strip():
        body.append("FORM: " + style.strip())
    if grounding and grounding.strip():
        body.append("GROUNDING: " + grounding.strip())
    if sequel_of:
        body.append("THIS IS A SEQUEL. The previous piece was «%s» — %s\nWrite a "
                    "genuine next chapter / follow-up: same world and (if a story) "
                    "characters, but move time forward and open on a real TWIST — "
                    "a reversal, a consequence come due, a new threat, a secret "
                    "surfacing. It must stand on its own too."
                    % (sequel_of.get("title", "?"), sequel_of.get("summary", "")))
    sysm = _READING_PLAN_SYSTEM.format(parts=parts)
    last = None
    for i in range(2):
        p = "\n\n".join(body) + ("" if i == 0 else "\n\nReturn ONLY strict JSON.")
        try:
            d = _parse_obj(run_claude(p, sysm, model=model or TRANSLATE_MODEL, timeout=90)[0])
            beats = [str(b).strip() for b in (d.get("beats") or []) if str(b).strip()]
            if beats:
                return {"title": (d.get("title") or topic or "").strip()[:80],
                        "hook": (d.get("hook") or "").strip()[:200],
                        "beats": beats}
        except LLMError as e:
            last = e
    raise last or LLMError("no plan")


_READING_FLOW_SYSTEM = """You write ONE part of a short, planned Russian reading
piece for a language learner. You are given the whole PLAN and told which PART to
write now.

PART — write exactly the part asked for. Hit its beat. Do NOT rush ahead into
later beats or drag in earlier ones.
- If this is NOT the last part: end it at a real pull-forward moment — a question
  opened, a decision looming, a fact that demands the next step. Not a recap.
- If this IS the last part ("FINAL"): bring the whole piece to a proper close —
  the story resolves, or the article's argument lands. No cliffhanger, no "to be
  continued", no teaser for a sequel.

FORM — the "FORM" note governs genre, structure, register and voice. Follow it.
Do NOT default to "a third-person story about two people talking" unless FORM asks.

VOCABULARY LEVEL — write for a reader who comfortably knows about the {rank}
most common Russian words ({cefr}). {guide}
- Keep words the reader would likely NOT know to about 1 in 50 (~2%). A light
  sprinkle is how they learn; a wall of them is not. (FORM may ask for specific
  vocabulary — honour it, but keep density in this range.)
- Natural, idiomatic Russian in the register FORM calls for.
- NEVER use stress marks (added afterwards). NEVER write any English.
- Always write ё with its dots (её, всё, ещё, идёт) — never as е.

CONTINUITY — "SO FAR" is what earlier parts established. Continue seamlessly; do
NOT recap. If SO FAR is empty this is part 1 — open on the hook, fast.

GROUNDING — if a "GROUNDING" note is given: real countries, organisations, named
people, dates, findings, real positions. Stay within what you know; stay general
rather than invent a specific; never fabricate a quote, statistic, law or person.

SEED WORDS — words the reader is mid-learning. Try to slip a natural form of one
or two in WHERE IT GENUINELY FITS. Better none than a bent sentence. Never force,
list, or flag them.

TARGET VOCAB — if listed, these are words that recur in the classic novel the
reader is preparing for. Same rule as seed words: use a natural form of one or
two only where the scene genuinely calls for it, never forced.

LENGTH — 2 to 3 short paragraphs, about 120-150 words.

Output ONE raw JSON object:
{{"text": ["paragraph one", "paragraph two", ...],
  "summary": "everything a reader needs to follow the next part: who/what/where + where things stand, <= 55 words"}}"""


def reading_flow_chunk(topic, prompt, summary, rank_est, seed_words=(),
                       grounding="", style="", plan=None, part=1, total=5,
                       target_words=(), model=None):
    """-> {"text": [paragraphs], "summary": str}. Raises LLMError on failure."""
    cefr, guide = _reading_level_line(rank_est)
    sys = _READING_FLOW_SYSTEM.format(rank=rank_est, cefr=cefr, guide=guide)
    parts = [f"TOPIC: {(topic or prompt or 'anything interesting').strip()}"]
    if prompt and prompt.strip() and prompt.strip() != (topic or "").strip():
        parts.append(f"WHAT THE READER ASKED FOR: {prompt.strip()}")
    if style and style.strip():
        parts.append("FORM: " + style.strip())
    if grounding and grounding.strip():
        parts.append("GROUNDING: " + grounding.strip())
    if plan and plan.get("beats"):
        beats = plan["beats"]
        lines = "\n".join(f"  {i + 1}. {b}" for i, b in enumerate(beats))
        parts.append(f"PLAN — «{plan.get('title', '')}»\n{lines}")
        bi = min(max(1, part), len(beats)) - 1
        tag = "FINAL PART" if part >= total else f"PART {part} of {total}"
        parts.append(f"WRITE NOW — {tag}. Its beat: {beats[bi]}")
    else:
        tag = "the FINAL part — resolve it" if part >= total else f"part {part} of {total}"
        parts.append(f"WRITE NOW — this is {tag}.")
    parts.append("SO FAR: " + ((summary or "").strip() or "(nothing yet — begin)"))
    if seed_words:
        parts.append("SEED WORDS (optional, work in 1-2 naturally): "
                     + ", ".join(seed_words))
    if target_words:
        parts.append("TARGET VOCAB (from the classic novel; optional, 1-2 only if the scene fits): "
                     + ", ".join(target_words))
    prompt_text = "\n\n".join(parts)
    m = model or TRANSLATE_MODEL
    last = None
    for i in range(2):
        p = prompt_text if i == 0 else prompt_text + "\n\nReturn ONLY strict JSON."
        try:
            return _parse_obj(run_claude(p, sys, model=m, timeout=150)[0])
        except LLMError as e:
            last = e
    raise last


# ============================================================ conversation partner

_CONVO_OPEN_SYSTEM = """You set up a spoken Russian conversation for a learner to
practise in. Given a scenario, invent a concrete, believable Russian-speaking
person and situation, then say their FIRST line.

The learner is a mid-20s American man ({level}) learning Russian mainly to talk
with his girlfriend's Russian-speaking family. Casual register unless the
character wouldn't use it.

Output ONE raw JSON object:
{{"persona": "who they are, in 1-2 sentences (name, relation/role, manner)",
  "situation": "where this is happening, 1 sentence",
  "goal": "what the learner is trying to do here, 1 short phrase",
  "opening_ru": "the character's first spoken line — natural, 1-2 sentences, no stress marks",
  "opening_en": "plain English of that line"}}"""


def convo_open(scenario, prompt="", level="b1", model=None):
    body = f"SCENARIO: {(scenario or prompt or 'casual small talk with a relative').strip()}"
    if prompt and prompt.strip() and prompt.strip() != (scenario or "").strip():
        body += f"\nEXTRA FROM THE LEARNER: {prompt.strip()}"
    sys = _CONVO_OPEN_SYSTEM.format(level=level)
    return _parse_obj(run_claude(body, sys, model=model or TRANSLATE_MODEL, timeout=90)[0])


_CONVO_REPLY_SYSTEM = """You are role-playing a real person in a SPOKEN Russian
conversation with a learner (level {level}). Stay fully in character:
{persona}
Setting: {situation}

THE LEARNER speaks Russian but drops into ENGLISH the moment they don't know a
word or phrase, and their turn is a rough speech-to-text transcript that may
have errors. UNDERSTAND THEIR FULL INTENT anyway — never act confused just
because of English or transcription noise; only ask them to clarify if a real
person genuinely would.

YOUR SPOKEN REPLY (`reply_ru`):
- in character, natural, ONE to THREE sentences — this is talking, not a lecture
- vocabulary and pace a {level} learner can follow; simplify if they're
  struggling, but never baby-talk
- keep it MOVING: react, then ask something back or add something
- NO stress marks

ALSO REPORT (never spoken — for the learner's review afterwards):
- `gaps`: each thing they said in English -> how to say it in Russian at their
  level. [] if none.
- `errors`: genuine Russian mistakes in their turn (case, aspect, agreement,
  wrong word) -> the fix + a <=6-word why. IGNORE transcription / punctuation
  noise. [] if none.
- `drifted`: true only if their turn was so off it broke the conversation.

Output ONE raw JSON object:
{{"reply_ru": "...", "reply_en": "plain English of your reply",
  "gaps": [{{"en": "...", "ru": "..."}}],
  "errors": [{{"wrong": "...", "right": "...", "why": "..."}}],
  "drifted": false}}"""


def convo_reply(persona, situation, history, learner_text, level="b1", model=None):
    sys = _CONVO_REPLY_SYSTEM.format(level=level, persona=persona or "a friendly relative",
                                    situation=situation or "a family gathering")
    lines = []
    for t in history[-12:]:
        who = "YOU" if t.get("role") == "partner" else "LEARNER"
        lines.append(f"{who}: {(t.get('text') or '').strip()}")
    lines.append(f"LEARNER (just now): {(learner_text or '').strip()}")
    return _parse_obj(run_claude("\n".join(lines), sys,
                                 model=model or TRANSLATE_MODEL, timeout=120)[0])


_CONVO_DEBRIEF_SYSTEM = """You review a whole practice conversation a Russian
learner ({level}) just had with an in-character partner. Be encouraging and
concrete.

Output ONE raw JSON object:
{{"summary": "2-3 sentences: how it went, in plain English",
  "wins": ["something they did well", ...],           // 1-3
  "focus": ["the single most useful thing to work on next, then maybe one more"], // 1-2
  "cards": [{{"ru": "a short useful Russian sentence they should be able to say",
              "en": "its English"}}, ...],             // 3-6, drawn from the gaps
  "chunks": ["a natural conversational phrase worth memorising from this", ...]   // 2-4
}}"""


def convo_debrief(persona, situation, history, level="b1", model=None):
    lines = [f"PARTNER: {persona}", f"SETTING: {situation}", ""]
    for t in history:
        who = "PARTNER" if t.get("role") == "partner" else "LEARNER"
        lines.append(f"{who}: {(t.get('text') or '').strip()}")
        meta = t.get("meta") or {}
        for e in (meta.get("errors") or []):
            lines.append(f"   [error: {e.get('wrong')} -> {e.get('right')}]")
        for g in (meta.get("gaps") or []):
            lines.append(f"   [said in English: {g.get('en')} = {g.get('ru')}]")
    sys = _CONVO_DEBRIEF_SYSTEM.format(level=level)
    return _parse_obj(run_claude("\n".join(lines), sys,
                                 model=model or TRANSLATE_MODEL, timeout=120)[0])
