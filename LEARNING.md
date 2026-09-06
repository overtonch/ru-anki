# How this app teaches — the learning-science knowledge base

The point of this file: when the learner asks "what should I do to get better at X",
or when we're designing a new mode, we don't re-derive second-language-acquisition
(SLA) research from scratch. This is the digest, plus how each idea maps onto the
app and where the gaps are.

The app is deliberately a **testbed for learning methods** — several modes that
attack the same language from different angles, so the learner can run their own
experiments on what works for them. Every mode should have a defensible reason to
exist in terms of the principles below.

---

## The learner (as of 2026-09)

- **Reading / listening comprehension: B2+ / C1−.** Large passive vocabulary,
  reads novels-adjacent material comfortably.
- **Speaking: A2 / B1 in ability, A1 in confidence.** Stutters, slow lexical
  retrieval, unsure of verb government (which case/preposition a verb takes).
  Knows the words; can't pull them out fast enough or wire them together.
- Native English speaker, mid-20s, in the US, learning to talk with his
  girlfriend's Russian-speaking family. Register target: casual, male forms,
  not formal, not slang.
- Has lots of **5-minute pockets** (commuting, in public) where silent
  phone-only practice is possible; less time for sit-down speaking aloud.

The central problem is the **passive → active gap**: comprehension has raced
ahead of production. That's normal (input is always ahead of output) but the
spread here is unusually wide, and it's the thing to close.

---

## The core principles

### 1. Nation's Four Strands
A balanced course spends roughly equal time on four things (Nation 2007):

| Strand | What it is | App features | Coverage |
|---|---|---|---|
| **Meaning-focused input** | reading & listening for the message | flow reading, watch/videos, songs, texts | strong |
| **Language-focused learning** | deliberate study of words/grammar/sound | SRS, grammar map, motion verbs, chunks, accent | strong |
| **Meaning-focused output** | producing language to communicate | Speak (reformulation), conversation partner | thin |
| **Fluency development** | getting faster at what you already know | — (nothing dedicated) | absent |

The learner is ~3/4. **Output and fluency are the gap** and where new modes
should go.

### 2. Comprehensible input (Krashen; refined by Nation, Hu & Nation)
Acquisition is driven by understanding messages slightly above current level.
- For **reading**, the coverage sweet spot is **~98% of running words known**
  (Hu & Nation 2000; Laufer & Ravenhorst-Kalovski 2010). Below ~95% comprehension
  and the ability to guess unknown words from context both collapse. Flow reading
  targets 2% unknown for this reason (see `app/reading_flow.py` `TARGET_UNKNOWN`).
- Input alone grows vocabulary **slowly** — a single meaningful encounter gives
  ~5–15% chance of durable learning (Nagy et al. 1985; Waring & Takaki 2003);
  reliable form+meaning learning needs **~10+ encounters** (Pellicer-Sánchez &
  Schmitt 2010). Hence: seed known/shaky cards into generated text, and pair
  input with the SRS.

### 3. Output Hypothesis (Swain)
Producing language forces you to process it more deeply than comprehension does,
and — crucially — makes you **notice the gaps** ("I don't know what preposition
goes with this verb"). Noticing a gap is what triggers learning it. The learner
is already doing this noticing; the app should give him structured chances to.

### 4. Retrieval practice / the testing effect (Roediger & Karpicke; Barcroft)
Pulling a word **out of memory** strengthens it far more than seeing it again.
The harder the successful retrieval (within reason), the bigger the gain.
- The SRS is 100% **recognition** retrieval (see word → recall meaning).
- It has **no productive twin** (see meaning/situation → produce the word). That
  is the single biggest missing piece for this learner.

### 5. Spacing & interleaving
Reviews spread over expanding intervals beat massed practice; mixing item types
beats blocking. FSRS handles this for the SRS. Any new productive mode should
also be spaced (cycle items back by retrieval success), not a one-pass list walk.

### 6. Chunking / the lexical approach (Sinclair; Wray; Boers)
Fluent speech is mostly **prefabricated multi-word units**, not word-by-word
assembly. "думать **о** чём-то", "зависеть **от** чего-то", "обраща́ться **к**
кому-то". Learn verbs **with their government** (case + preposition) as units.
Russian has ~100–150 verbs whose government isn't guessable from English — a
finite, learnable set that causes most government errors.

### 7. Automatization (DeKeyser; ACT-R)
Knowing a rule (declarative) becomes fluent use (procedural) only through
**repeated practice under production conditions**. Early practice can be
somewhat mechanical *if* it's meaningful and varied; it must then move to real
communication. This is the theory behind transformation/substitution drills —
rehabilitated from audiolingualism by keeping them meaningful, personalized, and
brief.

### 8. Fluency development (Nation)
Separate from learning new things: taking known material and doing it **faster**
under mild time pressure, repeatedly. Techniques: 4/3/2 (say the same thing in
4 then 3 then 2 minutes), repeated retellings, timed drills. Nothing in the app
does this yet.

### 9. Speech production has stages (Levelt)
*conceptualize → formulate → articulate.* This learner's bottleneck is the
**formulator**: lexical access speed + grammatical encoding. Articulation (the
motor-phonetic stage) is *not* his problem — which is why **silent practice is
legitimate for him**: inner speech engages retrieval and grammatical encoding;
it only skips the part he doesn't need. He loses interlocutor pressure and
pronunciation feedback, keeps ~80% of the benefit, and can do it anywhere.
(Guerrero's work on L2 inner speech; general covert-practice literature.)

### 10. Narrow reading / recycling (Schmitt & Carter; Kang; Gardner)
Staying within a topic, genre, or author makes vocabulary recur far more often
than general reading, so words reach the ~10-encounter threshold faster. This is
why flow reading has domains and lets you return to and continue old pieces, and
why the fiction domain targets the actual vocabulary of a target book
(`proficiency.book_readiness`, `fiction_target_words`).

### 11. Error correction / feedback
Output without feedback drifts. Corrective feedback works best when it's
**focused** (1–2 highest-value fixes, not a wall), **timely**, and lets the
learner **notice and re-produce** the correct form. The Speak mode's grading and
the conversation debrief follow this; keep new modes to the same bar.

### 12. Affect / the confidence gap
This learner's *confidence* lags his *ability* — he stutters and freezes even on
things he knows. Low-stakes, silent, self-paced production (no listener, no
clock at first) lets ability show through; success there rebuilds the confidence
that then transfers to real speaking. Don't front-load hard open-ended tasks.

---

## What each existing mode is for

| Mode | Strand(s) | Mechanism |
|---|---|---|
| **SRS review** | language-focused learning | spaced recognition retrieval, form↔meaning |
| **Flow reading** | meaning-focused input; narrow reading | level-tuned comprehensible input, seeded recycling, book-targeted vocab |
| **Watch / videos / songs** | meaning-focused input (+ listening) | authentic connected speech, tap-to-card |
| **Conversation partner** | meaning-focused output; fluency | real-time production, code-switch tolerated, focused debrief |
| **Speak (reformulation)** | meaning-focused output | English thought → Russian, native reformulations, gaps → production cards |
| **Grammar map / drill** | language-focused learning | targeted rule practice, contrastive |
| **Motion verbs / chunks** | language-focused learning | closed hard subsystems drilled explicitly |
| **Proficiency / Level tab** | metacognition | known-vocab estimate, per-domain fluency, AK readiness — makes progress visible, which sustains effort |

**Gaps:** productive vocabulary retrieval, verb-government as chunks, morphological
automatization, and dedicated fluency (speed) work. See TODO / the "speaking
activation" design.

---

## Design checklist for a new learning mode

Before building a mode, it should pass most of these:

1. **Which strand?** If it's a 4th "language-focused learning" mode, be sceptical
   — those are already strong.
2. **Retrieval, not recognition** for anything productive. The learner must
   generate from memory, then check — never pick from options as the main move.
3. **Meaningful & personalized.** Reps are about the learner's real life / a real
   situation, not abstract sentences. This is what separates it from failed
   audiolingual drill.
4. **Varied.** Same target word/pattern, several different contexts — not the
   same frame ten times.
5. **Spaced.** Items cycle back by success, not a single pass.
6. **Feedback is focused.** 1–2 highest-value corrections, with a chance to
   re-produce the fixed form.
7. **Fits a 5-minute silent pocket.** Type-or-think-then-reveal-and-self-grade.
   No audio required. Resumable mid-list.
8. **Graded.** There's an easier rung below and a harder rung above, so the
   learner can enter at the right difficulty and climb.
9. **Progress is visible.** It feeds a number on the stats page (e.g. active-word
   count) so the learner sees the passive→active conversion over time.
10. **Curriculum is finite and principled.** Work a defined list (frequency-ranked
    verbs, the top ~2.5k content words, the ~150 government verbs) — not an
    infinite generator, so "done" is reachable and progress is measurable.

---

## Curated lists to build the curriculum from

- **Verbs by frequency + government** — from the `freq` table, top ~400–500
  verbs, each tagged (LLM) with its case/preposition pattern(s) and 2–3 example
  frames. The ~150 with non-obvious government are the priority sub-list.
- **Core productive vocabulary** — the ~2,500 most frequent *content* words
  (`freq` rank, minus function words), minus what the learner can already use
  actively. This is the "everyone should be able to say these" set.
- **Sentence frames** — a bank of high-frequency slot templates
  («Мне нужно ___», «Я думаю, что ___», «Если ___, то ___») for substitution and
  frame-fill drills.
- **Target-book vocabulary** — already built for Anna Karenina
  (`app/data/books/`), extensible to other books via `build_book_vocab.py`.

---

## Sources (for chasing detail)

- Nation, *Learning Vocabulary in Another Language* (2013); "The four strands" (2007)
- Nation & Webb, *Researching and Analyzing Vocabulary* (2011)
- Hu & Nation, "Unknown vocabulary density and reading comprehension" (2000)
- Laufer & Ravenhorst-Kalovski, "Lexical threshold(s)…" (2010)
- Schmitt, Jiang & Grabe, "The percentage of words known in a text…" (2011)
- Waring & Takaki (2003); Pellicer-Sánchez & Schmitt (2010) — encounters needed
- Swain, "The output hypothesis" (2005 handbook chapter)
- Roediger & Karpicke, "Test-enhanced learning" (2006); Barcroft on productive vocab
- DeKeyser, *Practice in a Second Language* (2007) — automatization
- Levelt, *Speaking: From Intention to Articulation* (1989)
- Boers & Lindstromberg on collocation/formulaic language
- Guerrero, *Inner Speech — L2* (2005) — silent/covert practice
