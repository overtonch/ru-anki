"""Chunk deck — production drill for formulaic sequences ("prefabs").

The premise (Lexical Approach / formulaic-language research): a big share of
fluent speech is not built word-by-word in real time, it's pre-built multi-word
chunks slotted into sentences. This drill burns those chunks in so they surface
without construction lag.

A card shows a short, casual English utterance with ONE chunk highlighted; you
say the whole thing in Russian, and grade yourself on whether the *chunk* came
out right. Chunks are tagged by conversational FUNCTION (opening, hedging,
reacting, connecting, …) so progress slices by function, and the ones you keep
missing come back as spaced re-tests.

The scaffold FADES per chunk as you get it right — full English → English gist
→ a Russian cloze — so it auto-adjusts to your speaking level. Cards live in
`chunk_items`, misses in `chunk_lapse`, the per-chunk scaffold state in
`chunk_stage`. Same rolling-generation / self-grade / promote-to-SRS machinery
as `motion.py`.
"""
import json
import random
import re
import threading

import llm
import srs
import store

# ---------------------------------------------------------------- taxonomy

# id, label, one-line "what it's for"
FUNCTIONS = [
    ("open",     "openers",        "starting a turn — buying a beat before you speak"),
    ("time",     "buying time",    "holding the floor while you find the words"),
    ("hedge",    "hedging",        "softening a claim to an opinion or a guess"),
    ("react",    "reacting",       "surprise, sympathy, 'I'm listening' noises"),
    ("agree",    "agreeing",       "signalling you're on the same page"),
    ("disagree", "pushing back",   "disagreeing without a flat 'no'"),
    ("connect",  "connecting",     "structuring an argument, adding, reformulating"),
    ("narrate",  "telling a story","sequencing past events, marking the turn"),
    ("hypo",     "hypotheticals",  "'what if', 'if I were you', 'let's say'"),
    ("opinion",  "taking a stance","'personally', 'not my thing', 'I'm all for it'"),
    ("clarify",  "clarifying",     "checking understanding, repairing, rephrasing"),
    ("close",    "wrapping up",    "handing the turn back, ending an answer"),
    ("plan",     "making plans",   "proposing, coordinating, pinning down the when & where"),
    ("smalltalk","catching up",    "reconnecting — 'how've you been', 'what's new'"),
    ("ask",      "asking & offering", "softened requests, and offering a hand"),
    ("warmth",   "warmth & support", "'don't worry', 'I'm happy for you', 'take care'"),
    ("social",   "thanks & sorry", "the politeness glue — apologising, thanking, brushing it off"),
]
_FN = {f[0]: f for f in FUNCTIONS}
_FN_IDS = [f[0] for f in FUNCTIONS]


class Ch:
    __slots__ = ("id", "fn", "en", "ru", "note", "lit", "reg", "level", "ex")

    def __init__(self, id, fn, en, ru, note, lit="", reg="casual", level="b1", ex=()):
        self.id, self.fn, self.en, self.ru = id, fn, en, ru
        self.note, self.lit, self.reg, self.level = note, lit, reg, level
        self.ex = list(ex)

    def spec(self):
        return {"id": self.id, "fn": self.fn, "fn_label": _FN[self.fn][1],
                "ru": self.ru, "en": self.en, "note": self.note,
                "lit": self.lit, "reg": self.reg, "level": self.level}


# A curated seed catalogue of high-value Russian conversational prefabs. Kept
# deliberately compact and extensible — add rows, nothing else needs to change.
CHUNKS = [
    # ---- openers ----
    Ch("nu", "open", "well… / so…", "ну…",
       "The all-purpose Russian turn-opener; buys a beat before you commit.",
       "well", "casual", "a2", ["Ну, что тебе сказать…"]),
    Ch("slushay", "open", "listen, … / hey, …", "слушай, …",
       "Grabs attention before a point or a request. «смотри» works the same way.",
       "listen", "casual", "a2", ["Слушай, а ты завтра свободен?"]),
    Ch("znaesh-chto", "open", "you know what, …", "знаешь что, …",
       "Signals you're about to say something a bit unexpected or decisive.",
       "you-know what", "casual", "a2", ["Знаешь что, давай не пойдём."]),
    Ch("delo-v-tom", "open", "the thing is, …", "дело в том, что …",
       "Frames an explanation or an excuse. Extremely high frequency.",
       "matter is in that, that", "neutral", "b1", ["Дело в том, что я не успел."]),
    Ch("na-samom-dele", "open", "actually, … / in fact, …", "на самом деле …",
       "Corrects an assumption or introduces the real picture.",
       "on the real deed", "casual", "b1", ["На самом деле всё немного сложнее."]),
    Ch("esli-chestno", "open", "honestly, … / to be honest", "если честно, …",
       "Prefaces a candid or slightly awkward opinion. Your everyday 'honestly'.",
       "if honestly", "casual", "a2", ["Если честно, я очень устал."]),
    Ch("kak-tebe-skazat", "open", "how can I put it…", "как тебе сказать…",
       "Opener and time-buyer in one, for when the answer isn't simple. "
       "«как вам сказать» if you're on «вы».",
       "how to-you to-say", "casual", "b1", ["Как тебе сказать… и да, и нет."]),
    Ch("po-pravde", "open", "truth be told / to tell the truth", "по правде говоря, …",
       "A touch folksy but warm — fine with any relative. «если честно» is the "
       "plainer everyday default.",
       "on truth speaking", "neutral", "b1", ["По правде говоря, я про это забыл."]),

    # ---- buying time ----
    Ch("kak-by-skazat", "time", "how should I put it…", "как бы это сказать…",
       "Classic stall while you find the wording.",
       "how as-if this to-say", "casual", "b1", ["Как бы это сказать, чтобы не обидеть."]),
    Ch("day-podumat", "time", "let me think", "дай подумать / дайте подумать",
       "Buys a few seconds openly and politely.",
       "give to-think", "casual", "a2", ["Дай подумать… нет, не помню."]),
    Ch("pogodi", "time", "hang on / hold on a sec", "погоди / секунду",
       "Holds the floor while you gather your thought. «секундочку» is the softer, "
       "more polite version; «погодите» on «вы».",
       "wait / a second", "casual", "a2", ["Погоди, я сейчас вспомню."]),
    Ch("nu-v-obshchem", "time", "well, basically… / so, anyway…", "ну, в общем…",
       "Restarts a stalled sentence and pushes it along.",
       "well, in general", "casual", "a2", ["Ну, в общем, я согласен."]),
    Ch("eto-kak-ego", "time", "it's… what's it called", "это… как его / как это…",
       "Stalls while you reach for a specific word.",
       "this how it", "casual", "b1", ["Мне нужен этот… как его… штопор."]),
    Ch("chto-eshche-skazat", "time", "what else can I say…", "что ещё сказать…",
       "Fills a pause mid-answer and invites you to keep going.",
       "what else to-say", "casual", "b1", ["Что ещё сказать… в целом всё нормально."]),
    Ch("s-chego-nachat", "time", "where do I even start", "даже не знаю, с чего начать",
       "Opens a long answer while you organise it in your head.",
       "even not I-know, from what to-begin", "casual", "b2", []),

    # ---- hedging ----
    Ch("mne-kazhetsya", "hedge", "it seems to me / I feel like", "мне кажется, (что) …",
       "Softens a claim to an impression. One of the most useful frames there is.",
       "to-me it-seems that", "neutral", "a2", ["Мне кажется, он обиделся."]),
    Ch("po-moemu", "hedge", "in my view / I think", "по-моему, …",
       "One-word hedge; drop it in almost anywhere.",
       "on my [opinion]", "casual", "a2", ["По-моему, так будет лучше."]),
    Ch("naskolko-znayu", "hedge", "as far as I know", "насколько я знаю, …",
       "Limits your claim to your own knowledge.",
       "as-far-as I know", "neutral", "b1", ["Насколько я знаю, магазин уже закрыт."]),
    Ch("esli-ne-oshibayus", "hedge", "if I'm not mistaken", "если я не ошибаюсь, …",
       "Hedges a fact you're fairly but not fully sure of.",
       "if I not err", "neutral", "b1", ["Если я не ошибаюсь, это было в среду."]),
    Ch("vrode-by", "hedge", "sort of / apparently / I think so", "вроде бы …",
       "Light 'apparently'; marks second-hand or uncertain information.",
       "like would", "casual", "b1", ["Вроде бы он уже уехал."]),
    Ch("skoree-vsego", "hedge", "most likely / probably", "скорее всего, …",
       "Your default 'probably'.",
       "rather of-all", "neutral", "b1", ["Скорее всего, они опоздают."]),
    Ch("mogu-oshibatsya", "hedge", "I could be wrong, but…", "могу ошибаться, но …",
       "Pre-empts disagreement before a strong opinion.",
       "I-can to-err, but", "casual", "b1", ["Могу ошибаться, но, по-моему, это плохая идея."]),
    Ch("v-printsipe", "hedge", "basically / in principle / I guess", "в принципе, …",
       "Concedes a point loosely, or softens agreement.",
       "in principle", "casual", "b1", ["В принципе, я не против."]),

    # ---- reacting ----
    Ch("da-ladno", "react", "no way! / come on! / you're kidding", "да ладно!",
       "Friendly disbelief or surprise. Tone carries the meaning.",
       "yes okay", "casual", "a2", ["— Он выиграл. — Да ладно!"]),
    Ch("serezno", "react", "seriously? / for real?", "серьёзно? / правда?",
       "Checks a surprising claim.",
       "seriously", "casual", "a2", ["Серьёзно? Ты сам это сделал?"]),
    Ch("ne-mozhet-byt", "react", "that can't be / no way", "не может быть!",
       "Stronger disbelief than «да ладно».",
       "not it-can to-be", "casual", "a2", ["Не может быть, я же только что там был."]),
    Ch("nu-nado-zhe", "react", "well would you look at that / huh", "ну надо же",
       "Mild wonder at something unexpected. Warm, but skews older — «вот это да» "
       "or «серьёзно?» sit more naturally on a younger guy.",
       "well one-must EMPH", "casual", "b1", ["Ну надо же, как всё изменилось."]),
    Ch("vot-eto-da", "react", "wow / now that's something", "вот это да",
       "Impressed reaction.",
       "here this yes", "casual", "b1", ["Вот это да, я не ожидал."]),
    Ch("da-ty-chto", "react", "you don't say / whoa, really", "да ты что!",
       "Warm surprise or concern — NOT a challenge. Tone is everything; keep it "
       "light. «да вы что» on «вы».",
       "yes you what", "casual", "a2", ["Да ты что, когда это случилось?"]),
    Ch("zhal", "react", "that's a shame / too bad", "жаль / жалко",
       "Sympathetic reaction to bad news.",
       "pity", "casual", "a2", ["Жаль, что так вышло."]),
    Ch("ponyatno", "react", "got it / I see / makes sense", "понятно / ясно",
       "Acknowledges you followed. «ясно» said flatly can sound curt.",
       "understandable", "casual", "a2", ["Понятно, тогда идём без него."]),
    Ch("s-uma-soyti", "react", "that's wild / hard to believe", "с ума сойти",
       "Strong reaction, positive or negative. No vulgarity — fine with anyone.",
       "from the mind to-go", "casual", "b1", ["Такие цены — с ума сойти."]),
    Ch("nichego-strashnogo", "react", "no worries / it's fine / don't worry about it",
       "да ничего страшного",
       "Waves off someone's apology or mistake. The kind thing to say — instantly "
       "lowers the temperature.",
       "no nothing scary", "casual", "a2", ["— Прости, я опоздал. — Да ничего страшного."]),

    # ---- agreeing ----
    Ch("vot-imenno", "agree", "exactly / that's just it", "вот именно",
       "Emphatic agreement — 'you've put your finger on it'.",
       "here namely", "casual", "b1", ["— Дело не в деньгах. — Вот именно."]),
    Ch("soglasen", "agree", "agreed / I agree", "согласен / согласна",
       "Plain agreement; mark the gender.",
       "agreed", "neutral", "a2", ["Полностью с тобой согласен."]),
    Ch("ya-tozhe-tak-dumayu", "agree", "I think so too", "я тоже так думаю",
       "Aligns your view with theirs.",
       "I also so think", "neutral", "a2", ["Я тоже так думаю, давно пора."]),
    Ch("eto-tochno", "agree", "that's for sure / definitely", "это точно",
       "Confirms a shared observation.",
       "this exactly", "casual", "a2", ["Да, это точно, зимой тут холодно."]),
    Ch("ne-to-slovo", "agree", "you said it / and then some", "не то слово",
       "Agrees that their word was an understatement.",
       "not that word", "casual", "b2", ["— Устал? — Не то слово."]),
    Ch("i-ne-govori", "agree", "tell me about it / don't even", "и не говори",
       "Warm agreement with a complaint.",
       "and not say", "casual", "b1", ["— Пробки ужасные. — И не говори."]),

    # ---- pushing back ----
    Ch("ne-sovsem-tak", "disagree", "not quite / not exactly", "не совсем так",
       "Gentle correction without a flat 'no'.",
       "not entirely so", "casual", "b1", ["Ну, не совсем так, дай объясню."]),
    Ch("ne-obizhaysya", "disagree", "don't take this the wrong way, but…",
       "ты только не обижайся, но …",
       "Cushions honest criticism before you give it — signals it comes from care. "
       "«вы только не обижайтесь» on «вы».",
       "you only not take-offence, but", "casual", "b1",
       ["Ты только не обижайся, но по-моему ты перегибаешь."]),
    Ch("ya-by-tak-ne-skazal", "disagree", "I wouldn't say that", "я бы так не сказал",
       "Polite disagreement; distances you from the claim.",
       "I would so not say", "neutral", "b1", []),
    Ch("smotrya", "disagree", "depends / depends how", "смотря как / смотря что",
       "Refuses a yes/no and opens the nuance.",
       "looking how / looking what", "casual", "b1", ["— Это дорого? — Смотря для кого."]),
    Ch("da-no", "disagree", "yes, but… / true, but…", "да, но …",
       "Concede-then-counter; the workhorse move of any argument.",
       "yes, but", "casual", "a2", ["Да, но у нас нет на это времени."]),
    Ch("s-odnoy-storony", "disagree", "on one hand… on the other…",
       "с одной стороны… с другой стороны…",
       "Frames a balanced, two-sided take.",
       "from one side… from the other side", "neutral", "b1", []),
    Ch("ne-dumayu", "disagree", "I don't think so", "не думаю / вряд ли",
       "Mild rejection. «вряд ли» = 'I doubt it'.",
       "not I-think", "casual", "a2", ["Вряд ли он придёт в такую погоду."]),
    Ch("delo-ne-v-etom", "disagree", "that's not really the point", "да я не про то",
       "Redirects to the real issue. «да я не про то» / «я не об этом» is the "
       "warm family version; a flat «дело не в этом» can sound sharp.",
       "no I not about that", "casual", "b1", ["Да я не про то, я про то, как ты это сказал."]),
    Ch("ne-fakt", "disagree", "not necessarily", "необязательно / не факт",
       "«необязательно» is the gentle version. A flat «не факт» to someone stating "
       "something confidently can land as dismissive — save it for peers.",
       "not-necessarily / not a fact", "casual", "b1", ["— Он согласится. — Ну, необязательно."]),

    # ---- connecting ----
    Ch("vo-pervyh", "connect", "firstly… secondly…", "во-первых… во-вторых…",
       "For when you're genuinely laying out reasons. In relaxed chat it can sound "
       "like you're presenting — most of the time, just start talking.",
       "in-first… in-second", "neutral", "b2", []),
    Ch("kstati", "connect", "by the way / that reminds me", "кстати, …",
       "Drops in a related aside.",
       "by-the-way", "casual", "a2", ["Кстати, ты не видел мои ключи?"]),
    Ch("k-tomu-zhe", "connect", "on top of that / what's more", "к тому же …",
       "Adds a reinforcing point.",
       "to that EMPH", "neutral", "b1", ["Дорого, и к тому же далеко."]),
    Ch("i-vsyo-taki", "connect", "still / even so / and yet", "и всё-таки …",
       "Concede a point, then hold your ground. The spoken version — «тем не менее» "
       "means the same but sounds like a written report read aloud.",
       "and all-the-same", "casual", "b1", ["И всё-таки я бы не рискнул."]),
    Ch("v-lyubom-sluchae", "connect", "in any case / either way", "в любом случае …",
       "Sets the debate aside and moves on.",
       "in any case", "neutral", "b1", ["В любом случае, спасибо, что попробовал."]),
    Ch("to-est", "connect", "I mean / that is / in other words", "то есть …",
       "Reformulates what you just said.",
       "that is", "casual", "a2", ["Он опоздал, то есть вообще не пришёл."]),
    Ch("poluchaetsya", "connect", "so it turns out / so basically", "получается, (что) …",
       "Draws a conclusion from what's been said.",
       "it-comes-out that", "casual", "b1", ["Получается, мы зря приехали."]),
    Ch("koroche-govorya", "connect", "long story short / in short", "короче говоря, …",
       "Signals a summary is coming.",
       "shorter speaking", "casual", "b1", ["Короче говоря, я отказался."]),
    Ch("po-bolshomu-schetu", "connect", "when it comes down to it / really", "по большому счёту, …",
       "Gets past the details to what actually matters. Warmer in conversation "
       "than «по сути», which can sound a bit corporate.",
       "on the big account", "casual", "b2", ["По большому счёту, ничего не изменилось."]),

    # ---- telling a story ----
    Ch("i-vot", "narrate", "and so / so anyway", "и вот …",
       "Pushes a story to its next beat.",
       "and here", "casual", "a2", ["И вот, стою я на остановке…"]),
    Ch("v-obshchem", "narrate", "anyway / so basically", "в общем, …",
       "Skips ahead or wraps up a digression.",
       "in general", "casual", "a2", ["В общем, мы так и не поехали."]),
    Ch("i-tut", "narrate", "and then suddenly / and at that point", "и тут …",
       "Marks the turning point of a story.",
       "and here [suddenly]", "casual", "b1", ["И тут звонит телефон."]),
    Ch("delo-bylo-tak", "narrate", "here's what happened", "дело было так: …",
       "Opens an anecdote.",
       "the matter was so", "casual", "b1", []),
    Ch("ya-kak-raz", "narrate", "I was just (doing X) when…", "я как раз …",
       "Sets the scene that an event interrupts.",
       "I just-exactly", "casual", "b1", ["Я как раз выходил, когда он позвонил."]),
    Ch("okazalos", "narrate", "turned out (that)…", "оказалось, что …",
       "Delivers the reveal or the twist.",
       "it-turned-out that", "casual", "b1", ["Оказалось, что я перепутал день."]),
    Ch("v-itoge", "narrate", "in the end / so eventually", "в итоге …",
       "Delivers the outcome of the story.",
       "in the upshot", "casual", "b1", ["В итоге пришлось идти пешком."]),
    Ch("s-teh-por", "narrate", "ever since then", "с тех пор …",
       "Links a past event to the way things are now.",
       "from those times", "casual", "b1", ["С тех пор я туда не хожу."]),

    # ---- hypotheticals ----
    Ch("esli-by-ya-znal", "hypo", "if only I'd known / had I known",
       "если бы я знал(а), …",
       "Counterfactual regret about the past. «знала» if you're female.",
       "if would I knew", "casual", "b1", ["Если бы я знал, я бы не соглашался."]),
    Ch("na-tvoem-meste", "hypo", "if I were you / in your shoes",
       "на твоём месте я бы …",
       "Gives advice through a hypothetical. «на вашем месте» for a grandparent "
       "you «вы».",
       "on your place I would", "casual", "b1", ["На твоём месте я бы подождал."]),
    Ch("dopustim", "hypo", "let's say / suppose", "допустим, (что) …",
       "Sets up a hypothetical for the sake of argument.",
       "let's-suppose that", "neutral", "b1", ["Допустим, он прав. Что тогда?"]),
    Ch("predstav", "hypo", "imagine / picture this", "представь, что …",
       "Invites the listener into a scenario. «представьте» on «вы».",
       "imagine that", "casual", "b1", ["Представь, что тебе никто не верит."]),
    Ch("chto-esli", "hypo", "what if…", "что если …",
       "Floats a possibility or a proposal.",
       "what if", "casual", "a2", ["Что если мы просто позвоним?"]),
    Ch("davay-tak", "hypo", "okay, here's what we'll do", "давай так: …",
       "Proposes a concrete plan and gently takes the lead. Friendly, not bossy. "
       "«давайте так» on «вы» / with a group.",
       "let's [do it] so", "casual", "b1", ["Давай так: я готовлю, ты за напитки."]),
    Ch("v-ideale", "hypo", "ideally", "в идеале …",
       "Fine for plans ('ideally we'd leave by six'). Don't lean on it — used a "
       "lot it starts to sound corporate.",
       "in the ideal", "casual", "b1", ["В идеале выехать бы часов в шесть."]),

    # ---- taking a stance ----
    Ch("lichno-ya", "opinion", "personally, I think…", "лично я думаю, что …",
       "Marks the view as your own and leaves room to disagree. «считаю» instead "
       "of «думаю» sounds a bit firmer, more debate-like.",
       "personally I think that", "casual", "b1", []),
    Ch("mne-vse-ravno", "opinion", "I don't mind — whatever's easiest for you",
       "мне всё равно, как тебе удобнее",
       "The warm 'you pick' version. Bare «мне всё равно» alone can sound cold to "
       "family — «как тебе удобнее» keeps it kind. Only for genuinely low-stakes "
       "choices, never about something they care about.",
       "to-me all-the-same, as to-you more-convenient", "casual", "b1", []),
    Ch("ne-v-vostorge", "opinion", "I'm not thrilled about it / not a fan",
       "я не в восторге от …",
       "The polite way to be negative — understated, no drama.",
       "I not in delight from", "casual", "b1", ["Я не в восторге от этого плана."]),
    Ch("ne-tak-vazhno", "opinion", "it's not a big deal to me",
       "для меня это не так важно",
       "The neutral 'I'm not fussed'. «меня это не особо волнует» exists but sounds "
       "more dismissive — save it for things you truly don't care about.",
       "for me this not so important", "casual", "b1", []),
    Ch("obeimi-rukami-za", "opinion", "I'm all for it", "я обеими руками за",
       "Enthusiastic support.",
       "I with both hands for", "casual", "b1", ["Переезд? Я обеими руками за."]),
    Ch("ne-moe", "opinion", "not my thing", "это не моё",
       "Personal-taste rejection, no judgement implied.",
       "this not mine", "casual", "a2", ["Опера — это не моё, если честно."]),
    Ch("kak-po-mne", "opinion", "if you ask me / the way I see it", "как по мне, …",
       "Casual stance marker.",
       "as for me", "casual", "b1", ["Как по мне, и так сойдёт."]),

    # ---- clarifying ----
    Ch("ty-hochesh-skazat", "clarify", "so you're saying (that)…",
       "то есть ты хочешь сказать, что …",
       "Checks your understanding of what they said.",
       "that is you want to-say that", "casual", "b1", []),
    Ch("ya-imeyu-v-vidu", "clarify", "what I mean is / I mean",
       "я имею в виду, что …",
       "Repairs or sharpens your own point.",
       "I have in view that", "casual", "b1", ["Я имею в виду, что это не срочно."]),
    Ch("drugimi-slovami", "clarify", "in other words", "другими словами, …",
       "Restates the point more plainly.",
       "with other words", "neutral", "b1", []),
    Ch("ne-v-tom-smysle", "clarify", "not in that sense / that's not what I mean",
       "не в том смысле",
       "Blocks a likely misreading.",
       "not in that sense", "casual", "b1", ["Дорогой, но не в том смысле."]),
    Ch("kak-tebe-obyasnit", "clarify", "how do I explain this…",
       "как бы тебе объяснить…",
       "Time-buyer specific to explaining something hard. «как вам объяснить» on «вы».",
       "how as-if to-you to-explain", "casual", "b1", []),
    Ch("ya-ne-eto-imel-v-vidu", "clarify", "that's not what I meant",
       "я не это имел в виду",
       "Corrects a misunderstanding of you. «имела» if you're female.",
       "I not this had in view", "casual", "b1", []),
    Ch("v-smysle", "clarify", "meaning? / in what sense?", "в смысле?",
       "One-word request for clarification.",
       "in the sense", "casual", "a2", ["— Он ушёл. — В смысле, совсем?"]),

    # ---- wrapping up ----
    Ch("kak-to-tak", "close", "so yeah, that's about it", "в общем, как-то так",
       "The standard way to hand the turn back after an answer.",
       "in general, somehow so", "casual", "b1", []),
    Ch("vot-takie-dela", "close", "so that's the situation", "вот такие дела",
       "Closes an update, often with a small sigh.",
       "here such affairs", "casual", "b1", []),
    Ch("na-etom-vse", "close", "and that's about it", "ну вот и всё, собственно",
       "Wraps up a rundown. Bare «на этом всё» is a bit brisk for a chatty "
       "conversation — the «ну вот… собственно» softens it.",
       "well here and all, actually", "casual", "b1", ["Ну вот и всё, собственно."]),
    Ch("nu-vot-kak-to-tak", "close", "well, something like that",
       "ну вот, как-то так",
       "Trails off an answer you're not fully sure of.",
       "well here, somehow so", "casual", "b1", []),
    Ch("esli-korotko", "close", "in a nutshell / to keep it short",
       "если коротко, то …",
       "Prefaces a one-line summary.",
       "if shortly, then", "casual", "b1", ["Если коротко, то мы не договорились."]),
    Ch("nu-ty-ponyal", "close", "you know what I mean", "ну, ты понял, о чём я",
       "Wraps up when the rest is obvious — warmer than a bare «думаю, ты понял», "
       "which can sound like you can't be bothered to finish. «вы поняли» on «вы».",
       "well, you understood what about I", "casual", "b1", []),

    # ---- making plans ----
    Ch("davay", "plan", "let's… / how about we…", "давай …",
       "The workhorse for proposing anything. «давайте» on «вы» or with a group.",
       "let's", "casual", "a2", ["Давай в субботу к твоим родителям съездим."]),
    Ch("mozhet", "plan", "maybe we…? / what if we…?", "может, …?",
       "Floats a plan tentatively — easy for either of you to walk back.",
       "maybe", "casual", "a2", ["Может, в кино сходим вечером?"]),
    Ch("ya-tut-podumal", "plan", "so I was thinking…", "я тут подумал, …",
       "Eases into a suggestion or a favour. You're male, so «подумал».",
       "I here thought", "casual", "b1",
       ["Я тут подумал, может, на выходные рванём на дачу."]),
    Ch("kak-naschet", "plan", "how about…?", "как насчёт …?",
       "Proposes one specific option.",
       "how regarding", "casual", "b1", ["Как насчёт воскресенья, часов в двенадцать?"]),
    Ch("ty-vo-skolko", "plan", "what time are you…?", "ты во сколько …?",
       "Coordinates timing. «вы во сколько» on «вы».",
       "you at what-time", "casual", "a2", ["Ты во сколько завтра освободишься?"]),
    Ch("davay-sozvonimsya", "plan", "let's talk / text later", "давай ближе к делу созвонимся",
       "Punts the details to a later call or message.",
       "let's closer to the-matter call-each-other", "casual", "b1", []),
    Ch("dogovorilis", "plan", "deal / sounds good / it's settled", "договорились",
       "Closes a plan — warm, final, friendly.",
       "agreed [we]", "casual", "a2", ["— В семь у входа. — Договорились."]),
    Ch("esli-nichego-ne-pomenyaetsya", "plan", "all being well / if nothing changes",
       "если ничего не поменяется, …",
       "Hedges a plan that's likely but not locked in.",
       "if nothing not changes", "casual", "b1",
       ["В пятницу приеду, если ничего не поменяется."]),
    Ch("zaskochu", "plan", "I'll pop by / swing by", "заскочу на минутку",
       "Casual 'I'll come by briefly'.",
       "I'll-drop-in for a minute", "casual", "b1", ["Я вечером заскочу, занесу книги."]),

    # ---- catching up ----
    Ch("nu-kak-ty", "smalltalk", "so how are you? / how've you been?", "ну, как ты?",
       "The standard warm opener when you reconnect. «как вы?» on «вы».",
       "well, how you", "casual", "a2", ["Ну, как ты? Сто лет не виделись."]),
    Ch("chto-novogo", "smalltalk", "what's new? / anything new?", "что нового?",
       "Invites an update without any pressure.",
       "what of-new", "casual", "a2", ["Рассказывай, что нового."]),
    Ch("sto-let-ne-videlis", "smalltalk", "long time no see / it's been ages",
       "сто лет не виделись",
       "Warm greeting after a real gap.",
       "hundred years not seen-each-other", "casual", "b1",
       ["Сто лет не виделись! Ты как вообще?"]),
    Ch("kak-sam", "smalltalk", "and how about you? / and yourself?", "ну а ты как сам?",
       "Turns a 'how are you' back on the other person. «как вы сами?» on «вы».",
       "well and you how self", "casual", "b1",
       ["У меня всё по-старому. Ну а ты как сам?"]),
    Ch("kak-tam", "smalltalk", "how's … doing? / how are things with …?", "как там …?",
       "Asks after someone or something (родители, работа, кот).",
       "how there", "casual", "a2", ["Как там родители, всё нормально?"]),
    Ch("nu-rasskazyvay", "smalltalk", "so, tell me everything / go on then",
       "ну, рассказывай",
       "Warmly hands the floor to the other person.",
       "well, tell", "casual", "a2", ["Ну, рассказывай, как съездил."]),
    Ch("chem-zanimaeshsya", "smalltalk", "what are you up to these days?",
       "чем сейчас занимаешься?",
       "Catches up on someone's life. «чем занимаетесь?» on «вы».",
       "with-what now you-occupy-yourself", "casual", "b1", []),
    Ch("davno-ne-slyshal", "smalltalk", "haven't heard from you in a while",
       "давно тебя не слышал",
       "A gentle nudge that you've been out of touch. You're male: «слышал».",
       "long-ago you not heard", "casual", "b1",
       ["Давно тебя не слышал, всё в порядке?"]),
    Ch("kak-zhizn", "smalltalk", "how's life? / how's it going?", "ну как жизнь?",
       "Casual, all-purpose — a touch more informal than «как дела».",
       "well how life", "casual", "a2", ["Здорово! Как жизнь-то?"]),

    # ---- asking & offering ----
    Ch("mozhesh", "ask", "can you…? / could you…?", "можешь …?",
       "The plain everyday request — softer than an imperative. «можете» on «вы».",
       "you-can", "casual", "a2", ["Можешь скинуть мне адрес?"]),
    Ch("ne-mog-by-ty", "ask", "would you mind…? / could you possibly…?",
       "не мог бы ты …?",
       "More polite, or for a bigger favour. «не могли бы вы» on «вы».",
       "not could would you", "casual", "b1",
       ["Не мог бы ты завтра меня подбросить до вокзала?"]),
    Ch("esli-neslozhno", "ask", "if it's not too much trouble", "если несложно, …",
       "Adds a layer of politeness to a request.",
       "if not-difficult", "casual", "b1", ["Купи хлеба, если несложно."]),
    Ch("mozhno-poprosit", "ask", "hey, can I ask you a favour?",
       "слушай, можно тебя попросить?",
       "Flags a favour is coming and gives them room to say no. «можно вас "
       "попросить» on «вы».",
       "listen, may you to-ask", "casual", "b1", []),
    Ch("tebe-pomoch", "ask", "want a hand? / can I help?", "тебе помочь?",
       "Offers help directly. «вам помочь?» on «вы».",
       "to-you to-help", "casual", "a2", ["Тяжёлые сумки же — тебе помочь?"]),
    Ch("davay-ya", "ask", "let me (do it) / I've got this", "давай я …",
       "Offers to take a task off someone.",
       "let's I", "casual", "b1", ["Давай я вымою посуду, ты посиди."]),
    Ch("mogu-podvezti", "ask", "I can give you a ride", "я могу тебя подвезти",
       "Offers a lift. «я вас подвезу» on «вы».",
       "I can you to-give-a-lift", "casual", "b1", ["Тебе в центр? Могу подвезти."]),
    Ch("budesh-chay", "ask", "want some tea? / can I get you anything?", "будешь чай?",
       "The offer you make to anyone who comes over. «будете чай?» on «вы».",
       "you-will-have tea", "casual", "a2", ["Проходи, разувайся. Будешь чай?"]),
    Ch("ne-podskazhesh", "ask", "do you happen to know…?", "не подскажешь, …?",
       "Softened way to ask for information. «не подскажете» on «вы».",
       "not you-prompt", "casual", "b1",
       ["Не подскажешь, во сколько они закрываются?"]),

    # ---- warmth & support ----
    Ch("ne-perezhivay", "warmth", "don't worry about it / it'll be fine",
       "да не переживай ты так",
       "Reassures someone who's stressing. «не переживайте» on «вы».",
       "yes not worry you so", "casual", "a2",
       ["Да не переживай ты так, всё решится."]),
    Ch("rad-za-tebya", "warmth", "I'm really happy for you", "я очень рад за тебя",
       "Warm congratulations. You're male: «рад». «за вас» on «вы».",
       "I very glad for you", "casual", "a2",
       ["Слышал про новую работу — очень рад за тебя."]),
    Ch("derzhis", "warmth", "hang in there / stay strong", "держись",
       "Support during a hard patch.",
       "hold-on", "casual", "b1", ["Знаю, что непросто сейчас. Держись."]),
    Ch("skuchayu", "warmth", "I miss you", "я по тебе скучаю",
       "Direct and warm. «скучаю по вам» on «вы».",
       "I along you miss", "casual", "a2",
       ["Приезжай уже, я по тебе скучаю."]),
    Ch("rad-videt", "warmth", "good to see you", "рад тебя видеть",
       "The warm greeting when someone arrives. You're male: «рад». «рад вас "
       "видеть» on «вы».",
       "glad you to-see", "casual", "a2", ["О, привет! Рад тебя видеть."]),
    Ch("ty-molodec", "warmth", "well done / good for you", "ты молодец",
       "Genuine praise — works for anyone, any age.",
       "you well-done", "casual", "a2", ["Сам всё починил? Молодец."]),
    Ch("vsyo-budet-horosho", "warmth", "it's going to be okay", "всё будет хорошо",
       "The plain reassurance. Simple, and it means a lot.",
       "everything will-be good", "casual", "a2",
       ["Не накручивай себя. Всё будет хорошо."]),
    Ch("esli-chto-na-svyazi", "warmth", "I'm here if you need anything",
       "если что — я на связи",
       "Offers ongoing support without hovering.",
       "if something — I on connection", "casual", "b1",
       ["Ну ладно, поеду. Если что — я на связи."]),
    Ch("beregi-sebya", "warmth", "take care of yourself", "береги себя",
       "Warm sign-off, especially to older relatives. «берегите себя» on «вы».",
       "guard yourself", "casual", "a2", ["Ну всё, пока. Береги себя."]),

    # ---- thanks & sorry ----
    Ch("izvini-chto-otvlekayu", "social", "sorry to bother you, …",
       "извини, что отвлекаю, …",
       "Opens an interruption or a message politely. «извините, что отвлекаю» on «вы».",
       "sorry that I-distract", "casual", "b1",
       ["Извини, что отвлекаю, ты сейчас не занят?"]),
    Ch("spasibo-chto-vyruchil", "social", "thanks for bailing me out",
       "спасибо, что выручил",
       "Warm thanks for a real favour. You're thanking someone male here; «что "
       "выручила» if she's female; «что выручили» on «вы».",
       "thanks that [you] helped-out", "casual", "b1",
       ["Спасибо, что выручил вчера, я твой должник."]),
    Ch("da-ne-za-chto", "social", "don't mention it / no problem",
       "да не за что",
       "Waves off someone's thanks, warmly.",
       "yes not for what", "casual", "a2",
       ["— Спасибо огромное! — Да не за что."]),
    Ch("prosti-ya-zabyl", "social", "sorry, it totally slipped my mind",
       "прости, я совсем забыл",
       "Owns a small mistake without over-apologising. You're male: «забыл».",
       "sorry, I completely forgot", "casual", "a2",
       ["Прости, я совсем забыл тебе перезвонить."]),
    Ch("nichego-byvaet", "social", "it's fine, these things happen",
       "да ничего, бывает",
       "Forgives a small slip and keeps it light.",
       "yes nothing, [it] happens", "casual", "a2",
       ["— Опять опоздал, извини. — Да ничего, бывает."]),
    Ch("s-menya-prichitaetsya", "social", "I owe you one", "с меня причитается",
       "Light, friendly way to acknowledge a favour.",
       "from me [it] is-owed", "casual", "b2",
       ["Ну всё, с меня причитается. Куда сходим?"]),
    Ch("ne-beri-v-golovu", "social", "don't take it to heart / let it go",
       "не бери в голову",
       "Tells someone to stop dwelling on something. «не берите в голову» on «вы».",
       "not take into head", "casual", "b1",
       ["Он не со зла это сказал. Не бери в голову."]),
    Ch("da-perestan", "social", "oh stop it / come on now", "да перестань, ну что ты",
       "Waves off praise or an apology, warmly and a bit playfully.",
       "yes stop, well what you", "casual", "b1",
       ["— Ты нас так выручил. — Да перестань, ерунда."]),
    Ch("spasibo-za-vsyo", "social", "thank you for everything", "спасибо тебе за всё",
       "Heartfelt, for a bigger moment — leaving, a holiday, a hard time. «спасибо "
       "вам за всё» on «вы».",
       "thanks to-you for everything", "casual", "b1",
       ["Спасибо тебе за всё, правда. Было здорово."]),
]
_CHUNK = {c.id: c for c in CHUNKS}
assert len(_CHUNK) == len(CHUNKS), "duplicate chunk id"

# ---------------------------------------------------------------- scaffold stages

# Each review renders at the current stage for that chunk; a run of correct
# answers advances the stage (less English help), a miss steps it back.
STAGES = [
    ("literal", "full English line, chunk shown, with a word-for-word gloss"),
    ("plain",   "full English line, chunk marked"),
    ("recall",  "just the situation in a few words — say the whole Russian line"),
    ("cloze",   "the Russian line with the chunk blanked — supply the chunk"),
]
_MAX_STAGE = len(STAGES) - 1
_ADVANCE_STREAK = 2      # consecutive rights at a stage before it advances

# Learning order: the "conversational glue" comes first — highest payoff, lowest
# social risk. Stance / pushback carry more risk, so they surface once the glue is
# solid. A function's weight is scaled by this until you've seen enough of it.
_FN_PRIORITY = {
    "react": 3, "time": 3, "open": 3, "hedge": 3, "agree": 3,    # tier 1 — glue
    "smalltalk": 3, "warmth": 3, "social": 3,                    # tier 1 — social glue
    "narrate": 2, "connect": 2, "clarify": 2, "close": 2,        # tier 2 — structure
    "plan": 2, "ask": 2,                                         # tier 2 — getting things done
    "hypo": 1, "disagree": 1, "opinion": 1,                      # tier 3 — stance
}
_FN_PRIORITY_UNTIL = 6   # once you've been served this many of a function, priority stops mattering

_BATCH = 12
_MIN_BUFFER = 12
_TARGET_BUFFER = 30
_RETEST_GAP = (3, 5)
_RETEST_BATCH = 3
_PARKED = 1 << 30
_WINDOW = 24
_LEARN_MIN = 4
_LEARN_PCT = 0.8
_PRACTISE_MIN = 3
_FN_CAP = 2             # a single function may appear at most this many times per batch

_gen_lock = threading.Lock()
_retest_lock = threading.Lock()


def _c():
    return store.connect()


def _start_stage():
    """Where a brand-new chunk starts. Default 0 (full English + a word-for-word
    gloss of the chunk) — cold production is hard even with good comprehension;
    the gloss disappears after two correct answers. Raise it in the stats tab."""
    try:
        return max(0, min(_MAX_STAGE, int(srs.get_setting("chunk_start_stage", 0))))
    except (TypeError, ValueError):
        return 0


def _pos():
    try:
        return int(srs.get_setting("chunk_pos", 0))
    except (TypeError, ValueError):
        return 0


def _bump_pos(by=1):
    srs.set_setting("chunk_pos", _pos() + by)


# ---------------------------------------------------------------- per-chunk state

def _stage_row(c, chunk_id):
    r = c.execute("SELECT stage, streak, seen, hits FROM chunk_stage WHERE chunk_id=?",
                  (chunk_id,)).fetchone()
    if r:
        return dict(r)
    return {"stage": _start_stage(), "streak": 0, "seen": 0, "hits": 0}


def chunk_stage(chunk_id):
    c = _c()
    s = _stage_row(c, chunk_id)
    c.close()
    return s


def _apply_grade(chunk_id, verdict):
    """Advance / retreat the scaffold stage for a chunk after a fresh grade."""
    c = _c()
    s = _stage_row(c, chunk_id)
    stage, streak = s["stage"], s["streak"]
    if verdict == "right":
        streak += 1
        if streak >= _ADVANCE_STREAK and stage < _MAX_STAGE:
            stage += 1
            streak = 0
    else:
        streak = 0
        stage = max(0, stage - 1)
    c.execute(
        """INSERT INTO chunk_stage(chunk_id, stage, streak, seen, hits, updated_at)
           VALUES(?,?,?,1,?, datetime('now'))
           ON CONFLICT(chunk_id) DO UPDATE SET
             stage=excluded.stage, streak=excluded.streak,
             seen=chunk_stage.seen+1, hits=chunk_stage.hits+?,
             updated_at=datetime('now')""",
        (chunk_id, stage, streak, 1 if verdict == "right" else 0,
         1 if verdict == "right" else 0))
    c.commit()
    c.close()
    return stage


# ---------------------------------------------------------------- selection

def _chunk_stats_map():
    """{chunk_id: {seen, hits}} from the scaffold table (lifetime, not windowed —
    the window logic is in slice_stats)."""
    c = _c()
    rows = c.execute("SELECT chunk_id, seen, hits, stage FROM chunk_stage").fetchall()
    c.close()
    return {r["chunk_id"]: dict(r) for r in rows}


def pick_chunks(n):
    stats = _chunk_stats_map()
    # how many cards of each function you've actually graded so far
    fn_seen = {f: 0 for f in _FN_IDS}
    c = _c()
    for r in c.execute("SELECT fn, COUNT(*) n FROM chunk_items "
                       "WHERE fn IS NOT NULL AND verdict IS NOT NULL GROUP BY fn").fetchall():
        fn_seen[r["fn"]] = r["n"]
    c.close()
    out, seen_ids, fn_count = [], set(), {}
    order = CHUNKS[:]
    random.shuffle(order)
    tries = 0
    while len(out) < n and tries < n * 20:
        tries += 1
        ch = random.choice(order)
        if ch.id in seen_ids:
            continue
        if fn_count.get(ch.fn, 0) >= _FN_CAP:
            continue
        st = stats.get(ch.id)
        if not st:
            w = 1.7                                   # unseen — prioritise
        else:
            seen, hits = st["seen"], st["hits"]
            acc = hits / seen if seen else 0.0
            if seen >= _LEARN_MIN and acc >= _LEARN_PCT and st["stage"] >= _MAX_STAGE:
                w = 0.12                              # mastered at the hardest stage
            elif seen >= _PRACTISE_MIN and acc < 0.6:
                w = 2.4                               # shaky
            else:
                w = 1.0 + (0.15 * (_MAX_STAGE - st["stage"]))   # nudge low-stage up
        # learning order: bias toward the safe "glue" functions until you've done
        # a fair number of each; then priority washes out and it's all in rotation
        if fn_seen.get(ch.fn, 0) < _FN_PRIORITY_UNTIL:
            w *= _FN_PRIORITY.get(ch.fn, 1) / 2.0
        if random.random() > w / 2.4:
            continue
        seen_ids.add(ch.id)
        fn_count[ch.fn] = fn_count.get(ch.fn, 0) + 1
        out.append(ch.spec())
    return out


# ---------------------------------------------------------------- generation

_CYR = re.compile(r"[а-яёА-ЯЁ]")
_LAT_WORD = re.compile(r"[A-Za-z]{2,}")


def _find_span(ru, chunk_ru, fallback):
    """The chunk substring must sit verbatim inside `ru` for the cloze stage to
    work; fall back to the catalogue form. Returns "" if neither is in `ru`."""
    ru_l = (ru or "").lower()
    for cand in (chunk_ru, fallback):
        cand = (cand or "").strip().replace("́", "")
        if cand and cand.lower() in ru_l:
            i = ru_l.index(cand.lower())
            return ru[i:i + len(cand)]
    return ""


def _row(card, known=None):
    en = (card.get("en") or "").strip()
    ru = (card.get("ru") or "").strip().replace("́", "")
    ch = _CHUNK.get((known or {}).get("id")) or _CHUNK.get(card.get("chunk_id"))
    if not ch or not en or not ru:
        return {"ok": False}
    # the two fields must stay in their own language — the model sometimes
    # code-switches the Russian chunk into the English sentence (or vice versa)
    if _CYR.search(en) or len(_LAT_WORD.findall(ru)) >= 2:
        return {"ok": False}
    chunk_ru = _find_span(ru, card.get("chunk_ru"), ch.ru)
    if not chunk_ru:                       # can't blank it for the cloze stage → drop
        return {"ok": False}
    # keep chunk_en only if it's actually a slice of `en`; else no highlight
    chunk_en = (card.get("chunk_en") or "").strip()
    if not chunk_en or chunk_en.lower() not in en.lower():
        chunk_en = None
    gist = (card.get("gist") or "").strip()
    if _CYR.search(gist):
        gist = ""
    return {
        "ok": True,
        "chunk_id": ch.id, "fn": ch.fn,
        "en": en, "ru": ru,
        "chunk_en": chunk_en, "chunk_ru": chunk_ru,
        "gist": gist or None,
        "gloss": (card.get("gloss") or ch.lit or "").strip() or None,
        "note": (card.get("note") or ch.note or "").strip() or None,
        "target": json.dumps([chunk_ru], ensure_ascii=False),
    }


def _insert(cards, *, retest_for=None, focus=0, specs=None):
    c = _c()
    n = 0
    cards = cards or []
    match = specs if specs and len(specs) == len(cards) else None
    for i, card in enumerate(cards):
        f = _row(card, match[i] if match else None)
        if not f["ok"]:
            continue
        c.execute(
            """INSERT INTO chunk_items(chunk_id, fn, en, ru, chunk_en, chunk_ru,
                                       gist, gloss, note, target, retest_for, focus)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f["chunk_id"], f["fn"], f["en"], f["ru"], f["chunk_en"], f["chunk_ru"],
             f["gist"], f["gloss"], f["note"], f["target"], retest_for, focus))
        n += 1
    c.commit()
    c.close()
    return n


def _generate_batch():
    specs = pick_chunks(_BATCH)
    if not specs:
        return 0
    try:
        out = llm.chunk_cards(specs)
    except Exception as e:  # noqa: BLE001
        print(f"[chunks] generation failed: {e}", flush=True)
        return 0
    return _insert(out.get("cards"), specs=specs)


def _servable_count(c):
    return c.execute(
        "SELECT COUNT(*) n FROM chunk_items WHERE verdict IS NULL AND retest_for IS NULL "
        "AND COALESCE(focus,0)=0 "
        "AND (served_at IS NULL OR served_at < datetime('now','-30 minutes'))").fetchone()["n"]


def _topup(force=False):
    if not _gen_lock.acquire(blocking=False):
        return
    try:
        c = _c()
        have = _servable_count(c)
        c.close()
        rounds = 0
        while (force or have < _TARGET_BUFFER) and rounds < 3:
            made = _generate_batch()
            if not made:
                break
            have += made
            rounds += 1
            force = False
    finally:
        _gen_lock.release()


def topup_async(force=False):
    threading.Thread(target=_topup, kwargs={"force": force}, daemon=True).start()


# ---------------------------------------------------------------- lapses / re-tests

def _lapse_miss(chunk_id, item_id):
    if not chunk_id:
        return
    due = _pos() + random.randint(*_RETEST_GAP)
    c = _c()
    c.execute("INSERT OR IGNORE INTO chunk_lapse(chunk_id) VALUES(?)", (chunk_id,))
    c.execute("UPDATE chunk_lapse SET misses = misses + 1, resolved = 0, due_pos = ?, "
              "src_item_id = COALESCE(src_item_id, ?) WHERE chunk_id = ?",
              (due, item_id, chunk_id))
    row = c.execute("SELECT id, misses FROM chunk_lapse WHERE chunk_id = ?",
                    (chunk_id,)).fetchone()
    c.commit()
    c.close()
    if row and row["misses"] >= 2:
        _gen_retest_async(row["id"])


def _lapse_clear(chunk_id):
    if not chunk_id:
        return
    c = _c()
    c.execute("UPDATE chunk_lapse SET resolved = 1 WHERE resolved = 0 AND chunk_id = ?",
              (chunk_id,))
    c.execute("DELETE FROM chunk_items WHERE retest_for IN "
              "(SELECT id FROM chunk_lapse WHERE resolved = 1) AND verdict IS NULL")
    c.commit()
    c.close()


def _retest_graded(lapse_id, verdict):
    c = _c()
    lap = c.execute("SELECT * FROM chunk_lapse WHERE id = ?", (lapse_id,)).fetchone()
    if not lap:
        c.close()
        return
    if verdict == "right":
        c.execute("UPDATE chunk_lapse SET resolved = 1 WHERE id = ?", (lapse_id,))
        c.execute("DELETE FROM chunk_items WHERE retest_for = ? AND verdict IS NULL",
                  (lapse_id,))
        c.commit()
        c.close()
        return
    due = _pos() + random.randint(*_RETEST_GAP)
    c.execute("UPDATE chunk_lapse SET misses = misses + 1, due_pos = ? WHERE id = ?",
              (due, lapse_id))
    c.commit()
    c.close()
    _gen_retest_async(lapse_id)


def _gen_retest(lapse_id):
    c = _c()
    lap = c.execute("SELECT * FROM chunk_lapse WHERE id = ? AND resolved = 0",
                    (lapse_id,)).fetchone()
    if not lap:
        c.close()
        return 0
    have = c.execute("SELECT COUNT(*) n FROM chunk_items WHERE retest_for = ? AND verdict IS NULL",
                     (lapse_id,)).fetchone()["n"]
    chunk_id = lap["chunk_id"]
    c.close()
    ch = _CHUNK.get(chunk_id)
    if have >= _RETEST_BATCH or not ch:
        return 0
    try:
        out = llm.chunk_focus_cards(ch.spec(), n=_RETEST_BATCH)
    except Exception as e:  # noqa: BLE001
        print(f"[chunks] retest gen failed: {e}", flush=True)
        return 0
    cards = out.get("cards") or []
    return _insert(cards, retest_for=lapse_id, specs=[ch.spec()] * len(cards))


def _gen_retest_async(lapse_id):
    def run():
        if not _retest_lock.acquire(blocking=False):
            return
        try:
            _gen_retest(lapse_id)
        finally:
            _retest_lock.release()
    threading.Thread(target=run, daemon=True).start()


def _due_retests(c, need):
    if need <= 0:
        return []
    pos = _pos()
    laps = c.execute(
        "SELECT * FROM chunk_lapse WHERE resolved = 0 AND due_pos > 0 AND due_pos <= ? "
        "ORDER BY misses DESC, due_pos ASC LIMIT ?", (pos, need)).fetchall()
    out = []
    for lap in laps:
        item = None
        if lap["misses"] >= 2:
            r = c.execute("SELECT * FROM chunk_items WHERE retest_for = ? AND verdict IS NULL "
                          "AND served_at IS NULL ORDER BY id LIMIT 1", (lap["id"],)).fetchone()
            if r:
                c.execute("UPDATE chunk_items SET served_at = datetime('now') WHERE id = ?",
                          (r["id"],))
                item = dict(r)
            else:
                _gen_retest_async(lap["id"])
                continue
        else:
            src = c.execute("SELECT * FROM chunk_items WHERE id = ?",
                            (lap["src_item_id"],)).fetchone()
            if not src:
                c.execute("UPDATE chunk_lapse SET resolved = 1 WHERE id = ?", (lap["id"],))
                continue
            cur = c.execute(
                """INSERT INTO chunk_items(chunk_id, fn, en, ru, chunk_en, chunk_ru,
                                           gist, gloss, note, target, retest_for, served_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?, datetime('now'))""",
                (src["chunk_id"], src["fn"], src["en"], src["ru"], src["chunk_en"],
                 src["chunk_ru"], src["gist"], src["gloss"], src["note"], src["target"],
                 lap["id"]))
            item = dict(c.execute("SELECT * FROM chunk_items WHERE id = ?",
                                  (cur.lastrowid,)).fetchone())
        c.execute("UPDATE chunk_lapse SET due_pos = ? WHERE id = ?", (_PARKED, lap["id"]))
        out.append({**_public(item, c), "retest": True})
    return out


# ---------------------------------------------------------------- serve / grade

def _public(r, c=None):
    ch = _CHUNK.get(r["chunk_id"])
    try:
        target = json.loads(r["target"]) if r.get("target") else [r.get("chunk_ru")]
    except (ValueError, TypeError):
        target = [r.get("chunk_ru")]
    own = c is None
    if own:
        c = _c()
    stage = _stage_row(c, r["chunk_id"])["stage"]
    if own:
        c.close()
    fn = r.get("fn") or (ch.fn if ch else "")
    return {
        "id": r["id"], "chunk_id": r["chunk_id"], "fn": fn,
        "fn_label": _FN.get(fn, ("", fn, ""))[1],
        "en": r["en"], "ru": r["ru"],
        "chunk_en": r["chunk_en"], "chunk_ru": r["chunk_ru"],
        "chunk_gloss": ch.en if ch else None,     # catalogue English of the chunk — recall-stage fallback
        "target": [t for t in target if t],
        "gist": r["gist"], "gloss": r["gloss"], "note": r["note"],
        "register": ch.reg if ch else "",
        "stage": stage, "stage_name": STAGES[stage][0], "max_stage": _MAX_STAGE,
        "retest": bool(r.get("retest_for")), "focus": bool(r.get("focus")),
        "card_id": r["card_id"],
    }


def state():
    c = _c()
    unseen = _servable_count(c)
    seen = c.execute("SELECT COUNT(*) n FROM chunk_items WHERE verdict IS NOT NULL").fetchone()["n"]
    right = c.execute("SELECT COUNT(*) n FROM chunk_items WHERE verdict='right'").fetchone()["n"]
    lapses = c.execute("SELECT COUNT(*) n FROM chunk_lapse WHERE resolved=0").fetchone()["n"]
    mastered = c.execute(
        "SELECT COUNT(*) n FROM chunk_stage WHERE stage>=? AND seen>=? AND hits*1.0/seen>=?",
        (_MAX_STAGE, _LEARN_MIN, _LEARN_PCT)).fetchone()["n"]
    c.close()
    return {"buffer": unseen, "seen": seen, "right": right, "open_lapses": lapses,
            "mastered": mastered, "total": len(CHUNKS), "start_stage": _start_stage()}


def next_items(n=1):
    n = max(1, n)
    c = _c()
    retest_cap = max(1, n // 2)
    if n == 1 and _pos() % 3 != 0:
        retest_cap = 0
    out = _due_retests(c, retest_cap) if retest_cap else []
    rows = []
    if len(out) < n:
        rows = c.execute(
            """SELECT * FROM chunk_items
               WHERE verdict IS NULL AND (
                     (COALESCE(focus,0)=1 AND served_at IS NULL)
                     OR (retest_for IS NULL AND COALESCE(focus,0)=0
                         AND (served_at IS NULL OR served_at < datetime('now','-30 minutes'))))
               ORDER BY COALESCE(focus,0) DESC, served_at IS NULL DESC, id
               LIMIT ?""", (n - len(out),)).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            c.execute(f"UPDATE chunk_items SET served_at=datetime('now') "
                      f"WHERE id IN ({','.join('?' * len(ids))})", ids)
            c.commit()
    left = _servable_count(c)
    payload = out + [_public(dict(r), c) for r in rows]
    c.close()
    if payload:
        _bump_pos(len(payload))
    if left < _MIN_BUFFER:
        topup_async()
    return payload


def grade(item_id, verdict):
    verdict = "right" if verdict == "right" else "wrong"
    c = _c()
    r = c.execute("SELECT verdict, chunk_id, retest_for FROM chunk_items WHERE id=?",
                  (item_id,)).fetchone()
    if not r:
        c.close()
        return None
    fresh = r["verdict"] is None
    chunk_id, retest_for = r["chunk_id"], r["retest_for"]
    stage_now = _stage_row(c, chunk_id)["stage"]
    c.execute("UPDATE chunk_items SET verdict=?, stage=?, graded_at=datetime('now') WHERE id=?",
              (verdict, stage_now, item_id))
    c.commit()
    c.close()
    if not fresh:
        return {**state()}
    if retest_for:
        _retest_graded(retest_for, verdict)
        return {"retest": True, **state()}
    new_stage = _apply_grade(chunk_id, verdict)
    if verdict == "wrong":
        _lapse_miss(chunk_id, item_id)
    else:
        _lapse_clear(chunk_id)
    st = state()
    if new_stage != stage_now:
        st["stage_changed"] = {"chunk_id": chunk_id, "from": stage_now, "to": new_stage,
                               "name": STAGES[new_stage][0]}
    return st


# ---------------------------------------------------------------- "practise this"

def learn(function, n=6):
    """Queue a burst of focus cards for one conversational function."""
    if function not in _FN:
        return 0
    pool = [ch for ch in CHUNKS if ch.fn == function]
    if not pool:
        return 0
    specs = [random.choice(pool).spec() for _ in range(n)]
    try:
        out = llm.chunk_focus_cards(specs, n=n, fn_hint=_FN[function][1])
    except Exception as e:  # noqa: BLE001
        print(f"[chunks] learn gen failed: {e}", flush=True)
        return 0
    return _insert(out.get("cards"), focus=1, specs=specs)


# ---------------------------------------------------------------- stats

def _status(right, wrong):
    n = right + wrong
    pct = round(right / n, 2) if n else 0.0
    if n == 0:
        s = "new"
    elif n < _PRACTISE_MIN:
        s = "seen"
    elif n >= _LEARN_MIN and pct >= _LEARN_PCT:
        s = "learned"
    else:
        s = "practising"
    return {"seen": n, "right": right, "wrong": wrong, "pct": pct, "status": s}


def _windowed_by(col):
    """{key: {right, wrong}} over each key's recent `_WINDOW` graded items."""
    c = _c()
    rows = c.execute(
        f"SELECT {col} k, verdict FROM chunk_items "
        f"WHERE {col} IS NOT NULL AND verdict IS NOT NULL "
        f"ORDER BY graded_at DESC, id DESC").fetchall()
    c.close()
    agg = {}
    for r in rows:
        v = agg.setdefault(r["k"], {"right": 0, "wrong": 0})
        if v["right"] + v["wrong"] >= _WINDOW:
            continue
        v["right" if r["verdict"] == "right" else "wrong"] += 1
    return agg


def slice_stats():
    """Per conversational function: accuracy over the recent window + count."""
    agg = _windowed_by("fn")
    out = []
    for fid, label, help_ in FUNCTIONS:
        a = agg.get(fid, {"right": 0, "wrong": 0})
        out.append({"value": fid, "label": label, "help": help_,
                    "total": sum(1 for ch in CHUNKS if ch.fn == fid),
                    **_status(a["right"], a["wrong"])})
    return out


def catalog():
    """Every chunk with its scaffold stage + lifetime accuracy, worst-first."""
    stats = _chunk_stats_map()
    windowed = _windowed_by("chunk_id")
    out = []
    for ch in CHUNKS:
        st = stats.get(ch.id, {"seen": 0, "hits": 0, "stage": _start_stage()})
        w = windowed.get(ch.id, {"right": 0, "wrong": 0})
        status = _status(w["right"], w["wrong"])["status"]
        if st["seen"] and st["stage"] >= _MAX_STAGE and status == "learned":
            status = "learned"
        out.append({
            "id": ch.id, "fn": ch.fn, "fn_label": _FN[ch.fn][1],
            "en": ch.en, "ru": ch.ru, "note": ch.note, "lit": ch.lit,
            "register": ch.reg, "level": ch.level, "ex": ch.ex,
            "stage": st["stage"], "stage_name": STAGES[st["stage"]][0],
            "seen": st["seen"], "hits": st["hits"], "status": status,
        })
    out.sort(key=lambda x: (x["status"] != "practising", -x["seen"], x["stage"]))
    return out


def overall():
    c = _c()
    row = c.execute("SELECT COUNT(*) n, SUM(verdict='right') r FROM chunk_items "
                    "WHERE verdict IS NOT NULL").fetchone()
    c.close()
    n = row["n"] or 0
    return {"cards": n, "right": row["r"] or 0,
            "pct": round((row["r"] or 0) / n, 2) if n else 0.0}


def full_stats():
    c = _c()
    hist = {name: 0 for name, _ in STAGES}
    for r in c.execute("SELECT stage, COUNT(*) n FROM chunk_stage GROUP BY stage").fetchall():
        if 0 <= r["stage"] <= _MAX_STAGE:
            hist[STAGES[r["stage"]][0]] = r["n"]
    c.close()
    st = state()
    return {
        "overall": overall(),
        "functions": slice_stats(),
        "chunks": catalog(),
        "stages": [{"name": n, "help": h} for n, h in STAGES],
        "stage_hist": hist,
        "start_stage": _start_stage(),
        "mastered": st["mastered"], "total": st["total"],
        "open_lapses": st["open_lapses"],
    }


# ---------------------------------------------------------------- save as card

def save_as_card(item_id):
    c = _c()
    r = c.execute("SELECT * FROM chunk_items WHERE id=?", (item_id,)).fetchone()
    if not r:
        c.close()
        return None
    r = dict(r)
    c.close()
    if r["card_id"]:
        return srs.get_card(r["card_id"])
    p = _public(r)
    front = p["en"]
    meta = {"kind": "chunk", "chunk_id": p["chunk_id"], "fn": p["fn"],
            "target": p["target"], "gloss": p["gloss"], "chunk_ru": p["chunk_ru"]}
    card = srs.create_production_card(front, r["ru"], note=r["note"],
                                     speak_ref=f"chunk:{item_id}", meta=meta)
    c = _c()
    c.execute("UPDATE chunk_items SET card_id=? WHERE id=?", (card["id"], item_id))
    c.commit()
    c.close()
    return card


def session_suggest(item_ids, limit=8):
    ids = [int(x) for x in item_ids if str(x).strip().lstrip("-").isdigit()]
    if not ids:
        return []
    c = _c()
    rows = c.execute(
        f"SELECT * FROM chunk_items WHERE id IN ({','.join('?' * len(ids))}) AND verdict='wrong'",
        ids).fetchall()
    misses = {l["chunk_id"]: l["misses"]
              for l in c.execute("SELECT chunk_id, misses FROM chunk_lapse").fetchall()}
    best = {}
    for r in rows:
        p = _public(dict(r), c)
        key = r["chunk_id"]
        m = misses.get(key, 1)
        if key not in best or m > best[key]["misses"]:
            best[key] = {**p, "misses": m, "already_card": bool(r["card_id"])}
    c.close()
    return sorted(best.values(), key=lambda x: -x["misses"])[:max(1, limit)]
