"""A running internal estimate of how much Russian the learner actually knows —
overall and broken down by subject domain — plus a daily history so the stats
page can graph progress over time.

Signals it blends:
  * SRS cards (single-word cards + their word-formation families + words marked
    "known") give a hard floor — the frequency rank below which the learner
    plainly knows most words.
  * Flow-reading sessions give a live read: how often the learner taps an unknown
    word per 100 running words tells us where their comfortable reading level
    really sits, and which domains lag the rest.

The headline number is `known_rank` — a frequency-rank cutoff (≈ "knows the N
most common words"). Everything else (estimated vocabulary size, CEFR band,
per-domain comprehension) is derived from it plus the card counts.

Storage:
  * app_settings["known_rank"]     — the global cutoff (blended, slow-moving)
  * app_settings["prof_domains"]   — JSON {domain: {rank_est, words_read, taps, sessions}}
  * proficiency_snapshots          — one row per day, for the graphs
"""
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import books       # noqa: E402
import llm         # noqa: E402
import srs         # noqa: E402
import store       # noqa: E402

_RANK_MIN, _RANK_MAX = 800, 22000
_DEFAULT_RANK = 3500      # a B1/B2 reader's rough starting cutoff, before any signal
_MATURE_STABILITY = 21

# ---------------------------------------------------------------- domains

# A fixed spread of subject areas. `topics` seed the picker; `kw` classify a
# free-typed prompt. Order is roughly "everyday → specialised".
DOMAINS = [
    {"id": "daily", "label": "Everyday life",
     "blurb": "routines, home, errands, money, phones",
     "topics": ["A normal weekday, hour by hour",
                "Everything that went wrong before I left the apartment",
                "Trying to return something to a shop without the receipt",
                "How I actually use my phone all day"],
     "kw": ["apartment", "morning", "routine", "errand", "shop", "store", "money",
            "rent", "commute", "chores", "neighbour", "neighbor", "phone"]},
    {"id": "family", "label": "Family & relationships",
     "blurb": "feelings, in-laws, meeting the parents, friendship",
     "topics": ["How my girlfriend and I met",
                "Meeting her parents for the first time",
                "Two friends talk honestly about getting older",
                "What Russians mean when they talk about friendship"],
     "kw": ["family", "girlfriend", "boyfriend", "parents", "mother", "father",
            "in-law", "friend", "friendship", "relationship", "feelings", "love",
            "wedding", "grandmother", "grandfather"]},
    {"id": "work", "label": "Work & tech",
     "blurb": "articles on AI, software, the tech industry",
     "topics": ["Will AI take most jobs? What people in the field actually argue",
                "Open vs closed AI models — the case each side makes",
                "What the last year of AI progress actually changed",
                "Why software is still hard, explained without the jargon"],
     "kw": ["work", "job", "office", "programmer", "engineer", "software", "code",
            "startup", "company", "career", "interview", "salary", "meeting",
            "colleague", "boss", "computer", "developer"]},
    {"id": "food", "label": "Food & cooking",
     "blurb": "recipes, ingredients, the kitchen, eating out",
     "topics": ["Cooking borscht for the first time — what went wrong",
                "A recipe with ingredients I'd never heard of",
                "Ordering at a restaurant when I can't read half the menu",
                "How to shop at a Russian market"],
     "kw": ["food", "cook", "cooking", "recipe", "kitchen", "ingredient", "dish",
            "restaurant", "meal", "bake", "soup", "market", "grocery", "spice",
            "flavour", "flavor", "dinner", "breakfast"]},
    {"id": "nature", "label": "Nature & science",
     "blurb": "popular-science articles: animals, space, how things work",
     "topics": ["The strangest animals of the Russian far north",
                "How a thunderstorm actually forms",
                "What we now know about how birds navigate",
                "The insects underfoot that quietly run the ecosystem"],
     "kw": ["animal", "bird", "insect", "fish", "plant", "tree", "forest",
            "nature", "weather", "science", "biology", "space", "ocean", "river",
            "mountain", "climate", "species", "wildlife"]},
    {"id": "politics", "label": "Politics & society",
     "blurb": "articles on recent news, government, society",
     "topics": ["A recent election somewhere, and what actually decided it",
                "A new law in the news — the argument for and against it",
                "How a big international story is being reported, and by whom",
                "A calm explainer of how one government institution works"],
     "kw": ["politics", "government", "election", "law", "policy", "news",
            "society", "protest", "president", "parliament", "economy", "tax",
            "rights", "war", "immigration", "media"]},
    {"id": "culture", "label": "Culture & history",
     "blurb": "essays on history, traditions, the arts",
     "topics": ["How New Year became the biggest Russian holiday",
                "A short history of a city worth knowing",
                "The paintings every Russian grew up seeing, and why",
                "Where a few stubborn superstitions actually come from"],
     "kw": ["history", "culture", "tradition", "holiday", "festival", "art",
            "museum", "painting", "music", "literature", "poem", "church",
            "century", "war", "empire", "custom", "folklore"]},
    {"id": "fiction", "label": "Classic fiction",
     "blurb": "19th-c. Russian-novel prose — prep for Tolstoy, Dostoevsky…",
     "topics": ["A landowner returns to his estate after ten years in the city",
                "An awkward dinner where everyone is pretending",
                "A young officer, a card game, and a debt he can't pay",
                "A governess arrives at a house with too many secrets",
                "Two travellers share a coach through the winter night"],
     "kw": ["story", "mystery", "novel", "character", "fiction", "tale", "plot",
            "classic", "tolstoy", "dostoevsky", "chekhov", "estate", "19th century",
            "drama", "chapter"]},
    {"id": "scifi", "label": "Sci-fi & the speculative",
     "blurb": "near-future, space, technology gone strange",
     "topics": ["A gentle sci-fi story about a city that never sleeps",
                "First contact, told entirely through radio messages",
                "A person wakes up 100 years later in the same apartment",
                "The last repair technician on a dying space station"],
     "kw": ["sci-fi", "scifi", "science fiction", "future", "robot", "space",
            "alien", "spaceship", "dystopia", "time travel", "android", "colony",
            "cyberpunk", "AI"]},
    {"id": "travel", "label": "Travel & cities",
     "blurb": "getting around, directions, hotels, neighbourhoods",
     "topics": ["Getting hopelessly lost on the Moscow metro",
                "Checking into a hotel when something's wrong with the booking",
                "A walking tour of a neighbourhood I've never been to",
                "Asking strangers for directions and actually understanding them"],
     "kw": ["travel", "trip", "city", "metro", "subway", "train", "airport",
            "hotel", "directions", "map", "tourist", "street", "neighbourhood",
            "neighborhood", "flight", "station", "luggage"]},
    {"id": "health", "label": "Health & the body",
     "blurb": "the doctor, symptoms, the body, staying well",
     "topics": ["Describing symptoms to a doctor who doesn't speak English",
                "A normal check-up appointment, start to finish",
                "Explaining an old injury and how it happened",
                "What a pharmacist asks you and how to answer"],
     "kw": ["health", "doctor", "hospital", "sick", "illness", "symptom", "body",
            "pain", "medicine", "pharmacy", "injury", "dentist", "clinic",
            "nurse", "exercise", "sleep"]},
    {"id": "religion", "label": "Religion & philosophy",
     "blurb": "essays on world religions, Hindu & Buddhist thought",
     "topics": ["Vivekananda and the idea that all religions point to one truth",
                "Ramakrishna's parables, and what they were teaching",
                "Advaita Vedanta in plain terms: the self and the absolute",
                "Where Buddhism and classical Hindu philosophy actually disagree",
                "The Bhagavad Gita: what Krishna tells Arjuna, and why",
                "How the Hare Krishna movement grew from a Bengali tradition",
                "A short, fair introduction to one of the world's religions"],
     "kw": ["religion", "religious", "hindu", "hinduism", "buddhism", "buddhist",
            "vedanta", "advaita", "vivekananda", "ramakrishna", "krishna",
            "dharma", "karma", "nirvana", "moksha", "upanishad", "gita", "sutra",
            "meditation", "monk", "monastery", "philosophy", "soul", "god",
            "faith", "spiritual", "prayer", "church", "temple", "islam",
            "christianity", "judaism", "orthodox", "theology", "mysticism"]},
    {"id": "smalltalk", "label": "Small talk",
     "blurb": "the light conversation that carries a relationship",
     "topics": ["Small talk that goes surprisingly deep on a long train ride",
                "The weather, the weekend, and what to say next",
                "Catching up with someone you haven't seen in a year",
                "Making conversation at a dinner where you know one person"],
     "kw": ["small talk", "chat", "conversation", "weekend", "weather",
            "catch up", "party", "dinner party", "greeting"]},
]
_BY_ID = {d["id"]: d for d in DOMAINS}
_DEFAULT_DOMAIN = "daily"

# Per-domain grounding: passed to the reading generator so factual domains are
# built on real events / people / positions instead of invented stand-ins.
# Empty (fiction, small talk, everyday life) = ordinary invented prose is fine.
_GROUNDING = {
    "politics": (
        "Base this on actual news and history. Name real countries, real "
        "governments, real officials and organisations, and real reported events "
        "with roughly correct dates. When sides disagree, give the actual "
        "positions real parties or governments have taken — not invented ones. "
        "No fabricated laws, quotes, or spokespeople."),
    "work": (
        "When it touches AI or the tech industry, ground it in reality: real "
        "companies and real researchers, and the actual positions real figures "
        "in the field hold (on jobs and automation, on open vs closed models, on "
        "safety and regulation, on where the technology is heading). Name them. "
        "No invented labs, products, or quotes."),
    "nature": (
        "Ground every factual claim in real, mainstream science — real species, "
        "places, and mechanisms; real researchers and findings; and the actual "
        "state of scientific debate where one exists. Nothing invented or fringe "
        "presented as established fact."),
    "culture": (
        "Ground it in real history and real culture — real people, places, "
        "dates, movements and works. No invented events or figures."),
    "health": (
        "Keep all medical information real and accurate — mainstream, current "
        "medical understanding. No invented conditions, drugs, or advice."),
    "food": (
        "Use real dishes, real ingredients and real techniques, named "
        "correctly. Regional and historical detail should be accurate."),
    "travel": (
        "Use real cities, real neighbourhoods, real stations and lines, real "
        "landmarks — geographically accurate."),
    "religion": (
        "Represent every tradition accurately and fairly — real teachings, real "
        "history, real figures and texts, real doctrinal differences. Attribute "
        "claims to the tradition or thinker that holds them. No invented "
        "scriptures, gurus, or doctrines; no flattening one tradition into "
        "another."),
}


def domain_grounding(did):
    return _GROUNDING.get(did or "", "")


# Per-domain FORM — what genre / register the piece is written in. The factual
# domains read like real articles in the field (not a narrated story about people
# arguing); fiction reads like a 19th-century Russian novel to prep the reader
# for the classics. Domains not listed here stay flexible (light narrative or
# first-person, whatever the topic suggests).
_STYLE = {
    "work": (
        "Write it as a technology article or essay — the kind of thing found on a "
        "serious Russian tech site or a thoughtful blog, or what an English "
        "speaker would read on the topic online. Recount what is actually "
        "happening in the field and give the author's reading of what it means. "
        "Where people disagree, lay out each position neutrally and attributively "
        "(«одни исследователи полагают…», «другие возражают, что…», «по мнению …») "
        "and name the real figures who hold them. Explanatory register: gloss a "
        "term as you introduce it. Do NOT write it as a dramatised conversation "
        "between two engineers or a third-person story about people."),
    "politics": (
        "Write it as a news or analysis article — the kind a literate Russian "
        "reads online (think Meduza, РБК, a serious paper's explainer). Report an "
        "actual recent event: who, what, where, when. Where there is "
        "disagreement, give each side's position plainly and attributively "
        "(«сторонники … считают», «критики возражают, что…»). The author may "
        "interpret, but stays in the register of journalism. No invented "
        "dialogue, no narrated scene, not a story about two people talking."),
    "nature": (
        "Write it as a popular-science article — a clear factual explainer of the "
        "kind found in a good science magazine. What is known, how we know it, "
        "where scientists still disagree (attributively). Concrete examples. "
        "Explanatory, not a narrated walk or story."),
    "culture": (
        "Write it as a cultural or historical essay — factual, with the author's "
        "perspective, in the register of good popular-history writing. Not a "
        "story."),
    "health": (
        "Write it as a clear health explainer of the kind a reputable medical "
        "site publishes — factual, practical, calm. Not a narrated patient story "
        "unless the topic explicitly asks for one."),
    "fiction": (
        "Write it as literary prose in the manner of the 19th-century Russian "
        "novel — Tolstoy, Dostoevsky, Turgenev, Chekhov, and Bulgakov's «Мастер и "
        "Маргарита» at the later edge. Third-person narration with real scenes, "
        "gesture and interiority. This is preparation for reading the classics, "
        "so deliberately exercise the vocabulary a reader meets THERE but rarely "
        "in speech — always in a context that makes the meaning plain:\n"
        "  • narration & speech verbs: промолвил, пробормотал, возразил, "
        "усмехнулся, вздохнул, нахмурился, поморщился, спохватился, побрёл, "
        "потупился, вспыхнул, отшатнулся;\n"
        "  • manner adverbs: нехотя, украдкой, чинно, робко, поспешно, "
        "досадливо, снисходительно, рассеянно;\n"
        "  • DESCRIBING PEOPLE — face, build, bearing, dress, expression: "
        "сутулый, дородный, худощавый, смуглый, веснушчатый, приземистый, "
        "осанистый, бакенбарды, проседь, впалые щёки, надменный, приветливый;\n"
        "  • DESCRIBING ROOMS & places — furnishings, light, air, order: "
        "гостиная, кабинет, передняя, обои, портьеры, кресло, канделябр, "
        "полумрак, духота, натёртый паркет, изразцовая печь;\n"
        "  • THE PHYSICAL WORLD a scene is built from — materials (дуб, "
        "дубовый, кожа, бархат, ситец, холст, чугун, жесть, позолота), "
        "everyday-but-not-basic colours (бурый, сизый, багровый, лиловый, "
        "русый, вороной, седой, смуглый), smells (пахло сыростью / прелью / "
        "дымом / ладаном; тянуло холодом), textures and light (шершавый, "
        "липкий, тусклый, ослепительный);\n"
        "  • NATURE nearby — trees, undergrowth, weather: осина, ольха, "
        "верба, папоротник, вереск, крапива, репейник, мох, валежник, "
        "просека, овраг, изморозь, зной, сумерки;\n"
        "  • the objects and daily life of the period: извозчик, лакей, "
        "горничная, сюртук, пенсне, папироса, самовар, депеша, флигель, "
        "гувернантка, усадьба, крыльцо, дрожки.\n"
        "One or two genuinely new such words per paragraph — enough to build the "
        "world, not a costume parade. Setting: pre-revolutionary Russia unless "
        "the topic says otherwise."),
    "religion": (
        "Write it as an accessible essay on religious thought or history — the "
        "register of a good popular guide to religion, respectful and precise. "
        "Lay out what a tradition actually teaches, how it developed, and where "
        "traditions differ, attributively («по учению адвайты…», «буддийские "
        "школы, напротив, считают…»). Name the real figures, texts and movements "
        "(Вивекананда, Рамакришна, Шанкара, Будда, Упанишады, «Бхагавадгита»). "
        "Introduce each specialised term with a short gloss the first time. Not a "
        "sermon, not a story, not advocacy — explanation."),
    "scifi": (
        "Write it as a science-fiction short story — scene, character, a sense of "
        "wonder or unease. Modern narrative Russian."),
    "smalltalk": (
        "Mostly realistic casual dialogue, with light narration between the "
        "lines."),
}


def domain_style(did):
    return _STYLE.get(did or "", "")


_FORM_TAG = {
    "work": "article", "politics": "article", "nature": "article",
    "culture": "essay", "health": "explainer", "religion": "essay",
    "fiction": "classic prose", "scifi": "short story", "smalltalk": "dialogue",
}


def domains_public():
    """For the topic picker: id, label, blurb, seed topics."""
    return [{"id": d["id"], "label": d["label"], "blurb": d["blurb"],
             "topics": list(d["topics"]),
             "form": _FORM_TAG.get(d["id"], ""),
             "grounded": bool(_GROUNDING.get(d["id"]))} for d in DOMAINS]


def classify(topic="", prompt=""):
    """Which domain a session belongs to. Exact topic match wins; else keyword
    hit on the combined text; else the everyday-life default."""
    text = f"{topic} {prompt}".strip().lower()
    if not text:
        return _DEFAULT_DOMAIN
    for d in DOMAINS:
        if any(t.lower() == topic.strip().lower() for t in d["topics"]):
            return d["id"]
    best, best_hits = _DEFAULT_DOMAIN, 0
    for d in DOMAINS:
        hits = sum(1 for k in d["kw"] if k in text)
        if hits > best_hits:
            best, best_hits = d["id"], hits
    return best


def domain_label(did):
    d = _BY_ID.get(did)
    return d["label"] if d else (did or "").replace("-", " ").title()


# ---------------------------------------------------------------- card floor

def _known_lemmas(c):
    """Every lemma the learner plainly knows: single-word cards, their families,
    and words explicitly marked known/has_card."""
    known = {r["normalized_text"] for r in c.execute(
        "SELECT DISTINCT normalized_text FROM srs_cards WHERE is_phrase=0 AND normalized_text IS NOT NULL")}
    try:
        known |= {r["normalized_text"] for r in c.execute(
            "SELECT normalized_text FROM resolved_words WHERE reason IN ('known','has_card')")}
    except Exception:  # noqa: BLE001
        pass
    try:
        known |= {r["lemma"] for r in c.execute(
            """SELECT wf.lemma FROM word_family wf WHERE wf.root IN (
                 SELECT w2.root FROM word_family w2
                 JOIN srs_cards s ON s.normalized_text = w2.lemma AND s.is_phrase=0)""")}
    except Exception:  # noqa: BLE001
        pass
    return {l for l in known if l}


_COVERAGE = 0.80          # "knows this share of everything down to the floor"


def _card_floor(c):
    """A conservative lower bound on known_rank from coverage: the highest
    frequency rank down to which the learner still knows ~80% of the words.

    NB: which words the learner has *carded* says where their gaps are, not what
    they know — so this walks the frequency list and asks, cumulatively, what
    share of the commonest N words are accounted for (carded, in a card's family,
    or explicitly marked known). Coverage starts near 1.0 at the very top and
    falls off; the floor is where it crosses the threshold."""
    known = _known_lemmas(c)
    if len(known) < 40:
        return _RANK_MIN
    rows = c.execute(
        "SELECT normalized_text, rank FROM freq WHERE rank <= 12000 ORDER BY rank")
    seen = hit = 0
    floor = _RANK_MIN
    for r in rows:
        seen += 1
        if r["normalized_text"] in known:
            hit += 1
        if seen % 250 == 0:
            if hit / seen >= _COVERAGE:
                floor = r["rank"]
            elif seen >= 1500:
                break
    return int(max(_RANK_MIN, min(_RANK_MAX, floor)))


# ---------------------------------------------------------------- global cutoff

def _stored_rank():
    try:
        return int(srs.get_setting("known_rank", _DEFAULT_RANK) or _DEFAULT_RANK)
    except Exception:  # noqa: BLE001
        return _DEFAULT_RANK


def known_rank():
    """The global reading-vocabulary cutoff — the blended estimate, never below
    what the card collection alone proves."""
    c = store.connect()
    try:
        return max(_stored_rank(), _card_floor(c))
    finally:
        c.close()


# ---------------------------------------------------------------- target books

# how token-coverage of a whole book maps to "could you actually read it"
# (Hu & Nation 2000 / Laufer & Ravenhorst-Kalovski 2010 coverage thresholds)
_READINESS = [
    (0.985, "ready", "you'd read it comfortably, looking up the odd word"),
    (0.965, "almost", "readable with a dictionary next to you"),
    (0.930, "not yet", "hard going — you'd stop to look things up constantly"),
    (0.000, "a way to go", "too much unknown vocabulary to enjoy it right now"),
]


_book_cache = {}          # book -> (computed_at, result)


def book_readiness(book="anna_karenina"):
    """How much of `book` the learner could read now: token coverage (share of
    running words known), a difficulty read, and the frequent unknowns to target.
    Cached ~90s — the inputs (cards, known_rank) move slowly."""
    import time
    hit = _book_cache.get(book)
    if hit and time.time() - hit[0] < 90:
        return hit[1]
    try:
        fq = books.freq(book)
        m = books.meta(book)
    except Exception:  # noqa: BLE001
        return None
    c = store.connect()
    have = _known_lemmas(c)
    kr = max(_stored_rank(), _card_floor(c))
    todo = [l for l in fq if l not in have]
    ranks = store.rank_map(set(todo))
    # the 13k-word common stoplist counts as "known" — a B2 reader has these
    ph = ",".join("?" * len(todo)) if todo else "''"
    common = {r["normalized_text"] for r in c.execute(
        f"SELECT normalized_text FROM stoplist WHERE normalized_text IN ({ph})",
        todo)} if todo else set()
    c.close()

    def known(lem):
        if lem in have or lem in common:
            return True
        r = ranks.get(lem)
        return r is not None and r <= kr

    total = sum(fq.values()) or 1
    known_tok = sum(cnt for lem, cnt in fq.items() if known(lem))
    gaps = sorted(((cnt, lem) for lem, cnt in fq.items() if not known(lem)), reverse=True)
    coverage = known_tok / total
    types_known = 1 - len(gaps) / max(1, len(fq))
    for cut, verdict, feel in _READINESS:
        if coverage >= cut:
            break
    # a clean "words to target" list: real dictionary words only (drops leftover
    # names, lemmatiser noise), commonest first
    c = store.connect()
    top = [l for _, l in gaps[:400]]
    ph = ",".join("?" * len(top)) if top else "''"
    indict = {r["headword"] for r in c.execute(
        f"SELECT headword FROM dict_ru WHERE headword IN ({ph})", top)} if top else set()
    c.close()
    clean = [(n, l) for n, l in gaps if l in indict and len(l) > 3][:60]
    out = {
        "book": book, "title": m.get("title"), "author": m.get("author"),
        "coverage": round(coverage, 4),
        "types_known": round(types_known, 3),
        "unknown_lemmas": len(gaps),
        "key_words_to_go": sum(1 for cnt, _ in gaps if cnt >= 4),
        "verdict": verdict, "feels_like": feel,
        "top_gaps": [{"lemma": l, "count": n} for n, l in clean],
    }
    _book_cache[book] = (__import__("time").time(), out)
    return out


def fiction_target_words(limit=10, book="anna_karenina"):
    """The classic-novel words worth rehearsing in a fiction story: the frequent
    unknowns from the target book, with a couple of rarer content words mixed in
    so distinctive-but-uncommon vocabulary gets an airing too."""
    r = book_readiness(book)
    if not r or not r["top_gaps"]:
        return []
    _noise = {"утром", "вечером", "днем", "ночью", "деньга", "спасть", "старое",
              "конченый", "родный", "белые"}
    gaps = [g for g in r["top_gaps"] if g["lemma"] not in _noise and g["count"] >= 3]
    picks = [g["lemma"] for g in gaps[:max(1, limit - 3)]]
    mid = [g["lemma"] for g in gaps if 3 <= g["count"] <= 8][3:6]
    seen, out = set(), []
    for w in picks + mid:
        if w not in seen and len(w) > 3:
            seen.add(w)
            out.append(w)
    return out[:limit]


def starting_rank(domain=None):
    """Where a new flow-reading session should start. Global cutoff, pulled down
    toward the per-domain estimate when the learner is known to be weaker there."""
    g = known_rank()
    dom = _domain_state().get(domain or "")
    if dom and dom.get("rank_est"):
        return int(max(_RANK_MIN, min(g, dom["rank_est"])))
    return int(g)


# ---------------------------------------------------------------- per-domain

def _domain_state():
    try:
        raw = srs.get_setting("prof_domains", "") or ""
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}


def _save_domain_state(st):
    srs.set_setting("prof_domains", json.dumps(st, ensure_ascii=False))


def note_session(sid):
    """Fold a flow-reading session's live signal back into the global cutoff and
    its domain's running estimate. Cheap; call after each chunk the reader
    finishes."""
    c = store.connect()
    s = c.execute(
        "SELECT id, domain, rank_est, words_read, unknown_seen, topic, prompt "
        "FROM reading_flow_sessions WHERE id=?", (sid,)).fetchone()
    c.close()
    if not s or (s["words_read"] or 0) < 120:
        return
    did = s["domain"] or classify(s["topic"] or "", s["prompt"] or "")
    sess_rank = int(s["rank_est"] or _DEFAULT_RANK)
    words = int(s["words_read"] or 0)
    taps = int(s["unknown_seen"] or 0)

    # global: slow exponential blend, floored by the card collection
    cur = _stored_rank()
    blended = round(0.82 * cur + 0.18 * sess_rank)
    c = store.connect()
    floor = _card_floor(c)
    c.close()
    srs.set_setting("known_rank", int(max(_RANK_MIN, min(_RANK_MAX, max(blended, floor)))))

    # domain: track cumulative words/taps + a blended rank estimate
    st = _domain_state()
    d = st.get(did) or {"rank_est": sess_rank, "words_read": 0, "taps": 0, "sessions": 0}
    d["rank_est"] = round(0.7 * d.get("rank_est", sess_rank) + 0.3 * sess_rank)
    d["words_read"] = int(d.get("words_read", 0)) + words
    d["taps"] = int(d.get("taps", 0)) + taps
    d["sessions"] = int(d.get("sessions", 0)) + 1
    d["updated"] = _today()
    st[did] = d
    _save_domain_state(st)


def _comprehension_from(taps, words):
    """Share of running words the reader knew — 1 tap per 100 words ≈ 99%. Floored
    so a rough patch doesn't read as zero."""
    if not words:
        return None
    rate = taps / words
    return round(max(0.55, min(0.999, 1.0 - rate)), 3)


def _domain_rows(c):
    """Per-domain fluency: seed every domain, overlay whatever the learner has
    actually read. Sorted weakest-comprehension first."""
    st = _domain_state()
    # live recompute of words/taps straight from sessions (authoritative)
    live = {}
    for r in c.execute(
        "SELECT domain, COALESCE(SUM(words_read),0) w, COALESCE(SUM(unknown_seen),0) t, "
        "COUNT(*) n FROM reading_flow_sessions WHERE domain IS NOT NULL GROUP BY domain"):
        live[r["domain"]] = (r["w"], r["t"], r["n"])
    g = known_rank()
    out = []
    for d in DOMAINS:
        did = d["id"]
        w, t, n = live.get(did, (0, 0, 0))
        s = st.get(did) or {}
        rank_est = int(s.get("rank_est") or g)
        comp = _comprehension_from(t, w)
        out.append({
            "id": did, "label": d["label"], "blurb": d["blurb"],
            "words_read": int(w), "sessions": int(n),
            "rank_est": rank_est,
            "cefr": llm._reading_level_line(rank_est)[0],
            "comprehension": comp,
            "started": bool(w),
        })
    out.sort(key=lambda r: (not r["started"], r["comprehension"] or 1.0))
    return out


# ---------------------------------------------------------------- estimate

def _iso_day():
    import datetime as _dt
    return _dt.date.today().isoformat()


_today = _iso_day


def estimate():
    """The current headline picture — computed fresh, no side effects."""
    c = store.connect()
    q = c.execute
    kr = max(_stored_rank(), _card_floor(c))
    cefr, _ = llm._reading_level_line(kr)
    cards_total = q("SELECT COUNT(*) n FROM srs_cards").fetchone()["n"]
    cards_word = q("SELECT COUNT(*) n FROM srs_cards WHERE is_phrase=0").fetchone()["n"]
    cards_mature = q("SELECT COUNT(*) n FROM srs_cards WHERE is_phrase=0 AND suspended=0 "
                     "AND stability >= ?", (_MATURE_STABILITY,)).fetchone()["n"]
    words_read = q("SELECT COALESCE(SUM(words_read),0) n FROM reading_flow_sessions"
                   ).fetchone()["n"]
    taps_total = q("SELECT COALESCE(SUM(unknown_seen),0) n FROM reading_flow_sessions"
                   ).fetchone()["n"]
    # comprehension from the last ~2000 words read
    recent = q("SELECT COALESCE(SUM(words_read),0) w, COALESCE(SUM(unknown_seen),0) t FROM ("
               "  SELECT words_read, unknown_seen FROM reading_flow_sessions "
               "  WHERE words_read > 0 ORDER BY COALESCE(last_read_at, created_at) DESC LIMIT 6)"
               ).fetchone()
    comp = _comprehension_from(recent["t"], recent["w"])
    domains = _domain_rows(c)
    c.close()

    try:
        retention = srs.analytics(days=30).get("retention")
    except Exception:  # noqa: BLE001
        retention = None

    book = book_readiness("anna_karenina")
    try:
        import activate
        active_words = activate.active_count()
    except Exception:  # noqa: BLE001
        active_words = 0
    try:
        import speaking_levels
        speaking = speaking_levels.estimate(reading_cefr=cefr)
    except Exception as _e:  # noqa: BLE001
        print(f"[proficiency] speaking estimate: {_e}", flush=True)
        speaking = None

    # vocabulary size: the cutoff is "knows most words this common"; scale down a
    # little because nobody knows every word above their cutoff, then add cards
    # that sit beyond it.
    known_words = int(kr * 0.82)

    return {
        "known_rank": int(kr),
        "known_words": known_words,
        "active_words": active_words,
        "cefr": cefr,
        "cards_total": cards_total,
        "cards_word": cards_word,
        "cards_mature": cards_mature,
        "words_read": int(words_read),
        "taps_total": int(taps_total),
        "comprehension": comp,
        "retention": retention,
        "domains": domains,
        "book": book,
        "speaking": speaking,
        "day": _iso_day(),
    }


# ---------------------------------------------------------------- snapshots

_SNAP_COLS = ("known_words", "known_rank", "cefr", "cards_total", "cards_word",
              "cards_mature", "words_read", "comprehension", "retention", "domains")


def snapshot(force=True):
    """Write (or refresh) today's history row. Cheap enough to call on every
    stats load and after every reading chunk."""
    e = estimate()
    day = e["day"]
    dom_json = json.dumps({d["id"]: {"rank_est": d["rank_est"],
                                     "comprehension": d["comprehension"],
                                     "words_read": d["words_read"]}
                           for d in e["domains"]}, ensure_ascii=False)
    c = store.connect()
    exists = c.execute("SELECT 1 FROM proficiency_snapshots WHERE day=?", (day,)).fetchone()
    if exists and not force:
        c.close()
        return e
    ak_cov = (e.get("book") or {}).get("coverage")
    sp = e.get("speaking") or {}
    sp_ord = sp.get("ord")
    rd_ord = sp.get("reading_ord")
    sp_cefr = sp.get("cefr")
    c.execute(
        """INSERT INTO proficiency_snapshots
             (day, known_words, known_rank, cefr, cards_total, cards_word,
              cards_mature, words_read, comprehension, retention, domains,
              ak_coverage, active_words, speaking_ord, reading_ord, speaking_cefr)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(day) DO UPDATE SET
             known_words=excluded.known_words, known_rank=excluded.known_rank,
             cefr=excluded.cefr, cards_total=excluded.cards_total,
             cards_word=excluded.cards_word, cards_mature=excluded.cards_mature,
             words_read=excluded.words_read, comprehension=excluded.comprehension,
             retention=excluded.retention, domains=excluded.domains,
             ak_coverage=excluded.ak_coverage, active_words=excluded.active_words,
             speaking_ord=excluded.speaking_ord, reading_ord=excluded.reading_ord,
             speaking_cefr=excluded.speaking_cefr""",
        (day, e["known_words"], e["known_rank"], e["cefr"], e["cards_total"],
         e["cards_word"], e["cards_mature"], e["words_read"], e["comprehension"],
         e["retention"], dom_json, ak_cov, e.get("active_words"),
         sp_ord, rd_ord, sp_cefr))
    c.commit()
    c.close()
    return e


def history(days=180):
    c = store.connect()
    rows = c.execute(
        "SELECT * FROM proficiency_snapshots WHERE day >= date('now', ?) ORDER BY day",
        (f"-{int(days)} days",)).fetchall()
    c.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["domains"] = json.loads(d.get("domains") or "{}")
        except Exception:  # noqa: BLE001
            d["domains"] = {}
        out.append(d)
    return out


def overview(days=180):
    """Everything the stats page needs in one call."""
    e = snapshot(force=True)
    return {**e, "history": history(days),
            "books": books.all_meta(),
            "domain_labels": {d["id"]: d["label"] for d in DOMAINS}}
