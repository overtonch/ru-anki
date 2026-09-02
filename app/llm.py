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


def _parse_obj(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError(f"no JSON object in model output: {text[:300]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError(f"bad JSON object from model ({e}): {m.group(0)[:300]}")


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


def word_family(word, model=None):
    """-> (root, [member lemmas]) for a Russian word. One headless call."""
    text = _warm_or_oneshot(f"Word: {word}", _FAMILY_SYSTEM,
                            model or TRANSLATE_MODEL, timeout=40)
    obj = _parse_obj(text)
    fold = lambda s: (s or "").strip().lower().replace("ё", "е")
    members = [fold(m) for m in (obj.get("members") or []) if isinstance(m, str) and m.strip()]
    members = [m for m in members if all("а" <= ch <= "я" or ch == "-" for ch in m)]
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
