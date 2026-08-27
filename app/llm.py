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
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

CLAUDE = "claude"
DEFAULT_MODEL = os.environ.get("RU_EXTRACT_MODEL", "haiku")
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

EXTRACT_SYSTEM = """You extract advanced Russian vocabulary for ONE upper-intermediate / advanced (B2-C1) learner from an excerpt of a Russian YouTube transcript. The transcript is de-overlapped ASR, given as lines each prefixed with a [HH:MM:SS] tag.

Output ONLY pipe-delimited lines, one item per line, and NOTHING else:
SPAN|TRANSLATION|HH:MM:SS

- SPAN: almost always a SINGLE word in citation form. Use a multi-word span ONLY for a true set phrase / idiom / fixed expression whose meaning is not just the sum of its parts, or a phrasal verb / light-verb construction that must be learned as a unit — e.g. "как раз", "по большому счёту", "иметь в виду", "сойти с ума", "тем не менее", "вам шашечки или ехать". Do NOT bundle an ordinary adjective+noun, noun+noun, or verb+object just because they occur together (NOT "критические последствия", NOT "научный журналист", NOT "высокая температура"). If one word in such a pair is worth learning, flag that word alone.
- TRANSLATION: a concise English gloss; "a / b" if genuinely ambiguous; gloss idioms by meaning.
- HH:MM:SS: copy the [timestamp] tag of the line where the span occurs.
- Be SELECTIVE. Flag genuinely advanced, bookish, technical, colloquial, or idiomatic items. Skip anything an intermediate learner already knows. Roughly one item per 20-30 words of transcript; fewer if the passage is simple. Most items should be single words — genuine phrases are the exception, not the rule.
- SKIP transparent borrowings from English (or other Western languages) that an English speaker would recognise on sight: e.g. стрим, контент, дедлайн, анонс, спонсор, менеджер, эмулировать, логистика, тренд, хайп, фидбэк. Only keep a borrowing if its Russian meaning or usage is genuinely non-obvious.
- If a token looks like an ASR mistake rather than real vocabulary, still include it and start its translation with "(SUSPECT ASR) ".
- No header, no numbering, no commentary, no code fence. If nothing qualifies, output nothing."""

CHUNK_LINES = 80  # grouped transcript lines per headless call
_TS = re.compile(r"(\d\d):(\d\d):(\d\d)")


def _chunks(transcript, n=CHUNK_LINES):
    lines = [ln for ln in transcript.splitlines() if ln.strip()]
    return ["\n".join(lines[i:i + n]) for i in range(0, len(lines), n)]


def parse_items(text):
    """Parse the model's pipe-delimited output. Ignores any stray prose lines."""
    out = []
    for ln in text.splitlines():
        ln = ln.strip().strip("`").strip()
        if ln.count("|") < 2:
            continue
        parts = [p.strip() for p in ln.split("|")]
        span, tr = parts[0], parts[1]
        m = _TS.search(parts[2]) if len(parts) > 2 else None
        if not span or not tr or span.lower() in ("span", "span_text"):
            continue
        out.append({
            "span_text": span,
            "is_phrase": 1 if " " in span else 0,
            "translation": tr,
            "timestamp_start": f"{m.group(1)}:{m.group(2)}:{m.group(3)}" if m else None,
        })
    return out


def _extract_chunk(part, decided, model):
    prompt = part
    if decided:
        prompt += f"\n\n(Already covered — do not output these: {decided})"
    text, usage = run_claude(prompt, EXTRACT_SYSTEM, model=model, timeout=180)
    return parse_items(text), usage


def extract_candidates(title, transcript, already_decided, model=DEFAULT_MODEL,
                       progress=None, on_chunk=None, workers=WORKERS):
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
    parts = _chunks(transcript)
    total = len(parts)
    merged, seen, errors, done = [], set(), [], 0
    usage = {"in": 0, "out": 0, "think": 0, "cost_est": 0.0, "calls": 0}
    if progress:
        progress(0, total, errors)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futs = {pool.submit(_extract_chunk, part, decided, model): i
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

TRANSLATE_SYSTEM = """You gloss ONE Russian word or phrase as used in one specific ASR line, for a B2/C1 learner building a flashcard. The line may be truncated or contain glitches.

Output ONLY one raw JSON object, no fence:
{"span_text": "...", "is_phrase": true/false, "translation": "...", "sentence": "..."}
- span_text: clean citation form of what you glossed.
- translation: best contextual English gloss; "a / b" if ambiguous; idioms by meaning.
- sentence: the line lightly cleaned into a short readable Russian sentence containing an inflected form of span_text; if too fragmentary, write a minimal natural one."""


def translate_span(sentence, span, model=DEFAULT_MODEL):
    """Small single-item call for live lookup. -> dict(span_text, is_phrase, translation, sentence)."""
    prompt = f"Line: {sentence}\nThe learner tapped on / typed: {span}"
    text, _ = run_claude(prompt, TRANSLATE_SYSTEM, model=model, timeout=60)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise LLMError(f"no JSON object in model output: {text[:300]}")
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise LLMError(f"bad JSON object from model ({e}): {m.group(0)[:300]}")
