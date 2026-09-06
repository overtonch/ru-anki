"""Russian grammar catalog + progress tracking for the forms drill.

`CONCEPTS` is a curated, CEFR-tagged list of the grammar patterns / prepositions /
verb-government facts a learner works through from A2 to C1 (lighter at C2). It's
the *syllabus* the drill walks: each generated card is tagged with a concept id
(stored in `drill_items.skill`), so we can show per-concept accuracy and a
per-level completion percentage.

Levels roughly follow the TORFL / ТРКИ grammar minimums (A1 Элементарный … C2
ТРКИ-4). To extend: append a `C(...)` — ids are stable strings, keep them unique.

Progress is derived entirely from `drill_items` (no extra table): a concept is
  new         — never tested
  seen        — 1-2 cards
  practising  — 3+ cards, accuracy < 80%
  learned     — 4+ cards, accuracy >= 80% over the recent window
"""
import store

LEVELS = ("a2", "b1", "b2", "c1", "c2")
_WINDOW = 24        # per concept, only the most-recent N graded cards count
_SEEN_LEARN = 4     # need this many tries before a concept can count as "learned"
_LEARN_PCT = 0.8
_PRACTISE_MIN = 3
_EASY_MIN = 3       # "too easy" ratings in the window (with no miss) → concept rests


def _right(v):
    return v in ("right", "easy")


class C:
    __slots__ = ("id", "level", "cat", "title", "explain", "ex", "traps", "hint", "basic")

    def __init__(self, id, level, cat, title, explain, ex=(), traps=(), hint="", basic=False):
        self.id, self.level, self.cat, self.title = id, level, cat, title
        self.explain, self.ex, self.traps, self.hint = explain, list(ex), list(traps), hint
        self.basic = basic     # a "school basics" concept the user said they already have

    def public(self):
        return {"id": self.id, "level": self.level, "category": self.cat, "title": self.title,
                "explain": self.explain, "examples": [{"ru": r, "en": e} for r, e in self.ex],
                "traps": self.traps, "basic": self.basic}


# ============================================================ THE CATALOG
CONCEPTS = [
    # ---------------------------------------------------------------- cases: genitive
    C("gen-negation", "a2", "genitive", "Genitive of absence (нет / не было)",
      "After нет, не было, не будет the thing that's absent goes in the genitive.",
      ex=[("У меня нет времени.", "I don't have time."),
          ("Вчера не было дождя.", "There was no rain yesterday.")],
      traps=["не будет also takes genitive: завтра не будет урока."], basic=True,
      hint="a 'there is no X' / 'I don't have X' sentence"),
    C("gen-quantity", "a2", "genitive", "Genitive after quantity words",
      "много, мало, немного, несколько, сколько, столько + genitive (plural for countables, "
      "singular for uncountables).",
      ex=[("У нас мало времени.", "We have little time."),
          ("Здесь много туристов.", "There are many tourists here.")],
      traps=["немного молока (sg) vs несколько книг (pl)."], basic=True,
      hint="a 'много / несколько / сколько + noun' phrase"),
    C("gen-of-possession", "a2", "genitive", "Genitive for 'of' / possession",
      "Noun + noun-in-genitive = 'the X of Y' (центр города, дверь машины).",
      ex=[("Это машина моего брата.", "This is my brother's car."),
          ("Мы стояли у входа в музей.", "We stood at the museum entrance.")],
      basic=True, hint="an 'X of Y' possession phrase"),
    C("gen-numbers-5plus", "a2", "genitive", "Genitive plural after 5, 6, 7… (5+ rule)",
      "5 and above (and 0, and numbers ending in 5-9, 11-19) take genitive plural.",
      ex=[("В классе двадцать пять учеников.", "There are 25 pupils in the class."),
          ("Прошло шесть лет.", "Six years passed.")],
      traps=["год → лет with 5+ (пять лет, not годов)."], basic=True,
      hint="a sentence with 5+ of something"),
    C("gen-numbers-234", "a2", "genitive", "Genitive singular after 2, 3, 4",
      "2/3/4 (and compounds ending in 2-4, except 12-14) take genitive singular; "
      "adjectives in the phrase go plural.",
      ex=[("У меня два больших чемодана.", "I have two big suitcases."),
          ("Он прожил здесь три года.", "He lived here three years.")],
      traps=["две/три/четыре for feminine; adjective is plural (больших) even with gen sg noun."],
      basic=True, hint="a sentence with 2, 3 or 4 of something"),
    C("gen-comparative", "b1", "genitive", "Genitive after a comparative",
      "старше брата = older than [my] brother — the thing compared goes in the genitive "
      "(an alternative to чем + nominative).",
      ex=[("Она старше меня на два года.", "She is two years older than me."),
          ("Эта задача сложнее предыдущей.", "This problem is harder than the previous one.")],
      traps=["Only works with a synthetic comparative (старше, сложнее), not более сложный."],
      hint="a 'X is [comparative] than Y' sentence using the genitive form"),
    C("gen-partitive", "b1", "genitive", "Partitive genitive (some of / a portion)",
      "'some tea', 'a piece of bread', 'a cup of coffee' — the substance is genitive. "
      "Some masc nouns take a special -у: чаю, сахару, супу.",
      ex=[("Налей мне чаю, пожалуйста.", "Pour me some tea, please."),
          ("Купи килограмм картошки.", "Buy a kilo of potatoes.")],
      traps=["налить чай = pour THE tea; налить чаю = pour SOME tea."],
      hint="a 'some X' / 'a [measure] of X' sentence"),
    C("gen-dates", "b1", "genitive", "Genitive for dates ('on the Nth')",
      "'on the 5th of May' = пятого мая — ordinal + month both genitive. 'What's the date?' "
      "= Какое сегодня число? — Answer: nominative ordinal.",
      ex=[("Я приехал третьего сентября.", "I arrived on the 3rd of September."),
          ("Экзамен двадцатого числа.", "The exam is on the 20th.")],
      traps=["'What date is it' answer is nominative (сегодня пятое); 'on' the date is genitive."],
      hint="a 'something happened on the Nth of [month]' sentence"),
    C("gen-verbs-fear-wish", "b1", "government", "Verbs that take the genitive",
      "бояться, желать, хотеть (a bit), ждать, требовать, достигать, избегать, "
      "лишаться, придерживаться + genitive.",
      ex=[("Он боится темноты.", "He's afraid of the dark."),
          ("Желаю вам успеха!", "I wish you success!")],
      traps=["ждать: ждать автобуса (gen, indefinite) vs ждать маму (acc, a specific person)."],
      hint="a 'fear / wait for / wish / avoid' sentence"),
    C("gen-preps-basic", "b1", "prepositions", "Basic genitive prepositions",
      "без, для, до, после, около/у, от, из, с (off / since) all take the genitive.",
      ex=[("Я не могу жить без кофе.", "I can't live without coffee."),
          ("Магазин работает до девяти.", "The shop is open until nine.")],
      basic=True, hint="a sentence with без / для / до / после / от + noun"),
    C("gen-preps-advanced", "b2", "prepositions", "Advanced genitive prepositions",
      "из-за, из-под, кроме, среди, против, ради, вместо, накануне, вокруг, мимо, "
      "насчёт, в течение, во время, в результате + genitive.",
      ex=[("Мы опоздали из-за пробок.", "We were late because of traffic."),
          ("Кроме тебя, никто не пришёл.", "Apart from you, nobody came.")],
      traps=["из-за = because of (often negative) OR from behind; из-под = from under."],
      hint="a sentence with из-за / кроме / среди / вместо / в течение + noun"),
    C("gen-second-u", "b2", "nouns", "The special genitive -у (partitive / set phrases)",
      "A handful of masc nouns take -у instead of -а in partitive and fixed phrases: "
      "чаю, сахару, супу, народу, никакого толку, с глазу на глаз.",
      ex=[("Сколько сейчас народу на улице!", "What a crowd on the street!"),
          ("Он ушёл без спросу.", "He left without asking.")],
      hint="a set phrase or partitive using a -у genitive"),

    # ---------------------------------------------------------------- cases: accusative
    C("acc-direct-object", "a2", "accusative", "Direct object (accusative)",
      "The thing directly acted on. Inanimate masc looks like nominative; animate masc/all "
      "plural animate look like the genitive.",
      ex=[("Я вижу большой дом.", "I see a big house."),
          ("Она встретила подругу.", "She met her friend.")],
      basic=True, hint="a simple 'subject verbs a direct object' sentence"),
    C("acc-animate", "b1", "accusative", "Animate accusative = genitive",
      "For living things, accusative borrows the genitive form (masc sg and all plurals): "
      "вижу студента, вижу студентов, вижу собак.",
      ex=[("Ты знаешь этого человека?", "Do you know this person?"),
          ("Мы пригласили всех друзей.", "We invited all our friends.")],
      traps=["Applies to animals too; and to одушевлённые like покойник, кукла (grammatically)."],
      hint="a sentence with an animate direct object (person/animal), masc sg or plural"),
    C("acc-motion", "a2", "accusative", "Motion destination: в / на + accusative",
      "Going TO a place = в/на + accusative (answers куда?). Being AT it = в/на + "
      "prepositional (где?).",
      ex=[("Мы едем в Москву.", "We're going to Moscow."),
          ("Положи книгу на стол.", "Put the book on the table.")],
      basic=True, hint="a 'going / putting something INTO a place' sentence"),
    C("acc-duration", "b1", "accusative", "Duration: accusative with no preposition",
      "'for [a length of time]' during which something went on = bare accusative: весь день, "
      "всю неделю, целый час, много лет.",
      ex=[("Я ждал тебя целый час.", "I waited for you a whole hour."),
          ("Всю ночь шёл дождь.", "It rained all night.")],
      traps=["No 'for' word — весь день, not 'на весь день' (that's duration of a PLANNED future)."],
      hint="a 'did something FOR a whole day/week/hour' sentence, no preposition"),
    C("acc-za-within", "b1", "accusative", "за + accusative: 'within / in' a span",
      "How long an action TOOK to complete: сделал за час, прочитал за два дня.",
      ex=[("Он написал отчёт за один день.", "He wrote the report in one day."),
          ("Мы доехали за полчаса.", "We got there in half an hour.")],
      traps=["за час = it took an hour (completed); час = the process lasted an hour."],
      hint="a 'finished X in [time span]' sentence with за"),
    C("acc-cherez-time", "a2", "accusative", "через + accusative: 'in / after' (future)",
      "через час = in an hour from now; через неделю = in a week. Past 'ago' = time + назад.",
      ex=[("Позвоню через двадцать минут.", "I'll call in twenty minutes."),
          ("Он вернётся через год.", "He'll be back in a year.")],
      traps=["через for future 'in'; за for 'within which it got done'."],
      hint="an 'in [time] from now' sentence with через"),
    C("acc-kazhdy", "a2", "accusative", "каждый + accusative (every day/week)",
      "'every X' as a time phrase goes in the accusative: каждый день, каждую неделю, "
      "каждое утро.",
      ex=[("Я хожу в зал каждый день.", "I go to the gym every day."),
          ("Мы встречаемся каждую субботу.", "We meet every Saturday.")],
      basic=True, hint="an 'every day / every week' habitual sentence"),

    # ---------------------------------------------------------------- cases: dative
    C("dat-indirect-object", "a2", "dative", "Indirect object (to whom / for whom)",
      "The recipient: give/tell/show/write/send something TO someone.",
      ex=[("Я подарил маме цветы.", "I gave my mum flowers."),
          ("Расскажи нам эту историю.", "Tell us that story.")],
      basic=True, hint="a 'give / tell / show / write something to someone' sentence"),
    C("dat-age", "a2", "dative", "Age (мне двадцать лет)",
      "The person whose age it is goes in the dative; no verb in the present.",
      ex=[("Моему сыну восемь лет.", "My son is eight."),
          ("Сколько тебе лет?", "How old are you?")],
      traps=["год / года / лет follows the 1 / 2-4 / 5+ rule."],
      basic=True, hint="a 'someone is N years old' sentence"),
    C("dat-impersonal-state", "b1", "dative", "Dative + adverb: how someone feels",
      "мне холодно, ему скучно, нам весело, тебе трудно — the experiencer is dative, the "
      "state is a -о adverb, no verb (был/будет in other tenses).",
      ex=[("Мне было холодно всю ночь.", "I was cold all night."),
          ("Ей стало плохо.", "She started feeling sick.")],
      traps=["Past: мне было скучно (было agrees with nothing — neuter). Not 'я был холодный'."],
      hint="a 'someone feels cold / bored / sad' impersonal sentence"),
    C("dat-modal-inf", "a2", "dative", "нужно / надо / можно / нельзя + infinitive",
      "The person who must/may/can't do it is dative; the action is an infinitive.",
      ex=[("Мне нужно идти.", "I need to go."),
          ("Здесь нельзя курить.", "You can't smoke here.")],
      traps=["Past: мне нужно было; future: мне нужно будет."],
      basic=True, hint="a 'someone needs / may / can't do something' sentence"),
    C("dat-nravitsya", "a2", "dative", "нравиться / понравиться (to like)",
      "The liker is dative, the liked thing is the subject (nominative) and the verb agrees "
      "with IT.",
      ex=[("Мне нравится эта песня.", "I like this song."),
          ("Детям понравился фильм.", "The kids liked the film.")],
      traps=["нравиться (impf) = like in general; понравиться (pf) = took a liking / liked (once)."],
      basic=True, hint="a 'someone likes something' sentence with нравиться"),
    C("dat-verbs", "b1", "government", "Verbs that take the dative",
      "помогать, мешать, звонить, советовать, обещать, отвечать (to a person), верить, "
      "доверять, удивляться, радоваться, завидовать, принадлежать + dative.",
      ex=[("Не мешай мне работать.", "Don't disturb me while I work."),
          ("Я позвоню тебе вечером.", "I'll call you this evening.")],
      traps=["звонить + dative (звоню другу), never 'звонить друга'."],
      hint="a 'help / call / disturb / advise / believe someone' sentence"),
    C("dat-k-motion", "a2", "dative", "к + dative: towards / to a person / by a time",
      "Motion to a person or up to a place-edge, or a deadline: иду к врачу, к вечеру, "
      "к пяти часам.",
      ex=[("Приходи ко мне в гости.", "Come visit me."),
          ("Закончу к пятнице.", "I'll finish by Friday.")],
      traps=["ко мне / ко всем (buffer -о); 'to a person's place' is к + dative, not в."],
      hint="a 'going to someone's place' or 'by [deadline]' sentence with к"),
    C("dat-preps-advanced", "b2", "prepositions", "благодаря / согласно / вопреки + dative",
      "благодаря = thanks to (positive cause); согласно = according to; вопреки = despite / "
      "contrary to.",
      ex=[("Благодаря тебе я всё понял.", "Thanks to you I understood everything."),
          ("Вопреки прогнозу, было солнечно.", "Contrary to the forecast, it was sunny.")],
      traps=["благодаря + dative (not genitive); use из-за for a negative cause."],
      hint="a 'thanks to / according to / despite' sentence with the dative"),

    # ---------------------------------------------------------------- cases: instrumental
    C("instr-with-company", "a2", "instrumental", "с + instrumental: together with",
      "Doing something together with someone/something: с другом, с сахаром, с удовольствием.",
      ex=[("Я иду в кино с братом.", "I'm going to the cinema with my brother."),
          ("Кофе с молоком, пожалуйста.", "Coffee with milk, please.")],
      basic=True, hint="a 'doing something with someone / something' sentence"),
    C("instr-means", "b1", "instrumental", "Instrumental of means / instrument",
      "The tool or means, no preposition: писать ручкой, резать ножом, ехать поездом, "
      "говорить шёпотом.",
      ex=[("Он открыл дверь ключом.", "He opened the door with a key."),
          ("Мы поехали туда автобусом.", "We went there by bus.")],
      traps=["ехать поездом / автобусом (instr) OR на поезде / на автобусе (both fine)."],
      hint="a 'do something using [a tool] / travel by [transport]' sentence, no preposition"),
    C("instr-time", "a2", "instrumental", "Instrumental for parts of day & seasons",
      "утром, днём, вечером, ночью, весной, летом, осенью, зимой — no preposition.",
      ex=[("Утром я пью кофе.", "I drink coffee in the morning."),
          ("Зимой здесь очень холодно.", "It's very cold here in winter.")],
      basic=True, hint="an 'in the morning / evening / winter' time sentence"),
    C("instr-predicate", "b1", "instrumental", "Predicate instrumental (быть / стать кем)",
      "After быть (past/future/inf), стать, становиться, работать, служить, казаться, "
      "являться, называться, считаться — the role/quality is instrumental.",
      ex=[("Он хочет стать врачом.", "He wants to become a doctor."),
          ("В детстве я был очень застенчивым.", "As a child I was very shy.")],
      traps=["Present 'is' takes nominative (он врач); past/future/goal take instrumental (он был врачом)."],
      hint="a 'was / became / works as / seems [a role or quality]' sentence"),
    C("instr-verbs", "b1", "government", "Verbs that take the instrumental",
      "заниматься, интересоваться, увлекаться, пользоваться, владеть, управлять, руководить, "
      "гордиться, восхищаться, наслаждаться, дышать + instrumental.",
      ex=[("Она занимается музыкой.", "She does music / studies music."),
          ("Я горжусь тобой.", "I'm proud of you.")],
      traps=["интересоваться + instr (историей), but интерес к + dative (интерес к истории)."],
      hint="a 'be interested in / be proud of / use / be busy with' sentence"),
    C("instr-za-fetch", "b1", "instrumental", "за + instrumental: 'to (go and) get'",
      "идти / послать за + instrumental = go / send for something. (за + accusative = behind, "
      "motion.)",
      ex=[("Я сбегаю за хлебом.", "I'll pop out for bread."),
          ("Мама послала меня за молоком.", "Mum sent me for milk.")],
      traps=["за хлебом (instr) = to fetch bread; за дом (acc) = to behind the house."],
      hint="a 'go / send someone to fetch X' sentence with за"),
    C("instr-space-preps", "a2", "prepositions", "над / под / перед / между / за + instrumental",
      "Static spatial relations: над столом, под кроватью, перед домом, между окнами, за углом.",
      ex=[("Кот спит под столом.", "The cat sleeps under the table."),
          ("Встретимся перед входом.", "Let's meet in front of the entrance.")],
      traps=["Location = под столом (instr); motion 'to under' = под стол (acc)."],
      hint="a 'something is above / under / in front of / between' location sentence"),

    # ---------------------------------------------------------------- cases: prepositional
    C("prep-location", "a2", "prepositional", "в / на + prepositional: location",
      "Where something is (где?): в доме, на столе, в городе, на работе.",
      ex=[("Книга лежит на полке.", "The book is on the shelf."),
          ("Он живёт в маленькой деревне.", "He lives in a small village.")],
      basic=True, hint="a 'something is located in / on a place' sentence"),
    C("prep-o-about", "a2", "prepositional", "о / об / обо + prepositional: about",
      "Talking, thinking, dreaming ABOUT: о фильме, об экзамене, обо мне. об before a vowel "
      "sound, обо in обо мне / обо всём.",
      ex=[("Расскажи мне о своей поездке.", "Tell me about your trip."),
          ("Я часто думаю об этом.", "I often think about that.")],
      traps=["о + consonant, об + vowel-letter (об Анне), обо мне / обо всём."],
      basic=True, hint="a 'talk / think / dream about something' sentence"),
    C("prep-v-na-choice", "b1", "prepositions", "в vs на with institutions & activities",
      "Enclosed spaces & most countries/cities take в; open areas, events, activities, some "
      "institutions and a fixed list (вокзал, почта, завод, факультет, кухня, Украина, "
      "Кавказ) take на.",
      ex=[("Она на работе, а он в университете.", "She's at work, he's at university."),
          ("Мы были на концерте, потом на вокзале.", "We were at a concert, then at the station.")],
      traps=["на почте / на заводе / на факультете / на кухне; в школе / в театре / в офисе."],
      hint="a 'someone is at [an institution / event / activity]' sentence — pick в or на"),
    C("prep-pri", "b2", "prepositions", "при + prepositional",
      "'in the presence of', 'attached to', 'during the era/reign of', 'given / under "
      "conditions': при мне, при университете, при Петре I, при желании.",
      ex=[("Не говори об этом при детях.", "Don't talk about this in front of the children."),
          ("При такой погоде лучше остаться дома.", "In weather like this it's better to stay home.")],
      hint="a 'in the presence of / under [conditions] / during the time of' sentence with при"),
    C("prep-second-u", "b2", "nouns", "The special locative -у (в лесу, на берегу)",
      "About 100 masc nouns take stressed -у after в/на for location: в лесу, в саду, на "
      "берегу, на мосту, в углу, в шкафу, в этом году, на краю.",
      ex=[("Мы гуляли в лесу.", "We walked in the forest."),
          ("В прошлом году я был в Италии.", "Last year I was in Italy.")],
      traps=["Only after в/на for location — о лесе (about the forest) stays regular."],
      hint="a 'in the forest / in the garden / on the shore / that year' location sentence"),
    C("prep-po-upon", "c1", "prepositions", "по + prepositional: 'upon / on completion of'",
      "Bookish: по окончании университета, по приезде, по прибытии = upon finishing / on "
      "arrival.",
      ex=[("По окончании школы он уехал за границу.", "On finishing school he went abroad."),
          ("По прибытии сообщите нам.", "Let us know upon arrival.")],
      hint="a formal 'upon finishing / upon arriving' sentence with по + prepositional"),

    # ---------------------------------------------------------------- adjectives
    C("adj-agreement", "a2", "adjectives", "Adjective agreement",
      "Adjectives match their noun in gender, number and case.",
      ex=[("Я купил новую машину.", "I bought a new car."),
          ("Мы говорили о старых друзьях.", "We talked about old friends.")],
      basic=True, hint="a phrase with an adjective + noun in an oblique case"),
    C("adj-short-form", "b1", "adjectives", "Short-form adjectives (predicate)",
      "Some adjectives have a short predicate form: рад, готов, согласен, должен, болен, "
      "занят, уверен, похож, нужен. Often the only option (я согласен, not 'я согласный').",
      ex=[("Я полностью с тобой согласен.", "I completely agree with you."),
          ("Она была очень занята.", "She was very busy.")],
      traps=["Agree in gender/number: он готов, она готова, они готовы."],
      hint="a 'someone is ready / busy / sure / agrees' predicate sentence"),
    C("adj-comparative", "a2", "adjectives", "Comparative (better, older, more expensive)",
      "Synthetic: add -ее / -е (быстрее, дороже, старше, лучше). Predicate use is very common.",
      ex=[("Сегодня теплее, чем вчера.", "Today is warmer than yesterday."),
          ("Этот вариант лучше.", "This option is better.")],
      traps=["Irregulars: хороший→лучше, плохой→хуже, большой→больше, маленький→меньше."],
      basic=True, hint="a 'X is [comparative] than Y' sentence"),
    C("adj-comparative-analytic", "b1", "adjectives", "Analytic comparative (более / менее)",
      "более / менее + plain adjective, used attributively or where a synthetic form is "
      "awkward: более удобный способ, менее важный вопрос.",
      ex=[("Нам нужен более опытный сотрудник.", "We need a more experienced employee."),
          ("Это менее серьёзная проблема.", "This is a less serious problem.")],
      traps=["Don't double up: более лучше is wrong."],
      hint="an attributive 'a more / less [adjective] X' phrase"),
    C("adj-superlative", "b1", "adjectives", "Superlative (самый / -ейший)",
      "самый + adjective (самый интересный); or -ейший / -айший (интереснейший, "
      "глубочайший); наиболее in formal style.",
      ex=[("Это самый красивый город в стране.", "This is the most beautiful city in the country."),
          ("Он один из лучших врачей.", "He's one of the best doctors.")],
      hint="a 'the most [adjective] X' sentence"),
    C("adj-comparative-degree", "b2", "adjectives", "Intensifying a comparative",
      "гораздо / намного / значительно + comparative; всё + comparative ('X-er and X-er'); "
      "как можно + comparative ('as X as possible'); чуть / немного.",
      ex=[("Стало гораздо холоднее.", "It got much colder."),
          ("Приходи как можно раньше.", "Come as early as possible.")],
      hint="a 'much colder / colder and colder / as soon as possible' sentence"),

    # ---------------------------------------------------------------- pronouns
    C("pron-svoj", "b1", "pronouns", "свой — the subject's own",
      "When the possessor is the sentence's subject, use свой (not мой / твой / его / её / "
      "их) for the thing possessed.",
      ex=[("Он взял свою книгу.", "He took his (own) book."),
          ("Расскажи о своих планах.", "Tell me about your plans.")],
      traps=["Он взял его книгу = someone else's book. Doesn't normally modify the subject itself."],
      hint="a sentence where the subject does something to/with their OWN thing"),
    C("pron-sebya", "b1", "pronouns", "себя — reflexive object",
      "'oneself' as an object, any person: я вижу себя, он купил себе, расскажи о себе. "
      "Fixed: у себя, к себе, про себя.",
      ex=[("Посмотри на себя в зеркало.", "Look at yourself in the mirror."),
          ("Он купил себе новый телефон.", "He bought himself a new phone.")],
      traps=["No nominative form; себя / себе / собой / о себе only."],
      hint="a 'do something to / for / about oneself' sentence with себя"),
    C("pron-drug-druga", "b1", "pronouns", "друг друга — each other",
      "Reciprocal 'each other'. The first друг never changes; the second declines / takes "
      "the preposition: друг друга, друг другу, друг с другом, друг о друге.",
      ex=[("Они давно знают друг друга.", "They've known each other a long time."),
          ("Мы помогаем друг другу.", "We help each other.")],
      hint="a 'they [verb] each other' sentence"),
    C("pron-ves", "b1", "pronouns", "весь / всё / все",
      "весь + noun = the whole / all of; всё (n sg) = everything; все (pl) = everyone. "
      "Declines and agrees.",
      ex=[("Он съел весь торт.", "He ate the whole cake."),
          ("Спасибо всем за помощь.", "Thanks to everyone for the help.")],
      traps=["всё = everything (things); все = everybody (people). всех / всем in oblique."],
      hint="a sentence with 'the whole / everything / everyone' in an oblique case"),
    C("pron-negative-nikto", "a2", "pronouns", "никто / ничто + double negative",
      "Negative pronouns require не on the verb too: никто не пришёл, я ничего не знаю. "
      "With a preposition it splits: ни с кем, ни о чём.",
      ex=[("Я никому ничего не сказал.", "I didn't tell anyone anything."),
          ("Он ни с кем не разговаривает.", "He isn't talking to anyone.")],
      traps=["Always keep the не. Preposition goes inside: ни у кого, ни к кому."],
      basic=True, hint="a 'nobody / nothing / never [verb]' sentence with the double negative"),
    C("pron-negative-nekogo", "b2", "pronouns", "некого / нечего + infinitive",
      "'there's no one / nothing to …' — dative subject, stress on не: мне некого спросить, "
      "нам нечего терять, ему негде жить.",
      ex=[("Мне не с кем поговорить.", "I have no one to talk to."),
          ("Нам нечего бояться.", "We have nothing to fear.")],
      traps=["Different from ничего не: некого (no one exists to ask) vs никого не спросил (asked no one)."],
      hint="a 'there's no one / nothing / nowhere to [verb]' sentence"),
    C("pron-indefinite", "b1", "pronouns", "-то vs -нибудь vs -либо vs кое-",
      "-то = a specific but unknown one (кто-то позвонил); -нибудь = any one at all, "
      "non-specific / future / question (принеси что-нибудь); кое- = speaker knows but "
      "won't say; -либо = bookish 'any'.",
      ex=[("Кто-то оставил зонт.", "Someone left an umbrella."),
          ("Расскажи мне что-нибудь интересное.", "Tell me something interesting.")],
      traps=["Past fact → -то; request / future / question → -нибудь."],
      hint="a sentence needing 'someone / something' — pick -то or -нибудь by context"),
    C("pron-sam", "b2", "pronouns", "сам — '[did it] oneself' (emphatic)",
      "Emphasises that the person did it personally / alone: я сам всё сделал, она сама "
      "виновата, поговори с ним самим.",
      ex=[("Он сам во всём виноват.", "He himself is to blame for everything."),
          ("Я сделаю это сама.", "I'll do it myself.")],
      traps=["сам (emphatic doer) ≠ себя (reflexive object) ≠ один (alone)."],
      hint="a 'did it oneself / is to blame oneself' emphatic sentence"),
    C("pron-kazhdy-lyuboj", "b2", "pronouns", "каждый / любой / всякий",
      "каждый = each (one by one); любой = any (you choose); всякий = every kind of / all "
      "sorts (often + разный overtones, or 'any' in set phrases).",
      ex=[("Любой может ошибиться.", "Anyone can make a mistake."),
          ("Он приходит каждый вторник.", "He comes every Tuesday.")],
      hint="a sentence contrasting 'each' vs 'any' — pick каждый / любой"),

    # ---------------------------------------------------------------- numerals
    C("num-oblique", "b2", "numerals", "Declining numbers in oblique cases",
      "Numbers themselves decline: к двум часам, о ста рублях, с пятью друзьями, более двухсот "
      "человек. Both halves of compound numbers decline.",
      ex=[("Я вернусь через два дня.", "I'll be back in two days."),
          ("Он пришёл с тремя детьми.", "He came with three children.")],
      traps=["двумя, тремя, четырьмя, пятью; сорока / девяноста / ста are the same in all obliques."],
      hint="a sentence where a number is in the dative / instrumental / prepositional"),
    C("num-collective", "b2", "numerals", "Collective numerals (двое, трое, четверо)",
      "двое / трое / четверо / пятеро + genitive plural — for groups of people (esp. mixed / "
      "male), children, young animals, pluralia tantum, and 'X of us/you/them'.",
      ex=[("У них трое детей.", "They have three children."),
          ("Нас было двое.", "There were two of us.")],
      traps=["двое суток, двое ножниц; but две девушки (use cardinal for a purely female group)."],
      hint="a 'they have three children / there were two of us' sentence"),
    C("num-oba", "b1", "numerals", "оба / обе (both)",
      "оба (masc/neut) / обе (fem) + genitive singular, like 2. Obliques: обоих / обеих.",
      ex=[("Обе сестры живут в Москве.", "Both sisters live in Moscow."),
          ("Он взял обе сумки.", "He took both bags.")],
      traps=["обе for a feminine pair; в обоих случаях / в обеих руках in oblique."],
      hint="a 'both X' sentence — pick оба / обе"),
    C("num-approximation", "b2", "numerals", "Approximation by inversion (лет пять)",
      "Put the noun before the number to mean 'about': лет пять, часа два, человек десять, "
      "рублей сто. Or use около + genitive.",
      ex=[("Ему лет сорок.", "He's about forty."),
          ("Подожди минут пять.", "Wait about five minutes.")],
      hint="an 'about five years / about ten people' sentence using inversion"),
    C("num-time-telling", "b1", "numerals", "Telling the time",
      "Half past = полдесятого / половина десятого (of the NEXT hour); to = без четверти "
      "шесть, без десяти три; past = десять минут седьмого (bookish) or просто 6:10.",
      ex=[("Сейчас без четверти девять.", "It's a quarter to nine."),
          ("Встреча в половине третьего.", "The meeting is at half past two.")],
      traps=["полдесятого = 9:30 (half of the tenth hour). без пяти час = 12:55."],
      hint="a 'the time is half past / a quarter to' sentence"),
    C("num-dates-year", "b1", "numerals", "In which year (в 2020 году)",
      "'in [year]' = в + ordinal (only the last word) + году: в две тысячи двадцатом году. "
      "'What year' = в каком году.",
      ex=[("Я родился в тысяча девятьсот девяносто пятом году.", "I was born in 1995."),
          ("Это было в прошлом году.", "That was last year.")],
      traps=["Only the last part is ordinal & prepositional: в 2020 → в две тысячи двадцатом году."],
      hint="an 'in [year]' sentence"),

    # ---------------------------------------------------------------- verb conjugation
    C("conj-consonant-mutation", "b1", "verbs", "Consonant mutation in conjugation",
      "Many verbs change the stem consonant: любить→люблю, писать→пишу, ходить→хожу, "
      "видеть→вижу, платить→плачу. Often only in the 1st person sg (2nd conj) or throughout "
      "(1st conj).",
      ex=[("Я тебя очень люблю.", "I love you very much."),
          ("Я плачу за обед.", "I'm paying for lunch.")],
      traps=["2nd conj mutates only in я-form (люблю, but любишь / любит). 1st conj mutates throughout (пишу, пишешь)."],
      hint="a present-tense sentence where the verb's я-form has a mutated consonant"),
    C("conj-irregular", "b1", "verbs", "Irregular verbs (хотеть, бежать, есть, дать)",
      "A few verbs are mixed-conjugation or fully irregular: хочу/хочешь/хотят, бегу/бежишь/"
      "бегут, ем/ешь/едят, дам/дашь/дадут.",
      ex=[("Что ты хочешь на ужин?", "What do you want for dinner?"),
          ("Дети едят кашу.", "The children are eating porridge.")],
      hint="a present/future sentence with хотеть / бежать / есть / дать"),
    C("verb-imperative", "a2", "verbs", "Forming the imperative",
      "Drop the 3rd-pl ending: читают→читай(те), говорят→говори(те), купят→купи(те). "
      "Stem in a vowel → -й; давай(те) + inf/future for 'let's'; пусть + 3rd person.",
      ex=[("Закрой окно, пожалуйста.", "Close the window, please."),
          ("Давайте начнём.", "Let's begin.")],
      traps=["Aspect matters: делай (process/prohibition) vs сделай (one completed request)."],
      basic=True, hint="a command / request sentence in the imperative"),
    C("verb-sya", "b1", "verbs", "-ся verbs (reflexive / reciprocal / passive / intransitive)",
      "-ся can mean: oneself (мыться), each other (встречаться), passive (дом строится), "
      "or just be an intransitive pair (открывать→открываться, начинать→начинаться).",
      ex=[("Магазин открывается в девять.", "The shop opens at nine."),
          ("Мы познакомились прошлым летом.", "We met last summer.")],
      traps=["-ся verb takes no direct object: дверь открывается (not 'открывается дверь' as object)."],
      hint="a sentence with an intransitive / reflexive -ся verb"),

    # ---------------------------------------------------------------- verb aspect
    C("aspect-core", "b1", "aspect", "Aspect: process vs result",
      "Imperfective = the activity itself, ongoing or repeated. Perfective = a single whole "
      "action seen with its result / completion.",
      ex=[("Я читал книгу весь вечер.", "I was reading a book all evening."),
          ("Я прочитал книгу за два дня.", "I read the book in two days.")],
      traps=["No English tense maps 1:1 — think 'process/repeat' vs 'done/result'."],
      hint="a past sentence where the choice is 'was doing / used to' vs 'did & finished'"),
    C("aspect-past-single-vs-process", "b1", "aspect", "Past: completed event vs background",
      "Perfective for a single completed event that moves the story on; imperfective for what "
      "was going on around it.",
      ex=[("Когда я готовил ужин, зазвонил телефон.", "While I was cooking dinner, the phone rang."),
          ("Он вошёл, сел и открыл ноутбук.", "He came in, sat down and opened his laptop.")],
      hint="a 'while X was happening, Y happened' past sentence"),
    C("aspect-past-repeated", "b1", "aspect", "Past: repeated / habitual → imperfective",
      "каждый день, обычно, часто, всегда, по вечерам, много раз + imperfective past.",
      ex=[("В детстве мы каждое лето ездили на дачу.", "As kids we went to the dacha every summer."),
          ("Она часто звонила маме.", "She often called her mum.")],
      hint="a 'used to / would always do X' habitual past sentence"),
    C("aspect-annulled-result", "b2", "aspect", "Past: 'annulled result' (он открывал окно)",
      "Imperfective past can mean the action happened AND was undone: он открывал окно "
      "(opened it, then closed it); к нам приходил врач (came and left); я брал твою книгу "
      "(borrowed and returned).",
      ex=[("Кто-то открывал моё окно.", "Someone had my window open (it's shut now)."),
          ("Я уже читал эту статью.", "I've read this article before.")],
      traps=["он открыл окно (pf) = it's still open. он открывал окно (impf) = it's shut again."],
      hint="a past sentence meaning 'did X and it's since been undone / done it before'"),
    C("aspect-future", "b1", "aspect", "Future: буду + imperfective vs perfective future",
      "буду / будешь + imperfective infinitive = will be doing / will do (process, repeated). "
      "Perfective present-form = will do once & complete (напишу, приду, сделаю).",
      ex=[("Завтра я буду весь день работать.", "Tomorrow I'll be working all day."),
          ("Я напишу тебе, когда приеду.", "I'll write to you when I arrive.")],
      traps=["No 'буду' with a perfective — 'буду сделать' is wrong; just сделаю."],
      hint="a future sentence — choose будущее with буду vs a perfective future form"),
    C("aspect-phase-verbs", "b1", "aspect", "Phase verbs → imperfective infinitive only",
      "начать / начинать, продолжать, кончить / закончить, перестать, бросить ('quit') + "
      "imperfective infinitive, always.",
      ex=[("Он начал изучать русский год назад.", "He started learning Russian a year ago."),
          ("Перестань кричать!", "Stop shouting!")],
      traps=["начать сделать is wrong — начать делать. Same for перестать, продолжать."],
      hint="a 'start / continue / stop / quit doing X' sentence"),
    C("aspect-negative-imperative", "b2", "aspect", "Negative imperative: не + impf vs не + pf",
      "не + imperfective = a prohibition ('don't [ever] do this': не кури здесь). не + "
      "perfective = a warning against an accident ('mind you don't…': не опоздай, не упади, "
      "не забудь).",
      ex=[("Не рассказывай никому.", "Don't tell anyone."),
          ("Смотри не опоздай на поезд!", "Mind you don't miss the train!")],
      traps=["не забывай (as a rule) vs не забудь (this once, don't slip up)."],
      hint="a negative command — a standing rule (impf) or a one-off warning (pf)"),
    C("aspect-nelzya", "b2", "aspect", "нельзя + impf (forbidden) vs нельзя + pf (impossible)",
      "нельзя + imperfective = it's not allowed (здесь нельзя парковаться). нельзя + "
      "perfective = it can't be done (дверь нельзя открыть).",
      ex=[("В музее нельзя фотографировать.", "Photography isn't allowed in the museum."),
          ("Его нельзя убедить.", "He can't be persuaded.")],
      hint="a 'нельзя + infinitive' sentence — is it forbidden or impossible?"),
    C("aspect-infinitive-modal", "b1", "aspect", "Aspect of the infinitive after modals",
      "After хочу / могу / нужно / можно / должен: perfective for a specific single result, "
      "imperfective for a process, a habit, or (esp.) a negated / general one.",
      ex=[("Я хочу купить эту куртку.", "I want to buy this jacket."),
          ("Тебе нужно больше отдыхать.", "You need to rest more.")],
      hint="a 'want / need / can + infinitive' sentence — pick the infinitive's aspect"),
    C("aspect-general-fact", "b2", "aspect", "Imperfective for a 'general fact' (Ты читал…?)",
      "Asking / stating simply WHETHER an action ever took place, with no interest in the "
      "result, uses the imperfective: Ты смотрел этот фильм? Я звонил ему (fact that I did).",
      ex=[("Вы уже обедали?", "Have you had lunch?"),
          ("Я показывал тебе новые фотографии?", "Did I show you the new photos?")],
      traps=["Ты прочитал письмо? = did you finish it. Ты читал письмо? = did you read it at all."],
      hint="a 'have you ever / did you [at all]' general-fact question"),
    C("aspect-pair-formation", "b2", "aspect", "How aspect pairs are built",
      "Prefix (писать→написать, делать→сделать); suffix -ыва/-ива for a derived imperfective "
      "(переписать→переписывать, рассказать→рассказывать); -и↔-а (решить↔решать); suppletive "
      "(говорить↔сказать, брать↔взять, класть↔положить).",
      ex=[("Я долго решал эту задачу и наконец решил её.", "I worked on this problem a long time and finally solved it."),
          ("Он взял ручку и начал брать заметки.", "He took a pen and started taking notes.")],
      hint="a sentence using both members of a tricky aspect pair"),

    # ---------------------------------------------------------------- verbs of motion
    C("motion-uni-vs-multi", "b1", "motion", "идти vs ходить (one trip vs repeated / round-trip)",
      "Unidirectional (идти, ехать, лететь, бежать) = one trip in progress, one direction. "
      "Multidirectional (ходить, ездить, летать, бегать) = repeated trips, round trips, "
      "general ability, or 'there and back'.",
      ex=[("Сейчас я иду на работу.", "I'm walking to work right now."),
          ("Я хожу на работу каждый день.", "I go to work every day.")],
      traps=["Вчера я ходил в кино = went and came back. Вчера я шёл в кино, когда… = was on my way."],
      hint="a 'going somewhere' sentence — one trip now vs a habit / round trip"),
    C("motion-foot-vs-vehicle", "a2", "motion", "идти vs ехать (on foot vs by vehicle)",
      "идти / ходить = under your own steam; ехать / ездить = by transport. Also плыть "
      "(sail/swim), лететь (fly).",
      ex=[("В школу я хожу пешком, а на работу езжу на метро.",
           "I walk to school but take the metro to work."),
          ("Летом мы поедем на юг.", "In summer we'll go south.")],
      basic=True, hint="a 'go somewhere on foot / by transport' sentence"),
    C("motion-set-off", "b1", "motion", "пойти / поехать — set off, 'let's go'",
      "The perfective по- form = start moving / set off, and is the normal past 'went' and "
      "future 'will go': Я пошёл! Пойдём в кафе. Завтра поедем за город.",
      ex=[("Вчера мы пошли в театр.", "Yesterday we went to the theatre."),
          ("Пойдём погуляем!", "Let's go for a walk!")],
      traps=["пошёл (set off / went, single) vs ходил (went & came back) vs шёл (was walking)."],
      hint="a 'we set off / let's go / went (once)' sentence with пойти / поехать"),
    C("motion-prefix-arrival-departure", "b1", "motion", "при- / у- : arrive / leave",
      "при- = arrive, get here (приходить/прийти, приезжать/приехать). у- = leave, go away, "
      "be gone (уходить/уйти, уезжать/уехать).",
      ex=[("Поезд приходит в семь.", "The train arrives at seven."),
          ("Она уже ушла домой.", "She's already left for home.")],
      traps=["prefixed motion verbs form NORMAL aspect pairs: приходить (impf) / прийти (pf)."],
      hint="an 'arrives / has left / will come' sentence with a при-/у- motion verb"),
    C("motion-prefix-in-out", "b1", "motion", "в(о)- / вы- : in / out",
      "в- = go in, enter (входить/войти); вы- = go out, step out, leave (for a bit) "
      "(выходить/выйти). вы- is stressed in the perfective (вы́йти, вы́ехать).",
      ex=[("Войдите!", "Come in!"),
          ("Он вышел на пять минут.", "He stepped out for five minutes.")],
      hint="an 'enter / step out / go in / go out' sentence"),
    C("motion-prefix-approach-away", "b2", "motion", "под(о)- / от(о)- : approach / move off",
      "под- = come up to, approach (подходить/подойти к + dat). от- = move away from, step "
      "back (отходить/отойти от + gen).",
      ex=[("К нам подошёл официант.", "A waiter came up to us."),
          ("Отойди от края!", "Step back from the edge!")],
      traps=["под- + к + dative; от- + от + genitive."],
      hint="a 'come up to / step away from' sentence with под-/от-"),
    C("motion-prefix-do-za-pro", "b2", "motion", "до- / за- / про- : reach / drop by / pass",
      "до- = get as far as, reach (доходить/дойти до + gen). за- = drop in, call by, or go "
      "behind (заходить/зайти к / за). про- = go past, through, or cover (a distance).",
      ex=[("Мы дошли до озера за час.", "We reached the lake in an hour."),
          ("Зайди ко мне после работы.", "Drop by mine after work.")],
      hint="a 'reach / drop by / go past' sentence with до-/за-/про-"),
    C("motion-14-pairs", "b2", "motion", "The other motion pairs (нести, вести, везти…)",
      "нести/носить (carry), вести/водить (lead, drive a car), везти/возить (transport), "
      "лететь/летать, плыть/плавать, ползти/ползать, лезть/лазить, тащить/таскать, "
      "гнать/гонять, катить/катать, брести/бродить — all pair uni/multi.",
      ex=[("Она несёт тяжёлую сумку.", "She's carrying a heavy bag."),
          ("Он водит машину с восемнадцати лет.", "He's been driving since he was 18.")],
      traps=["нести (carrying now, one way) vs носить (carries regularly / wears clothes)."],
      hint="a 'carry / lead / transport / fly / swim' sentence with the right uni/multi verb"),
    C("motion-figurative", "b2", "motion", "Figurative motion verbs",
      "Idiomatic: время идёт, дождь / снег идёт, речь идёт о, часы идут, автобус идёт "
      "(номер 5), это тебе не идёт, дела идут хорошо.",
      ex=[("Речь идёт о серьёзной проблеме.", "We're talking about a serious problem."),
          ("Как идут твои дела?", "How are things going for you?")],
      hint="an idiomatic sentence where a motion verb isn't literal (время идёт, речь идёт о…)"),

    # ---------------------------------------------------------------- prepositions: по
    C("po-along-surface", "b1", "prepositions", "по + dative: along / around / over a surface",
      "Movement over/along a surface or through an area: идти по улице, гулять по парку, "
      "путешествовать по Европе, разбросать по комнате.",
      ex=[("Мы долго гуляли по старому городу.", "We walked around the old town for a long time."),
          ("Слёзы текли по щекам.", "Tears ran down his cheeks.")],
      hint="a 'walk / travel / spread ALONG or AROUND a place' sentence with по"),
    C("po-means-channel", "b1", "prepositions", "по + dative: by means of / via a channel",
      "по телефону, по электронной почте, по радио, по телевизору, по интернету, по плану, "
      "по расписанию, по профессии, по имени, по ошибке.",
      ex=[("Давай поговорим по телефону.", "Let's talk on the phone."),
          ("Он инженер по профессии.", "He's an engineer by profession.")],
      hint="a 'by phone / by email / by profession / according to plan' sentence with по"),
    C("po-recurring-days", "b1", "prepositions", "по + dative plural: on recurring days",
      "по понедельникам, по выходным, по вечерам, по утрам = every Monday, on weekends, in "
      "the evenings (as a habit).",
      ex=[("По субботам я хожу на йогу.", "On Saturdays I go to yoga."),
          ("Музей закрыт по понедельникам.", "The museum is closed on Mondays.")],
      traps=["Dative PLURAL: по понедельникам, not по понедельнику."],
      hint="an 'every Monday / on weekends' habitual sentence with по + dative plural"),
    C("po-cause-distributive", "b2", "prepositions", "по + dative: cause / distribution",
      "Cause (minor / unintentional): по ошибке, по привычке, по невнимательности, по "
      "болезни. Distributive: по одному, дать всем по яблоку, по два раза.",
      ex=[("Я взял чужой зонт по ошибке.", "I took someone else's umbrella by mistake."),
          ("Раздайте детям по конфете.", "Give each child a sweet.")],
      hint="a 'by mistake / by habit' or 'one each / two apiece' sentence with по"),
    C("po-verbs", "b1", "government", "Verbs with по + dative (скучать, тосковать)",
      "скучать по + dative (or prepositional: по вас), тосковать по, ударить по, стучать по, "
      "судить по.",
      ex=[("Я очень скучаю по дому.", "I really miss home."),
          ("Не суди о книге по обложке.", "Don't judge a book by its cover.")],
      hint="a 'miss / judge by / knock on' sentence with по"),

    # ---------------------------------------------------------------- prepositions: from
    C("from-iz-s-ot", "b1", "prepositions", "из vs с vs от ('from')",
      "из = out of an enclosed place / country (opposite of в): из Москвы, из сумки. с = off "
      "a surface / back from an activity or event (opposite of на): с работы, с концерта, со "
      "стола. от = from a person or a starting point: письмо от друга, отойти от двери.",
      ex=[("Он только что вернулся с работы.", "He just got back from work."),
          ("Я получил посылку от бабушки.", "I got a parcel from my grandmother.")],
      traps=["Match the 'to' preposition: в город → из города; на почту → с почты; к врачу → от врача."],
      hint="a 'came back / got something FROM a place / person' sentence — pick из / с / от"),
    C("iz-za-iz-pod", "b2", "prepositions", "из-за / из-под + genitive",
      "из-за = because of (usually a bad cause) OR from behind (из-за угла, встать из-за "
      "стола). из-под = from under (достать из-под кровати) OR 'formerly containing' "
      "(бутылка из-под вина).",
      ex=[("Рейс задержали из-за погоды.", "The flight was delayed because of the weather."),
          ("Кот вылез из-под дивана.", "The cat crawled out from under the sofa.")],
      hint="a 'because of / from behind / from under' sentence with из-за or из-под"),

    # ---------------------------------------------------------------- prepositions: за
    C("za-acc-exchange-price", "b1", "prepositions", "за + accusative: in exchange / for a price / for (someone)",
      "платить за, спасибо за, бороться за, голосовать за, выйти замуж за, купить за 100 "
      "рублей, за тебя (on your behalf / a toast to you).",
      ex=[("Спасибо за помощь.", "Thanks for the help."),
          ("Я купил это за тысячу рублей.", "I bought it for a thousand roubles.")],
      hint="a 'thank / pay / vote / fight FOR something' sentence with за + accusative"),
    C("za-acc-time-before", "b2", "prepositions", "за + accusative … до + genitive: 'X before'",
      "за час до встречи, за неделю до экзамена = an hour before the meeting, a week before "
      "the exam.",
      ex=[("Приходите за десять минут до начала.", "Come ten minutes before the start."),
          ("Он позвонил за день до отъезда.", "He called the day before leaving.")],
      hint="a 'ten minutes / a day BEFORE something' sentence with за … до"),

    # ---------------------------------------------------------------- prepositions: acc misc
    C("acc-preps-misc", "b2", "prepositions", "про / через / сквозь / несмотря на + accusative",
      "про = about (colloquial о); через = across / through / via / in [time]; сквозь = "
      "through a resisting medium (сквозь толпу, сквозь слёзы); несмотря на = despite.",
      ex=[("Расскажи мне про свой отпуск.", "Tell me about your holiday."),
          ("Несмотря на дождь, мы пошли гулять.", "Despite the rain, we went for a walk.")],
      hint="a 'through the crowd / despite the rain / about (colloq.)' sentence"),

    # ---------------------------------------------------------------- syntax: clauses
    C("chto-vs-chtoby", "b1", "syntax", "что vs чтобы",
      "что + indicative = report a fact (Я знаю, что он придёт). чтобы + past-form / "
      "infinitive = purpose or a wanted/ordered action (Я хочу, чтобы ты пришёл; Я пришёл, "
      "чтобы помочь).",
      ex=[("Скажи ему, чтобы он подождал.", "Tell him to wait."),
          ("Я уверен, что всё будет хорошо.", "I'm sure everything will be fine.")],
      traps=["After хотеть / просить / советовать with a DIFFERENT subject → чтобы + past. Same subject → чтобы + infinitive."],
      hint="a 'want / tell / know THAT / IN ORDER TO' complex sentence — pick что or чтобы"),
    C("chtoby-purpose", "b1", "syntax", "чтобы + infinitive: in order to",
      "Same-subject purpose: чтобы + infinitive. Often after motion or effort verbs.",
      ex=[("Я встал рано, чтобы успеть на поезд.", "I got up early to catch the train."),
          ("Что нужно сделать, чтобы получить визу?", "What do you need to do to get a visa?")],
      hint="an 'I did X in order to do Y' sentence with чтобы + infinitive"),
    C("reported-speech", "b1", "syntax", "Reported speech — no tense shift",
      "Russian keeps the original tense: Он сказал, что придёт завтра (he said he WOULD come "
      "— literally 'will come'). Questions use ли or the question word.",
      ex=[("Она сказала, что уже сделала домашнее задание.", "She said she had already done the homework."),
          ("Я спросил, придёт ли он.", "I asked whether he would come.")],
      traps=["No backshift: 'he said he was tired' → он сказал, что устал (or что он устал)."],
      hint="a 'he said / asked that …' reported-speech sentence"),
    C("relative-kotory", "b1", "syntax", "который — relative clauses",
      "который agrees in gender/number with its antecedent, but takes the CASE its own clause "
      "needs: человек, которого я видел (acc); дом, в котором мы жили (prep).",
      ex=[("Это книга, которую мне подарили.", "This is the book I was given."),
          ("Женщина, с которой ты говорил, — моя тётя.", "The woman you were talking to is my aunt.")],
      traps=["Gender from the antecedent, case from the relative clause. Preposition goes before который."],
      hint="a sentence with a который relative clause where который is in an oblique case"),
    C("relative-gde-kuda", "b1", "syntax", "где / куда / откуда / когда as relatives",
      "For places and times: город, где я родился; ресторан, куда мы ходили; день, когда мы "
      "познакомились. (Alternative to в котором etc.)",
      ex=[("Вот дом, где я вырос.", "Here's the house where I grew up."),
          ("Я помню день, когда мы встретились.", "I remember the day we met.")],
      hint="a 'the place where / the day when' relative-clause sentence"),
    C("to-chto", "b2", "syntax", "то, что / тот, кто / всё, что",
      "'the thing that', 'the one who', 'everything that' — то declines for the main clause, "
      "что/кто for the subordinate: Я думаю о том, что ты сказал; Тот, кто опоздает, "
      "останется без обеда.",
      ex=[("Спасибо за то, что помог.", "Thanks for helping."),
          ("Он не верит тому, что говорят в новостях.", "He doesn't believe what they say on the news.")],
      traps=["The preposition attaches to то: о том, что; за то, что; к тому, что."],
      hint="a 'thanks for / think about / believe THE FACT THAT' sentence with то, что"),
    C("conditional-real", "a2", "syntax", "Real conditional (если + future)",
      "A real / likely condition: если + present or future, main clause future. No 'бы'.",
      ex=[("Если будет дождь, мы останемся дома.", "If it rains, we'll stay home."),
          ("Позвони мне, если что-то случится.", "Call me if anything happens.")],
      basic=True, hint="an 'if X (really) happens, then Y' conditional"),
    C("conditional-unreal", "b1", "syntax", "Unreal conditional (если бы + past + бы)",
      "Hypothetical / counterfactual: если бы + past in BOTH clauses, plus бы in the main "
      "clause. Same form for present, past and future hypotheticals.",
      ex=[("Если бы у меня было время, я бы тебе помог.", "If I had time, I'd help you."),
          ("Если бы ты меня послушал, всё было бы иначе.", "If you'd listened to me, things would be different.")],
      traps=["Past-tense form only, in every branch: было бы, пришёл бы, знал бы. No present/future forms."],
      hint="an 'if I had … I would …' hypothetical with если бы"),
    C("kak-budto", "b2", "syntax", "как будто / будто / словно (as if)",
      "'as if / like': Он говорит так, как будто всё знает. Often with a past-form for an "
      "unreal comparison.",
      ex=[("Она посмотрела на меня, как будто впервые видела.", "She looked at me as if seeing me for the first time."),
          ("Было тихо, будто все ушли.", "It was quiet, as if everyone had left.")],
      hint="an 'as if / like' comparison sentence with как будто"),
    C("bare-conditional-particle-by", "b1", "syntax", "бы for softening / advice / wishes",
      "бы + past-form: politeness (Я бы хотел…), advice (Ты бы отдохнул), a wish (Поспать бы), "
      "hedged opinion (Я бы сказал, что…).",
      ex=[("Я бы выпил чаю.", "I'd have some tea."),
          ("Тебе бы к врачу сходить.", "You should see a doctor.")],
      hint="a softened 'I'd like / you should / if only' sentence with бы"),

    # ---------------------------------------------------------------- syntax: impersonal / existence
    C("existence-est-vs-zero", "a2", "syntax", "есть vs zero copula in 'у меня…'",
      "у меня есть X = I HAVE an X (existence). Drop есть when the noun is qualified and "
      "existence isn't the point: у меня новая машина (I have a NEW car — the point is 'new').",
      ex=[("У тебя есть машина? — Да, у меня красная машина.",
           "Do you have a car? — Yes, I have a red car."),
          ("У него хорошее чувство юмора.", "He has a good sense of humour.")],
      traps=["Existence / first mention → есть. Describing a known thing → no есть."],
      basic=True, hint="a 'у [someone] (есть) X' possession sentence — decide whether to keep есть"),
    C("possession-past-future", "a2", "syntax", "Possession in past / future (был / не было)",
      "у меня был / была / было / были X (agrees with X); negative → не было + genitive; "
      "future → будет / не будет + genitive.",
      ex=[("У нас не было выбора.", "We had no choice."),
          ("У меня завтра будет свободное время.", "I'll have free time tomorrow.")],
      traps=["Positive: был agrees (был телефон, была машина). Negative: не было + genitive always."],
      basic=True, hint="a 'someone had / didn't have / will have X' sentence"),
    C("impersonal-3pl", "b1", "syntax", "Impersonal 3rd-person plural (говорят, что…)",
      "No subject, verb in 3rd-person plural = 'they / people / one': Говорят, что будет "
      "дождь. Здесь строят новый мост. Мне сказали подождать.",
      ex=[("В России пьют много чая.", "In Russia people drink a lot of tea."),
          ("Тебя просят перезвонить.", "You're being asked to call back.")],
      hint="a 'they say / people do / you're being asked' subjectless 3rd-plural sentence"),
    C("passive-sya-vs-participle", "b2", "syntax", "-ся passive vs short past passive participle",
      "Imperfective / process passive → -ся: дом строится, вопрос обсуждается. Perfective / "
      "result passive → short participle: дом построен, вопрос решён, книга написана.",
      ex=[("Этот мост был построен в прошлом веке.", "This bridge was built last century."),
          ("Магазин закрыт на ремонт.", "The shop is closed for repairs.")],
      traps=["Ongoing → -ся (обсуждается). Done, with a result → short participle (обсуждён)."],
      hint="a passive sentence — ongoing process (-ся) or completed result (short participle)"),
    C("stoit-inf", "b2", "syntax", "стоит / стоило + infinitive (it's worth / just have to)",
      "стоит + inf = it's worth doing / one should; стоит только … как = 'the moment you …'; "
      "не стоит = don't bother / no need.",
      ex=[("Тебе стоит извиниться.", "You ought to apologise."),
          ("Не стоит из-за этого расстраиваться.", "It's not worth getting upset over this.")],
      hint="an 'it's worth / you ought to / no need to' sentence with стоит + infinitive"),
    C("dolzhen-vs-prihoditsya", "b2", "syntax", "должен / нужно / приходится / вынужден",
      "должен = obligation / expectation; нужно / надо = need; приходится (+ dat + impf inf) = "
      "'have to / end up having to' (against one's wish); вынужден = forced to.",
      ex=[("Мне приходится рано вставать.", "I have to get up early (like it or not)."),
          ("Ты не должен так со мной разговаривать.", "You shouldn't talk to me like that.")],
      hint="a 'have to / am forced to / ought to' obligation sentence — pick the right one"),

    # ---------------------------------------------------------------- syntax: comparison / negation
    C("comparison-constructions", "b1", "syntax", "чем / в … раз / всё + comparative",
      "чем + nominative (X больше, чем Y); в два раза / вдвое больше; на + acc (старше на "
      "год); всё + comparative ('more and more').",
      ex=[("Он зарабатывает в два раза больше меня.", "He earns twice as much as me."),
          ("Становится всё холоднее.", "It's getting colder and colder.")],
      hint="a 'twice as much / older by a year / more and more' comparison sentence"),
    C("negation-ne-vs-ni", "b1", "syntax", "не vs ни",
      "не = plain negation of one word. ни = 'not a single' / reinforces a negative / in "
      "'neither…nor' (ни…ни) / in concessives (кто бы ни, что бы ни).",
      ex=[("Он не сказал ни слова.", "He didn't say a single word."),
          ("Ни он, ни я не знали ответа.", "Neither he nor I knew the answer.")],
      hint="a 'not a single / neither…nor' sentence using ни"),
    C("ne-tolko-no-i", "b1", "syntax", "Correlative conjunctions (не только … но и, как … так и)",
      "не только X, но и Y; как X, так и Y; то ли … то ли; либо … либо; ни X ни Y.",
      ex=[("Он говорит не только по-русски, но и по-китайски.",
           "He speaks not only Russian but also Chinese."),
          ("Как взрослые, так и дети были в восторге.", "Both adults and children were delighted.")],
      hint="a 'not only … but also / both … and' correlative sentence"),
    C("genitive-of-negation", "b2", "syntax", "Genitive of negation (optional, of a direct object)",
      "A negated direct object may go genitive instead of accusative: Я не читал этой книги. "
      "Genitive is stronger / more abstract / for non-existence; accusative keeps a specific "
      "thing in view. Obligatory with нет / не было and with некоторые verbs.",
      ex=[("Я не понимаю твоего вопроса.", "I don't understand your question."),
          ("Он не любит громкой музыки.", "He doesn't like loud music.")],
      traps=["Я не видел фильм (a specific film) vs Я не видел фильма (didn't see any of it / it at all)."],
      hint="a 'I don't [verb] X' sentence where the negated object goes genitive"),

    # ---------------------------------------------------------------- participles & gerunds
    C("participle-short-passive", "b1", "participles", "Short past passive participle as predicate",
      "-н / -т ending, agrees like a short adjective, means 'has been Xed / is Xed': дверь "
      "закрыта, работа сделана, всё готово, город основан в …",
      ex=[("Магазин уже закрыт.", "The shop is already closed."),
          ("Письмо было написано вчера.", "The letter was written yesterday.")],
      traps=["написан / написана / написано / написаны — agree with the subject."],
      hint="a 'the X is/was closed / written / done / built' short-participle sentence"),
    C("participle-active", "b2", "participles", "Active participles (-ущий / -вший)",
      "Replace a который-clause where который is the SUBJECT: студент, который читает → "
      "читающий студент; человек, который позвонил → позвонивший человек. Bookish.",
      ex=[("Люди, живущие в этом доме, очень дружелюбные.", "The people living in this building are very friendly."),
          ("Мы нашли документы, подтверждающие оплату.", "We found documents confirming payment.")],
      hint="a sentence with a present/past active participle replacing a который clause"),
    C("participle-passive-long", "c1", "participles", "Long-form passive participles (-нный / -мый)",
      "прочитанная книга, любимый фильм, обсуждаемый вопрос, построенный дом — as full "
      "attributive adjectives.",
      ex=[("Это самый обсуждаемый фильм года.", "It's the most talked-about film of the year."),
          ("На столе лежала недописанная статья.", "An unfinished article lay on the desk.")],
      hint="a phrase with a long-form passive participle modifying a noun"),
    C("gerund-imperfective", "b2", "gerunds", "Imperfective gerund (-я): 'while doing'",
      "Simultaneous action, same subject: Он шёл по улице, разговаривая по телефону. Читая, "
      "она делала заметки. From the 3rd-pl present: читают → читая, говорят → говоря.",
      ex=[("Не разговаривай, пожалуйста, за рулём.", "Please don't talk while driving."),
          ("Улыбаясь, он протянул мне руку.", "Smiling, he held out his hand.")],
      traps=["Same subject as the main verb. быть → будучи; irregular: давая, узнавая."],
      hint="a 'while doing X, [subject] did Y' sentence with an imperfective gerund"),
    C("gerund-perfective", "b2", "gerunds", "Perfective gerund (-в / -вши): 'having done'",
      "A completed prior action, same subject: Закончив работу, он пошёл домой. Приехав, "
      "позвони мне.",
      ex=[("Позавтракав, я сел за работу.", "Having had breakfast, I sat down to work."),
          ("Выйдя из дома, она вспомнила про зонт.", "Having left the house, she remembered the umbrella.")],
      hint="a 'having done X, [subject] then did Y' sentence with a perfective gerund"),

    # ---------------------------------------------------------------- particles & discourse
    C("particle-zhe", "b2", "particles", "же — emphasis, contrast, 'same'",
      "Emphasises / shows impatience or contrast (Куда же ты? Я же говорил!); marks "
      "sameness (тот же, там же, в тот же день).",
      ex=[("Ты же обещал!", "But you promised!"),
          ("Мы приехали в тот же день.", "We arrived the same day.")],
      hint="a sentence where же adds 'but you…! / the very same'"),
    C("particle-ved", "b2", "particles", "ведь — 'after all / you know'",
      "Appeals to shared knowledge or gives a reason: Возьми зонт, ведь идёт дождь. Ты ведь "
      "знаешь его.",
      ex=[("Не волнуйся, ведь это не твоя вина.", "Don't worry — it's not your fault, after all."),
          ("Ведь я тебя предупреждал.", "I did warn you, you know.")],
      hint="a sentence where ведь means 'after all / you know'"),
    C("particle-li", "b1", "syntax", "ли — indirect yes/no questions & 'whether'",
      "In reported / embedded yes-no questions: Я не знаю, придёт ли он. Also for a hesitant "
      "direct question: Не пойти ли нам домой?",
      ex=[("Спроси, открыт ли ещё магазин.", "Ask whether the shop is still open."),
          ("Я не уверен, стоит ли это делать.", "I'm not sure whether it's worth doing.")],
      traps=["ли goes right after the word being questioned, which moves to the front: придёт ли он."],
      hint="an 'I don't know / ask WHETHER …' embedded question with ли"),
    C("particle-razve-neuzheli", "b2", "particles", "разве / неужели — surprise / disbelief",
      "разве = 'really? / you mean to say?' (mild challenge); неужели = 'surely not? / can it "
      "be?' (stronger surprise).",
      ex=[("Неужели ты не помнишь?", "Surely you remember?"),
          ("Разве сегодня не воскресенье?", "Isn't today Sunday?")],
      hint="a surprised / incredulous question with разве or неужели"),

    # ---------------------------------------------------------------- verbal government (dedicated)
    C("gov-zvonit-pomogat-dat", "b1", "government", "звонить / помогать / мешать / советовать + dative",
      "These take a bare dative for the person: звоню маме, помогаю другу, мешаю соседям, "
      "советую тебе.",
      ex=[("Я позвоню тебе завтра.", "I'll call you tomorrow."),
          ("Врач посоветовал ему отдохнуть.", "The doctor advised him to rest.")],
      hint="a 'call / help / bother / advise someone' sentence — the person is dative"),
    C("gov-boyatsya-gen", "b1", "government", "бояться + genitive",
      "бояться + genitive of what you fear (бояться собак, темноты, ошибиться). Colloquially "
      "also + accusative for a specific person.",
      ex=[("Она боится летать на самолёте.", "She's afraid of flying."),
          ("Не бойся трудностей.", "Don't be afraid of difficulties.")],
      hint="a 'be afraid of X' sentence with бояться + genitive"),
    C("gov-zanimatsya-instr", "b1", "government", "заниматься + instrumental",
      "заниматься спортом / музыкой / наукой / детьми = do / be busy with / study.",
      ex=[("По вечерам я занимаюсь английским.", "In the evenings I study English."),
          ("Чем ты сейчас занимаешься?", "What are you doing right now?")],
      traps=["заниматься + instrumental, never 'заниматься на спорт'."],
      hint="a 'do / be busy with / study X' sentence with заниматься"),
    C("gov-interesovatsya-instr", "b1", "government", "интересоваться / увлекаться + instrumental",
      "интересоваться историей, увлекаться фотографией. Related noun phrase: интерес к + "
      "dative.",
      ex=[("Он увлекается шахматами с детства.", "He's been into chess since childhood."),
          ("Я всегда интересовался астрономией.", "I've always been interested in astronomy.")],
      hint="a 'be interested in / be into X' sentence with интересоваться / увлекаться"),
    C("gov-polzovatsya-instr", "b1", "government", "пользоваться + instrumental",
      "пользоваться словарём / общественным транспортом / успехом / популярностью.",
      ex=[("Можно воспользоваться вашим телефоном?", "May I use your phone?"),
           ("Этот бар пользуется популярностью у студентов.", "This bar is popular with students.")],
      hint="a 'use / take advantage of X' sentence with пользоваться"),
    C("gov-zaviset-otkazatsya-ot-gen", "b1", "government", "зависеть / отказываться от + genitive",
      "зависеть от + gen (depend on), отказываться от + gen (refuse / give up), избавиться "
      "от + gen (get rid of).",
      ex=[("Всё зависит от погоды.", "It all depends on the weather."),
          ("Он отказался от нашей помощи.", "He turned down our help.")],
      hint="a 'depend on / refuse / give up X' sentence with … от + genitive"),
    C("gov-privykat-gotovitsya-k-dat", "b1", "government", "привыкать / готовиться / относиться к + dative",
      "привыкать к + dat (get used to), готовиться к + dat (prepare for), относиться к + dat "
      "(treat / relate to), стремиться к + dat (strive for).",
      ex=[("Я никак не привыкну к холоду.", "I just can't get used to the cold."),
          ("Как ты относишься к его идее?", "What do you think of his idea?")],
      hint="a 'get used to / prepare for / feel about X' sentence with … к + dative"),
    C("gov-nadeyatsya-na-acc", "b1", "government", "надеяться / рассчитывать / влиять на + accusative",
      "надеяться на + acc (hope for / rely on), рассчитывать на + acc (count on), влиять на "
      "+ acc (affect), обращать внимание на + acc (pay attention to).",
      ex=[("Я надеюсь на лучшее.", "I'm hoping for the best."),
          ("Погода сильно влияет на моё настроение.", "The weather really affects my mood.")],
      hint="a 'hope for / count on / affect / pay attention to X' sentence with … на + accusative"),
    C("gov-zhalovatsya-na-acc", "b1", "government", "жаловаться / сердиться на + accusative",
      "жаловаться на + acc (complain about — a problem or a person), сердиться / злиться на "
      "+ acc (be angry at), обижаться на + acc (be hurt by).",
      ex=[("Он всё время жалуется на здоровье.", "He complains about his health all the time."),
          ("Не сердись на меня.", "Don't be angry with me.")],
      hint="a 'complain about / be angry at someone' sentence with … на + accusative"),
    C("gov-dumat-o-nad", "b1", "government", "думать о + prep / над + instr",
      "думать о + prep = think about (have in mind); думать над + instr = ponder / work on "
      "(a problem). Also мечтать о + prep, заботиться о + prep.",
      ex=[("Я весь день думаю о тебе.", "I've been thinking about you all day."),
          ("Она долго думала над ответом.", "She thought hard about her answer.")],
      hint="a 'think about / ponder / dream about X' sentence — о + prep or над + instr"),
    C("gov-skuchat-po", "b1", "government", "скучать / тосковать по + dative",
      "скучать по + dative (по дому, по друзьям); with pronouns often + prepositional (по "
      "тебе, по вас).",
      ex=[("Я очень скучаю по своей семье.", "I really miss my family."),
          ("Она скучает по тем временам.", "She misses those times.")],
      hint="a 'miss home / miss someone' sentence with скучать по"),
    C("gov-rabotat-smeyatsya-nad-instr", "b1", "government", "работать / смеяться над + instrumental",
      "работать над + instr (work on a project / oneself), смеяться над + instr (laugh at / "
      "mock), издеваться над + instr, задуматься над + instr.",
      ex=[("Он сейчас работает над новой книгой.", "He's working on a new book now."),
          ("Не смейся надо мной!", "Don't laugh at me!")],
      hint="a 'work on / laugh at X' sentence with … над + instrumental"),
    C("gov-uchastvovat-v-prep", "b1", "government", "участвовать / нуждаться в + prepositional",
      "участвовать в + prep (take part in), нуждаться в + prep (need — formal), "
      "сомневаться в + prep (doubt), убедиться в + prep, признаться в + prep.",
      ex=[("Мы участвовали в конкурсе.", "We took part in the competition."),
          ("Он нуждается в помощи.", "He needs help.")],
      hint="a 'take part in / need / doubt X' sentence with … в + prepositional"),
    C("gov-pozdravlyat-s-instr", "b1", "government", "поздравлять с + instrumental",
      "поздравлять с + instr (праздником, днём рождения, успехом); соглашаться с + instr; "
      "спорить с + instr; знакомиться с + instr; справляться с + instr (cope with).",
      ex=[("Поздравляю тебя с днём рождения!", "Happy birthday!"),
          ("Он не справился с этой задачей.", "He couldn't cope with this task.")],
      hint="a 'congratulate on / agree with / cope with X' sentence with … с + instrumental"),
    C("gov-blagodarit-izvinyatsya-za", "b1", "government", "благодарить / извиняться за + accusative",
      "благодарить (кого) за (что) + acc; извиняться / просить прощения за + acc; "
      "отвечать за + acc (be responsible for); наказать за + acc.",
      ex=[("Извини за опоздание.", "Sorry for being late."),
          ("Кто отвечает за этот проект?", "Who's in charge of this project?")],
      hint="a 'thank / apologise / be responsible FOR something' sentence with за + accusative"),
    C("gov-sprashivat-prosit", "b1", "government", "спрашивать у / просить + genitive / о + prep",
      "спрашивать у кого-то (ask a person), спрашивать о ком/чём (ask about); просить у кого "
      "(что / чего) or просить кого о чём (ask someone for something).",
      ex=[("Спроси у мамы, где ключи.", "Ask mum where the keys are."),
          ("Он попросил меня о помощи.", "He asked me for help.")],
      hint="an 'ask someone / ask FOR / ask ABOUT something' sentence"),
    C("gov-zhenitsya-vyjti-zamuzh", "b1", "government", "жениться на + prep / выйти замуж за + acc",
      "A man: жениться на + prepositional (женился на Анне). A woman: выйти замуж за + "
      "accusative (вышла замуж за Ивана). Both: пожениться (they got married).",
      ex=[("Он женился на своей однокласснице.", "He married his classmate."),
          ("Она вышла замуж за врача.", "She married a doctor.")],
      hint="a 'married someone' sentence — жениться на or выйти замуж за"),

    # ---------------------------------------------------------------- noun morphology
    C("noun-spelling-y-i", "a2", "nouns", "Spelling rule: и not ы after к г х ж ш щ ч",
      "After velars (к г х) and hushers (ж ш щ ч) write и, never ы: книги, ноги, мыши, "
      "карандаши. Affects plurals and genitive singular.",
      ex=[("У меня две книги и три ручки.", "I have two books and three pens."),
          ("Мы купили подарки для друзей.", "We bought presents for our friends.")],
      basic=True, hint="a plural / genitive form where the spelling rule forces -и after к/г/х/ж/ш/щ/ч"),
    C("noun-fleeting-vowel", "b1", "nouns", "Fleeting vowels (отец → отца, день → дня)",
      "A last-syllable -о-/-е-/-ё- often drops when an ending is added: отец/отца, день/дня, "
      "кусок/куска, окно/окон (appears in gen pl), подарок/подарка.",
      ex=[("Это подарок для моего отца.", "This is a present for my father."),
          ("Он работает семь дней в неделю.", "He works seven days a week.")],
      traps=["Also appears (not drops) in the zero-ending gen pl: окно → много окон, письмо → писем."],
      hint="a phrase in an oblique case with a fleeting-vowel noun (отец, день, кусок…)"),
    C("noun-neuter-mya", "b1", "nouns", "The -мя neuter nouns (имя, время, знамя)",
      "имя, время, знамя, племя, семя, бремя, пламя: oblique stem in -ен- (имени, временем, "
      "о времени; pl имена, времена).",
      ex=[("Как твоё полное имя?", "What's your full name?"),
          ("У меня совсем нет времени.", "I have no time at all.")],
      hint="a sentence with имя / время in an oblique case (имени, временем, времени)"),
    C("noun-nom-pl-a", "b1", "nouns", "Nominative plural in -а / -я (города, учителя)",
      "A set of masc nouns take stressed -а/-я in the nom pl: город→города, дом→дома, "
      "глаз→глаза, учитель→учителя, паспорт→паспорта, поезд→поезда, номер→номера.",
      ex=[("В этом районе строят новые дома.", "New houses are being built in this area."),
          ("Все берега реки заросли камышом.", "Both banks of the river are overgrown with reeds.")],
      hint="a plural sentence with a -а/-я nom-pl noun (дома, города, учителя, глаза…)"),
    C("noun-gen-pl", "b1", "nouns", "Genitive plural endings (zero / -ов / -ей)",
      "Masc → -ов/-ев (столов, музеев) or -ей after ж/ш/ч/щ/ь (карандашей, гостей). Fem/neut "
      "→ zero, often with a fleeting vowel (книг, окон, писем, девушек) or -ей (ночей, "
      "дверей). Irregular: людей, детей, друзей, братьев.",
      ex=[("В городе много старых зданий.", "There are many old buildings in the city."),
          ("У неё пять братьев и две сестры.", "She has five brothers and two sisters.")],
      traps=["This is where '5+ of X' lands, so it comes up constantly with numbers."],
      hint="a '[5+ / много / несколько] of X' sentence forcing the genitive plural"),
    C("noun-indeclinable", "a2", "nouns", "Indeclinable nouns (кофе, метро, пальто, такси)",
      "Loanwords ending in -о/-е/-и/-у don't change: в метро, без пальто, два кофе, на такси. "
      "Gender: mostly neuter (метро — оно), but кофе is masc, and people-nouns follow the "
      "person (месье, леди).",
      ex=[("Мы поехали домой на такси.", "We took a taxi home."),
          ("Он заказал два капучино.", "He ordered two cappuccinos.")],
      basic=True, hint="a sentence with an indeclinable noun in what would be an oblique case"),
    C("noun-adjectival", "b1", "nouns", "Adjectival nouns (столовая, мороженое, будущее)",
      "Nouns that decline like adjectives: столовая (canteen), гостиная, ванная, мороженое, "
      "будущее, прошлое, животное, учёный, рабочий, полицейский.",
      ex=[("Встретимся в столовой.", "Let's meet in the canteen."),
          ("Дети любят мороженое.", "Kids love ice cream.")],
      hint="a sentence with an adjectival noun in an oblique case (в столовой, без мороженого…)"),

    # ---------------------------------------------------------------- time expressions
    C("time-v-acc-days-clock", "a2", "time", "в + accusative: days and clock times",
      "в понедельник, во вторник, в среду; в час, в два часа, в пять часов; в этот момент. "
      "'at' a day or an hour.",
      ex=[("Урок начинается в девять часов.", "The lesson starts at nine."),
          ("Приходи в субботу.", "Come on Saturday.")],
      basic=True, hint="an 'on [day] / at [o'clock]' time sentence with в + accusative"),
    C("time-v-prep-months-years", "a2", "time", "в + prepositional: months, years, decades",
      "в мае, в январе; в 2020 году, в этом году; в двадцатом веке; в детстве, в молодости. "
      "'in' a longer span.",
      ex=[("Мой день рождения в апреле.", "My birthday is in April."),
          ("В детстве я жил в деревне.", "As a child I lived in a village.")],
      basic=True, hint="an 'in [month] / in [year] / in childhood' time sentence with в + prepositional"),
    C("time-na-week-period", "b1", "time", "на + prepositional / accusative: weeks & planned spans",
      "на этой неделе, на прошлой неделе, на выходных (location-style 'in that week'). на + "
      "accusative for how long a state / plan will LAST: уехать на месяц, взять книгу на "
      "неделю, опоздать на десять минут.",
      ex=[("На следующей неделе у меня отпуск.", "Next week I'm on holiday."),
          ("Он уехал в командировку на три дня.", "He's away on a business trip for three days.")],
      traps=["на неделю (going away FOR a week) vs неделю (the action lasted a week) vs за неделю (got it done within a week)."],
      hint="a 'this/next week' or 'going away FOR N days' sentence with на"),
    C("time-do-posle-k", "a2", "time", "до / после / к + a time point",
      "до + gen (until, before): до обеда, до пяти. после + gen (after): после работы. к + "
      "dat (by, towards): к вечеру, к понедельнику.",
      ex=[("Давай закончим до обеда.", "Let's finish before lunch."),
          ("Я вернусь к шести.", "I'll be back by six.")],
      basic=True, hint="a 'before / after / by [a time]' sentence with до / после / к"),
    C("time-s-do-ot-do", "a2", "time", "с … до / от … до: from … to (a span)",
      "с понедельника до пятницы, с девяти до шести, от начала до конца. Both ends genitive.",
      ex=[("Магазин работает с восьми до двадцати двух.", "The shop is open from 8 to 22."),
          ("Он проспал с обеда до вечера.", "He slept from lunchtime till evening.")],
      hint="a 'from [time] to [time]' span sentence with с … до"),
    C("time-instr-parts-of-day", "a2", "time", "Instrumental time (утром, зимой) — see also instr-time",
      "The bare instrumental for recurring / general times of day and seasons: ранним утром, "
      "поздней ночью, прошлым летом, этой весной.",
      ex=[("Прошлым летом мы были на море.", "Last summer we were at the seaside."),
          ("Поздним вечером позвонил старый друг.", "Late in the evening an old friend called.")],
      basic=True, hint="a 'last summer / early in the morning / this spring' time sentence"),
    C("time-davno-nadolgo", "b2", "time", "давно / долго / надолго / недавно",
      "давно = long ago / for a long time up to now (Я давно его знаю). долго = for a long "
      "time (a completed or ongoing stretch). надолго = for a long time INTO the future "
      "(уехал надолго). недавно = recently.",
      ex=[("Ты здесь давно?", "Have you been here long?"),
          ("Он уехал надолго.", "He's gone away for a long time.")],
      traps=["Я долго ждал (the wait lasted long) vs Я давно жду (I've been waiting since long ago)."],
      hint="a 'for a long time / long ago / for long (ahead)' sentence — pick давно / долго / надолго"),
    C("time-v-techenie", "b2", "time", "в течение + gen vs за + acc (over a period)",
      "в течение + gen = throughout / over the course of (в течение недели шли дожди). за + "
      "acc = within which a result was achieved (за неделю он всё сделал).",
      ex=[("В течение года компания выросла вдвое.", "Over the year the company doubled in size."),
          ("Я прочитал книгу за выходные.", "I read the book over the weekend.")],
      hint="an 'over the course of / within [a period]' sentence — в течение + gen or за + acc"),

    # ---------------------------------------------------------------- C1 / C2
    C("aspect-biaspectual", "c1", "aspect", "Bi-aspectual verbs (использовать, женить…)",
      "Some verbs (mostly -овать loans and a few native ones) serve as BOTH aspects — "
      "context / adverbs decide: использовать, организовать, исследовать, гарантировать, "
      "телеграфировать, ранить, обещать, велеть, женить(ся), казнить.",
      ex=[("Вчера он организовал всю поездку за час.", "Yesterday he organised the whole trip in an hour (pf)."),
          ("Он часто организовывал такие встречи.", "He often organised such meetings (a derived impf).")],
      hint="a sentence with a bi-aspectual verb where context has to fix the aspect"),
    C("aspect-delimitative-po", "c1", "aspect", "по- + verb: 'do a bit of / for a while'",
      "The delimitative prefix по- turns an imperfective into a perfective meaning 'do X for "
      "a short while': поработать, погулять, почитать, посидеть, подумать. Also пере- "
      "('re-do'), под- ('do a little more'), за- ('start').",
      ex=[("Давай посидим ещё немного.", "Let's sit a bit longer."),
          ("Мне надо подумать.", "I need to think it over.")],
      hint="a 'do X for a little while / a bit' sentence with a по-delimitative verb"),
    C("gen-negation-obligatory", "c1", "syntax", "When the genitive of negation is obligatory",
      "Genitive (not accusative) is forced: with нет / не было / не будет; with negated "
      "'perception / possession' verbs (не видел, не знает, не имеет) of an abstract or "
      "indefinite object; in не оставалось / не находилось constructions.",
      ex=[("Я не имею ни малейшего представления.", "I have not the slightest idea."),
          ("У него не оказалось при себе документов.", "He turned out not to have any documents on him.")],
      hint="a strongly-negated sentence where the object MUST be genitive"),
    C("syn-ustupitelnoe", "c1", "syntax", "Concessive clauses (хотя, несмотря на то что, как ни)",
      "хотя / несмотря на то, что = although; как ни / сколько ни / что ни + verb = "
      "'however much …', 'no matter what …'; пусть / пускай (even though).",
      ex=[("Как я ни старался, у меня не получилось.", "However hard I tried, I didn't manage it."),
          ("Несмотря на то что было поздно, магазин ещё работал.",
           "Even though it was late, the shop was still open.")],
      hint="a 'however hard I tried / even though …' concessive sentence"),
    C("syn-s-tem-chtoby", "c1", "syntax", "Bookish purpose & result (с тем чтобы, для того чтобы, так что)",
      "для того чтобы / с тем чтобы = in order that (formal чтобы); так что = 'and so' "
      "(result); вследствие чего; в результате чего.",
      ex=[("Он говорил медленно, с тем чтобы все поняли.", "He spoke slowly so that everyone would understand."),
          ("Дорогу перекрыли, так что нам пришлось объезжать.",
           "The road was closed, so we had to take a detour.")],
      hint="a formal 'in order that / and so' purpose-or-result sentence"),
    C("syn-nominalization", "c1", "syntax", "Nominalisation (verbal nouns + genitive chains)",
      "Formal Russian packs clauses into noun phrases: строительство дороги, повышение цен, "
      "в связи с отсутствием, после рассмотрения заявления. Chains of genitives.",
      ex=[("Причиной задержки стало отсутствие финансирования.",
           "The reason for the delay was a lack of funding."),
          ("После завершения строительства парк открыли для публики.",
           "After construction was completed the park opened to the public.")],
      hint="a formal sentence built on a verbal noun + a genitive chain"),
    C("part-obosoblenie", "c1", "participles", "Punctuating participial & gerund phrases",
      "A participial phrase AFTER its noun, and any gerund phrase, is set off by commas: "
      "Книга, лежавшая на столе, исчезла. Закончив работу, он ушёл. A short participial "
      "phrase before the noun is not.",
      ex=[("Студенты, сдавшие экзамен, могут идти домой.", "Students who passed the exam may go home."),
          ("Не зная адреса, мы долго искали дом.", "Not knowing the address, we looked for the house a long time.")],
      hint="a sentence with a comma-set-off participial or gerund phrase"),
    C("part-passive-formation", "c1", "participles", "Forming past passive participles (-нн / -енн / -т)",
      "-ать verbs → -анн (прочитанный); -ить / -еть → -енн with mutation (купить→купленный, "
      "решить→решённый, встретить→встреченный); monosyllable / -нуть / -оть → -т "
      "(открытый, забытый, спетый).",
      ex=[("Все билеты уже проданы.", "All the tickets are already sold."),
          ("Дверь была заперта изнутри.", "The door was locked from the inside.")],
      hint="a sentence with a correctly-formed past passive participle"),
    C("ger-formation-pitfalls", "c1", "gerunds", "Gerund formation gaps & irregulars",
      "Many common verbs have no imperfective gerund (писать, пить, ждать, петь, мочь, "
      "бежать — use a clause instead). Irregulars: быть→будучи, давать→давая, "
      "-ся keeps -сь (улыбаясь).",
      ex=[("Будучи студентом, он подрабатывал в кафе.", "Being a student, he worked part-time in a café."),
          ("Она ушла, ничего не сказав.", "She left without saying anything.")],
      hint="a sentence with будучи or a -сь gerund, or one that must avoid a missing gerund"),
    C("particle-to-topic", "c1", "particles", "-то as a topic / contrast marker",
      "Postposed -то highlights the topic, often with a 'but as for …' contrast: Я-то "
      "думал, ты знаешь. Денег-то у нас нет. Сегодня-то он придёт.",
      ex=[("Ты-то откуда знаешь?", "And how do YOU know?"),
          ("Эту книгу я читал, а вот ту — нет.", "This book I've read, but that one — no.")],
      hint="a sentence where postposed -то marks a contrastive topic"),
    C("syn-sequence-reported", "c1", "syntax", "Layered reported speech & mixed clauses",
      "Deep embedding keeps every verb in its original tense and often stacks что / чтобы / "
      "ли: Он сказал, что не знал, придёт ли она и стоит ли её ждать.",
      ex=[("Я не был уверен, понял ли он, что я имел в виду.",
           "I wasn't sure whether he'd understood what I meant."),
          ("Она спросила, не думаю ли я, что нам пора уходить.",
           "She asked whether I didn't think it was time for us to leave.")],
      hint="a multiply-embedded reported-speech sentence with ли / что / чтобы"),
    C("register-colloquial-vs-formal", "c2", "syntax", "Register: colloquial vs bookish equivalents",
      "The same idea has a neutral, a colloquial and a bookish form: из-за того что / "
      "потому что / вследствие того что; сейчас / щас / в настоящее время; про / о / "
      "относительно; надо / нужно / необходимо / следует.",
      ex=[("Ввиду сложившихся обстоятельств встреча отменяется.",
           "In view of the circumstances the meeting is cancelled (bookish)."),
          ("Раз такое дело, давай перенесём.", "Since that's how it is, let's reschedule (colloquial).")],
      hint="a sentence that has to hit a specific register — bookish or colloquial"),
    C("aspect-perfective-present-habitual", "c2", "aspect", "Perfective 'present' for a typical action",
      "A perfective present-tense form can express a recurring TYPICAL sequence: Он придёт, "
      "сядет, ничего не скажет. Also in 'бывает, что…' and vivid narration.",
      ex=[("Иногда посмотришь на него — и всё понятно.", "Sometimes you (just) look at him and everything's clear."),
          ("Он то придёт, то уйдёт — не поймёшь.", "He comes and goes — you can't make sense of it.")],
      hint="a 'typical sequence' sentence using a perfective present form for habit"),
    C("num-both-halves-decline", "c1", "numerals", "Compound numbers: every part declines",
      "In к двумстам пятидесяти рублям / о трёх тысячах четырёхстах человеках every element "
      "takes the case. Hundreds: двести/двухсот/двумстам/двумястами/двухстах.",
      ex=[("Речь идёт о более чем пятистах участниках.", "We're talking about more than five hundred participants."),
          ("К тысяче девятистам добавили ещё сотню.", "Another hundred was added to the nineteen hundred.")],
      hint="a sentence with a large compound number in an oblique case"),
    C("gov-abstract-formal", "c1", "government", "Government of formal / abstract verbs",
      "способствовать + dat, содействовать + dat, препятствовать + dat, руководствоваться + "
      "instr, обладать + instr, располагать + instr ('have at one's disposal'), являться + "
      "instr, служить + instr, свидетельствовать о + prep, подлежать + dat.",
      ex=[("Эти меры способствуют развитию экономики.", "These measures promote economic growth."),
          ("Компания располагает всеми необходимыми ресурсами.", "The company has all the necessary resources.")],
      hint="a formal sentence whose verb governs a non-obvious case (способствовать + dat, etc.)"),

    # ================================================================
    # Added 2026-09-03 — a deeper pass, weighted to B2 / C1 / C2
    # ================================================================

    # ---------------------------------------------------------------- B1 core misses
    C("verb-position-vs-placement", "b1", "verbs", "Position vs placement verbs (стоять/ставить…)",
      "Four pairs: стоять — ставить/поставить (upright), лежать — класть/положить (flat), "
      "сидеть — сажать/посадить, висеть — вешать/повесить. State verbs take в/на + "
      "prepositional; placement verbs take в/на + accusative.",
      ex=[("Книга лежит на столе. — Положи книгу на стол.", "The book is lying on the table. — Put the book on the table."),
          ("Ваза стоит в шкафу. — Поставь вазу в шкаф.", "The vase is in the cupboard. — Put the vase in the cupboard.")],
      traps=["класть (impf) but положить (pf) — never 'ложить'. Placement = motion = accusative."],
      hint="a 'X is lying/standing/hanging somewhere' or 'put X there' sentence — pick the verb + case"),
    C("cause-ot-gen", "b1", "prepositions", "от + genitive: involuntary cause / reaction",
      "A physical or emotional reaction you don't control: дрожать от холода, кричать от боли, "
      "плакать от счастья, устать от работы, умереть от голода, покраснеть от стыда.",
      ex=[("Она заплакала от радости.", "She burst into tears of joy."),
          ("Я весь дрожу от холода.", "I'm shivering all over from the cold.")],
      traps=["от + gen for an involuntary reaction; из-за + gen for an external circumstance; "
             "по + dat for a minor fault (по ошибке)."],
      hint="a 'shaking / crying / tired FROM [cause]' reaction sentence with от + genitive"),
    C("s-gen-since", "b1", "prepositions", "с + genitive: 'since / from' a starting point",
      "с детства, с утра, с понедельника, с первого взгляда, со временем, с тех пор, с "
      "рождения. (Opposite of до.)",
      ex=[("Я знаю его с детства.", "I've known him since childhood."),
          ("Со временем всё наладится.", "In time everything will work out.")],
      traps=["с + gen 'since'; из/с/от + gen 'from a place/person' is a different use."],
      hint="a 'since childhood / from morning / with time' starting-point sentence with с"),
    C("na-acc-purpose", "b1", "prepositions", "на + accusative: purpose / allocation / a ticket for",
      "деньги на еду, время на отдых, разрешение на въезд, билет на поезд / на концерт, "
      "экзамен на права, право на ошибку, ответ на вопрос, на всякий случай.",
      ex=[("У меня нет денег на такси.", "I don't have money for a taxi."),
          ("Купи два билета на поезд.", "Buy two train tickets.")],
      hint="a 'money / time / a ticket / permission FOR something' sentence with на + accusative"),
    C("paren-vvodnye", "b1", "syntax", "Parenthetical words (кажется, конечно, по-моему…)",
      "Set off by commas, not part of the sentence structure: кажется, конечно, наверное, к "
      "сожалению, по-моему, во-первых, значит, кстати, наоборот, таким образом, честно говоря.",
      ex=[("По-моему, ты прав.", "In my opinion, you're right."),
          ("Он, кажется, уже ушёл.", "He seems to have already left.")],
      traps=["кажется as a parenthetical (comma) vs кажется as the verb (Мне кажется, что…)."],
      hint="a sentence with a comma-set-off parenthetical (кажется / конечно / по-моему / к сожалению)"),
    C("num-pol-poltora", "b1", "numerals", "пол- / полтора / полчаса",
      "полчаса, полгода, полкило, пол-литра (пол- + genitive singular, written solid; пол- "
      "before a vowel / capital / 'л' gets a hyphen). полтора (m/n) / полторы (f) + genitive "
      "singular; oblique полутора.",
      ex=[("Подожди полчаса.", "Wait half an hour."),
          ("Он опоздал на полтора часа.", "He was an hour and a half late.")],
      hint="a 'half an hour / a kilo and a half' sentence with пол- or полтора"),

    # ---------------------------------------------------------------- B2 syntax: clauses
    C("clause-time", "b2", "syntax", "Time clauses: пока / пока не / прежде чем / после того как",
      "когда (when), пока (while), пока не (until), прежде чем / до того как (before), после "
      "того как (after), с тех пор как (since), как только (as soon as), по мере того как "
      "(as / to the extent that). Aspect: пока + impf, пока не + pf, как только + pf.",
      ex=[("Подожди здесь, пока я не вернусь.", "Wait here until I get back."),
          ("Прежде чем ответить, подумай.", "Think before you answer.")],
      traps=["пока (while, impf) vs пока не (until, pf, no negation meaning). прежде чем + inf "
             "if same subject, + past if different."],
      hint="a complex 'while / until / before / after / as soon as …' time-clause sentence"),
    C("clause-cause", "b2", "syntax", "Cause clauses: так как / поскольку / из-за того что / благодаря тому что",
      "потому что (because — neutral, second position), так как / поскольку (since — can start "
      "the sentence, bookish), из-за того что (because of — negative), благодаря тому что "
      "(thanks to — positive), оттого что, ввиду того что (formal).",
      ex=[("Поскольку было поздно, мы остались дома.", "Since it was late, we stayed home."),
          ("Матч отменили из-за того, что шёл дождь.", "The match was cancelled because it was raining.")],
      traps=["потому что can't start a sentence; так как / поскольку can. Comma before что: "
             "из-за того, что."],
      hint="a 'since / because / thanks to the fact that …' cause-clause sentence"),
    C("clause-degree-result", "b2", "syntax", "Degree & result: так/такой … что, настолько … что",
      "так + verb/adverb + , что …; такой + adjective + , что …; настолько … что; до такой "
      "степени, что; такой … , чтобы (result you aim for).",
      ex=[("Он так устал, что заснул за столом.", "He was so tired he fell asleep at the table."),
          ("Это была такая скучная лекция, что все ушли.", "It was such a boring lecture that everyone left.")],
      traps=["так with a verb/adverb (так холодно), такой with an adjective+noun (такой холод)."],
      hint="a 'so … that …' / 'such a … that …' degree-and-result sentence"),
    C("chem-tem", "b2", "syntax", "чем … , тем … (the more … the more)",
      "Two comparatives, one in each clause: Чем больше я читаю, тем меньше понимаю. The "
      "'тем' clause has inverted order (тем + comparative first).",
      ex=[("Чем раньше начнёшь, тем быстрее закончишь.", "The sooner you start, the sooner you finish."),
          ("Чем дальше в лес, тем больше дров.", "The further into the forest, the more firewood (— it gets worse).")],
      hint="a 'the more X, the more Y' sentence with чем … , тем …"),
    C("num-predicate-agreement", "b2", "numerals", "Verb agreement with a quantified subject",
      "пять человек пришло (neuter sg — the group as a mass) vs пять человек пришли (plural — "
      "individuals). Plural is preferred for animate / active / definite subjects; singular "
      "for a total, an existence statement, or approximation.",
      ex=[("На столе лежало пять книг.", "There were five books on the table."),
          ("Пять студентов уже сдали экзамен.", "Five students have already passed the exam.")],
      traps=["Existence / 'there was' → singular neuter. Definite people doing something → plural."],
      hint="a '[number] of X [verb-ed]' sentence — pick singular vs plural agreement"),
    C("khvatat-khvatit", "b2", "syntax", "хватать / хватить + genitive (to be enough)",
      "Impersonal: у меня не хватает времени / денег / опыта (impf, ongoing lack); мне не "
      "хватило духа (pf, a specific occasion); хватит! (that's enough); мне хватит (I'll have "
      "enough).",
      ex=[("Нам не хватает одного человека.", "We're one person short."),
          ("Хватит спорить!", "Stop arguing!")],
      traps=["The thing lacking is genitive; the person is dative or у + genitive."],
      hint="a 'there isn't enough X' / 'X is short' sentence with (не) хватать + genitive"),
    C("impersonal-verbs", "b2", "verbs", "Impersonal verbs (светает, тошнит, повезло, следует)",
      "No subject, verb in 3rd sg (neuter in the past): светает / темнеет / морозит (weather); "
      "меня тошнит / знобит / морозит (body); мне повезло / удалось / следует / стоит / "
      "хочется / не спится.",
      ex=[("На улице уже темнеет.", "It's already getting dark outside."),
          ("Тебе следует извиниться.", "You ought to apologise.")],
      traps=["Past is neuter singular: повезло, стемнело, меня тошнило."],
      hint="a subjectless 'it's getting dark / I feel sick / you should' impersonal-verb sentence"),
    C("reflexive-impersonal", "c1", "verbs", "Reflexive impersonal (мне не работается, как живётся)",
      "Dative experiencer + a -ся verb in 3rd sg, describing an involuntary mood / capacity: "
      "мне не спится, ему не сидится на месте, сегодня хорошо пишется, как вам здесь живётся?",
      ex=[("Что-то мне сегодня не работается.", "Somehow I just can't get any work done today."),
          ("Ему не верится, что всё кончилось.", "He can't quite believe it's over.")],
      hint="a 'somehow I can't [verb] today' involuntary-mood sentence with a dative + -ся verb"),
    C("aspect-bylo", "c1", "aspect", "The particle было (пошёл было, хотел было)",
      "Perfective past + было = an action started or intended, then cut short or reversed: Он "
      "встал было, но снова сел. Я хотел было позвонить, да передумал.",
      ex=[("Она заснула было, но её разбудил телефон.", "She had just fallen asleep when the phone woke her."),
          ("Мы собрались было уходить, но нас остановили.", "We were about to leave, but we were stopped.")],
      hint="a 'started / was about to … but then …' sentence with the было particle"),
    C("aspect-no-partner", "b2", "aspect", "Verbs with no aspect partner",
      "Some verbs are imperfective-only (состоять, отсутствовать, зависеть, значить, "
      "принадлежать, преобладать, находиться) or perfective-only (очнуться, очутиться, "
      "хлынуть, ринуться, понадобиться, состояться). Don't force a partner.",
      ex=[("Комитет состоит из пяти человек.", "The committee consists of five people."),
          ("Вдруг хлынул дождь.", "Suddenly the rain came pouring down.")],
      hint="a sentence with an imperfective-only or perfective-only verb (состоять, хлынуть, …)"),
    C("aspect-time-clause-future", "b2", "aspect", "Aspect in 'when / until' clauses about the future",
      "Когда я приду, я позвоню (pf — a single completed point) vs Когда я буду в Москве, я "
      "позвоню (impf state). Пока не + pf (жди, пока он не придёт).",
      ex=[("Как только он вернётся, дай мне знать.", "As soon as he gets back, let me know."),
          ("Пока ты будешь готовить, я накрою на стол.", "While you cook, I'll set the table.")],
      hint="a future 'when / as soon as / while / until …' sentence — pick the aspect in the clause"),
    C("adj-possessive", "b2", "adjectives", "Possessive adjectives (мамин, папин, лисий, Пушкинский)",
      "From people: -ин / -ов (мамин, папин, дядина, Сашина). From animals: -ий/-ья/-ье "
      "(лисий, собачья, медвежье). From names: -ский (пушкинский стиль). Mixed / special "
      "declension for the -ин / -ий types.",
      ex=[("Это мамина сумка.", "That's mum's bag."),
          ("Мы шли по лисьим следам.", "We followed fox tracks.")],
      traps=["лисий: лисья, лисье, лисьи — soft, with a -ь-. Different from a normal adjective."],
      hint="a phrase with a possessive adjective (мамин / лисий / пушкинский) in an oblique case"),
    C("adj-case-complement", "b2", "adjectives", "Adjectives that govern a case",
      "полный + gen (полон людей), богатый + instr, довольный + instr, достойный + gen, "
      "склонный / способный / готовый + к + dat, похожий на + acc, равнодушный к + dat, "
      "характерный для + gen.",
      ex=[("Зал был полон народу.", "The hall was full of people."),
          ("Он способен на всё.", "He's capable of anything.")],
      hint="a 'full of / proud of / capable of / prone to X' sentence — pick the case the adjective takes"),
    C("noun-pluralia-tantum", "b2", "nouns", "Plural-only nouns (часы, деньги, ножницы, сутки)",
      "часы, деньги, ворота, каникулы, сани, ножницы, очки, брюки, духи, сутки, будни, "
      "переговоры, похороны — no singular. Count them with collective numerals (двое суток) "
      "or пара (пара ножниц).",
      ex=[("Мои часы отстают.", "My watch is slow."),
          ("Прошло трое суток.", "Three days and nights passed.")],
      traps=["двое / трое суток (collective numeral), not 'два сутки'."],
      hint="a sentence with a plural-only noun (часы / деньги / сутки / ножницы) as subject or object"),
    C("noun-mat-doch", "b2", "nouns", "мать / дочь — the -ер- stem in oblique cases",
      "мать → матери / матерью / (о) матери; pl матери / матерей. дочь → дочери / дочерью; pl "
      "дочери / дочерей / (with) дочерьми.",
      ex=[("Я позвонил матери.", "I called my mother."),
          ("Он гордится своей дочерью.", "He is proud of his daughter.")],
      hint="a sentence with мать or дочь in an oblique case (матери / дочерью / …)"),
    C("noun-gender-special", "c1", "nouns", "Common-gender & collective-singular nouns",
      "Common gender — agree by the person: сирота, коллега, умница, задира, плакса, "
      "неряха (он / она круглый / круглая сирота). Collective singular — always singular, "
      "plural sense: молодёжь, листва, мебель, посуда, обувь, родня.",
      ex=[("Она такая умница!", "She's such a clever girl!"),
          ("Молодёжь любит эту музыку.", "Young people love this music.")],
      hint="a sentence with a common-gender noun (коллега / умница) or a collective singular (молодёжь)"),
    C("noun-diminutive-expressive", "c1", "nouns", "Diminutive & expressive suffixes",
      "-ик / -ок / -чик / -ец (small): дом → домик, сын → сынок. -очк- / -ечк- / -оньк- / "
      "-еньк- (affectionate): мама → мамочка, дядя → дядюшка. -ищ- (augmentative): дом → "
      "домище. -ишк- (dismissive): городишко.",
      ex=[("Купи мне мороженого, папочка.", "Buy me an ice cream, daddy."),
          ("Ну и домище они построили!", "What a huge house they've built!")],
      hint="a sentence using a diminutive / affectionate / augmentative form of a noun"),
    C("prep-pod-nad-za-acc-idiom", "b2", "prepositions", "под / за + accusative — idiomatic",
      "под + acc: под вечер, под утро, под старость, под Новый год, попасть под дождь, "
      "отдать под суд, взять под контроль. за + acc: за границу, за город, выйти замуж за, "
      "взяться за дело, за что? (what for).",
      ex=[("Мы уезжаем за город на выходные.", "We're going out of town for the weekend."),
          ("Под вечер стало холодно.", "Towards evening it got cold.")],
      traps=["за границей / за городом (location, instr) vs за границу / за город (motion, acc)."],
      hint="a 'towards evening / out of town / caught in the rain' idiomatic под/за + accusative sentence"),

    # ---------------------------------------------------------------- B2 particles / discourse
    C("particle-ka", "b2", "particles", "-ка — softening an imperative / suggestion",
      "Attached to an imperative or a 1st-person form: дай-ка посмотреть, покажи-ка, "
      "давай-ка, а пойду-ка я домой. Makes a request casual and gentle.",
      ex=[("Дай-ка мне эту книгу.", "Let me have that book, would you."),
          ("Погоди-ка, я сейчас.", "Hang on a sec, I'll be right there.")],
      hint="a casual softened request with -ка on the imperative"),
    C("discourse-connectors", "b2", "particles", "Contrast connectors: зато / однако / тем не менее / впрочем",
      "зато (but on the plus side), однако (however — mid-sentence or formal 'but'), тем не "
      "менее / всё же / всё-таки (nevertheless), впрочем (then again / actually), а то (or "
      "else), зато (makes up for it).",
      ex=[("Дорого, зато качественно.", "Expensive, but at least it's good quality."),
          ("Он устал, тем не менее продолжил работу.", "He was tired; nevertheless he kept working.")],
      hint="a sentence linked with зато / однако / тем не менее / впрочем"),
    C("exclamative-chto-za", "b2", "syntax", "Exclamatives: что за … / какой … / вот это …",
      "Что за шум? Какая красота! Что ты за человек! Вот это да! Ну и погода! Экспрессивные: "
      "то-то и оно, ещё бы, как же.",
      ex=[("Что за глупость ты говоришь!", "What nonsense you're talking!"),
          ("Какой сегодня чудесный день!", "What a wonderful day it is today!")],
      hint="an exclamation with что за / какой / вот это / ну и"),
    C("negation-emphatic", "b2", "syntax", "Emphatic negation: вовсе не / отнюдь не / далеко не / ничуть",
      "вовсе не (not at all), отнюдь не (by no means — bookish), далеко не (far from), ничуть "
      "не / нисколько не (not in the least), никак (in no way), и не думал (didn't even think "
      "of).",
      ex=[("Это далеко не лучший вариант.", "This is far from the best option."),
          ("Я вовсе не устал.", "I'm not tired at all.")],
      hint="a strongly-negated 'not at all / far from / by no means' sentence"),

    # ---------------------------------------------------------------- C1 syntax
    C("prep-denominal", "c1", "prepositions", "Compound / denominal prepositions",
      "в связи с + instr, в отличие от + gen, по сравнению с + instr, в соответствии с + "
      "instr, в зависимости от + gen, за счёт + gen, по мере + gen, вплоть до + gen, наряду "
      "с + instr, за исключением + gen, включая + acc, судя по + dat, несмотря на + acc.",
      ex=[("В связи с ремонтом магазин закрыт.", "The shop is closed due to renovations."),
          ("По сравнению с прошлым годом цены выросли.", "Compared with last year, prices have risen.")],
      hint="a formal sentence with a compound preposition (в связи с / в отличие от / за счёт / …)"),
    C("concessive-by-ni", "c1", "syntax", "Universal concession: кто бы ни, что бы ни, как бы ни",
      "'no matter who / what / how / where / how much' — subordinate word + бы + ни + past: "
      "Что бы ни случилось, я тебя поддержу. Как ни старайся, всего не успеешь (also без бы, "
      "with present).",
      ex=[("Куда бы он ни поехал, всюду его узнают.", "Wherever he goes, he's recognised everywhere."),
          ("Сколько бы я ни спал, всё равно устаю.", "No matter how much I sleep, I'm still tired.")],
      traps=["ни (concession), not не. бы + past for a hypothetical; drop бы for a general fact."],
      hint="a 'no matter who / what / how / where …' universal-concession sentence with … бы ни"),
    C("cause-s-gen-emotion", "c1", "prepositions", "с + genitive: 'out of' an emotion (со злости, с горя)",
      "A deed done in the grip of a feeling: со злости, с горя, с досады, с перепугу, с "
      "непривычки, со скуки, с радости. Colloquial, often with the -у genitive.",
      ex=[("Он с досады хлопнул дверью.", "He slammed the door in annoyance."),
          ("С непривычки у меня заболели ноги.", "My legs ached because I wasn't used to it.")],
      hint="an 'out of anger / grief / boredom' sentence with с + genitive"),
    C("word-order-theme-rheme", "c1", "syntax", "Word order — given before new",
      "Neutral Russian order puts known information first and the new / stressed information "
      "last. 'A book is on the table' = На столе (лежит) книга; 'The book is on the table' = "
      "Книга (лежит) на столе. Questions and emphasis reshuffle accordingly.",
      ex=[("В комнату вошёл незнакомый мужчина.", "An unfamiliar man came into the room. (new: the man)"),
          ("Этот фильм я уже видел.", "I've already seen this film. (topic fronted)")],
      hint="a sentence where the word order has to put the new / stressed element last"),
    C("participle-present-passive", "c1", "participles", "Present passive participles (-ем- / -им-)",
      "From imperfective transitive verbs: любимый, уважаемый, читаемый, видимый, "
      "невидимый, независимый, необходимый, движимый. Many survive only as adjectives.",
      ex=[("Это самая читаемая газета в стране.", "This is the most widely-read newspaper in the country."),
          ("Уважаемые коллеги!", "Dear (esteemed) colleagues!")],
      hint="a phrase with a present passive participle (любимый / уважаемый / видимый) modifying a noun"),
    C("participle-vs-gerund-choice", "c1", "participles", "Participle vs gerund — which one",
      "A participle modifies a NOUN and agrees with it (человек, читающий газету). A gerund "
      "modifies the VERB and shares the main subject (Читая газету, он пил кофе). Mixing them "
      "up ('*читающий газету, он…') is a classic error.",
      ex=[("Мужчина, сидящий у окна, — мой отец.", "The man sitting by the window is my father."),
          ("Сидя у окна, он читал газету.", "Sitting by the window, he read the paper.")],
      hint="a sentence that needs a participle (agrees with a noun) or a gerund (shares the subject)"),
    C("reported-skeptical", "c1", "particles", "дескать / мол / якобы — reported & skeptical",
      "мол / дескать = 'he says / supposedly' (relaying someone's words, often with distance). "
      "якобы = 'allegedly / supposedly' (the speaker doubts it). будто (бы) = 'as if / "
      "reportedly'.",
      ex=[("Он говорит, мол, некогда ему.", "He says he hasn't got time, supposedly."),
          ("Она якобы ничего не знала.", "She allegedly knew nothing.")],
      hint="a sentence relaying someone's claim with мол / дескать / якобы"),
    C("noun-stress-shift", "c1", "nouns", "Mobile stress in declension",
      "Stress moves between stem and ending: рука́ → ру́ку / ру́ки (pl); голова́ → го́лову; "
      "о́зеро → озёра; дом → дома́ (pl); стол → столы́. Getting the stress wrong can change "
      "the case heard (ру́ки gen sg vs руки́…).",
      ex=[("Он взял меня за́ руку.", "He took me by the hand. (stress on за)"),
          ("В озёрах водится рыба.", "There are fish in the lakes.")],
      hint="a sentence where a noun's stress shifts off the stem in an oblique or plural form"),
    C("substantivized-adjectives", "c1", "nouns", "Adjectives used as nouns (прохожий, знакомый, будущее)",
      "Decline as adjectives, function as nouns: прохожий, знакомый, больной, дежурный, "
      "рабочий, учёный, взрослый, родные, будущее, прошлое, главное, целое, животное, "
      "насекомое.",
      ex=[("Я встретил старого знакомого.", "I met an old acquaintance."),
          ("Не будем говорить о прошлом.", "Let's not talk about the past.")],
      hint="a sentence with a substantivized adjective (знакомый / больной / будущее) in an oblique case"),

    # ---------------------------------------------------------------- C2
    C("archaic-bookish-forms", "c2", "syntax", "Archaic & high-style forms (дабы, коль скоро, ежели)",
      "Literary / archaic: дабы (= чтобы), коль скоро / ежели (= если), покуда (= пока), "
      "нежели (= чем), сей / оный, столь, ибо (= потому что), кабы. Also the vocative-like "
      "боже, отче and the -о ending in poetry.",
      ex=[("Он спешил, дабы не опоздать.", "He hurried so as not to be late."),
          ("Ибо сказано было...", "For it had been said...")],
      hint="a high-style / archaic sentence using ибо / дабы / коль скоро / нежели"),
    C("ellipsis-gapping", "c2", "syntax", "Ellipsis & gapping (dropping the shared verb)",
      "Russian freely omits a repeated verb, often marked by a dash: Я поехал на север, а он "
      "— на юг. Одни говорят одно, другие — другое. Also the zero copula everywhere.",
      ex=[("Утром — совещание, вечером — самолёт.", "A meeting in the morning, a flight in the evening."),
          ("Ты выбрал кофе, а я — чай.", "You chose coffee, and I (chose) tea.")],
      hint="a sentence that drops a repeated verb, with a dash standing in for it"),
    C("emphatic-genitive-superlative", "c2", "syntax", "Emphatic 'X of X's' (лучший из лучших, царь царей)",
      "A genitive-plural noun after its own singular for maximal emphasis: лучший из лучших, "
      "герой из героев, тайна тайн, из года в год, день ото дня, друг для друга.",
      ex=[("Это был день из дней.", "It was a day of days."),
          ("Он мастер из мастеров.", "He is a master among masters.")],
      hint="an emphatic 'the best of the best / X of X's' construction"),
]

_BY_ID = {c.id: c for c in CONCEPTS}
_BY_LEVEL = {}
for _c in CONCEPTS:
    _BY_LEVEL.setdefault(_c.level, []).append(_c)


def concept(cid):
    return _BY_ID.get(cid)


def all_public():
    return [c.public() for c in CONCEPTS]


def _id_setting(key):
    import json
    c = store.connect()
    row = c.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    c.close()
    if not row:
        return set()
    try:
        return {str(x) for x in json.loads(row["value"])}
    except (ValueError, TypeError):
        return set()


def _known_ids():
    """Concept ids the learner has told us they've got from school — de-prioritised."""
    return {cc.id for cc in CONCEPTS if cc.basic} | _id_setting("drill_known_concepts")


def muted_ids():
    """Concepts the learner has marked 'I know this — stop the cards'. Excluded
    from generation entirely (until unmuted)."""
    return _id_setting("drill_muted_concepts") & set(_BY_ID)


def set_muted(cid, muted):
    if cid not in _BY_ID:
        return False
    import json
    cur = muted_ids()
    cur = (cur | {cid}) if muted else (cur - {cid})
    c = store.connect()
    c.execute("INSERT OR REPLACE INTO app_settings(key, value) VALUES('drill_muted_concepts', ?)",
              (json.dumps(sorted(cur)),))
    c.commit()
    c.close()
    return True


# ------------------------------------------------------------------ progress

def _status(n, pct, easy=0, wrong=0):
    if n == 0:
        return "new"
    # earned rest: you kept rating it "too easy" and never missed it in the window
    if easy >= _EASY_MIN and wrong == 0:
        return "easy"
    if n < _PRACTISE_MIN:
        return "seen"
    if n >= _SEEN_LEARN and pct >= _LEARN_PCT:
        return "learned"
    return "practising"


def _tally(right, wrong, easy=0):
    n = right + wrong
    pct = round(right / n, 2) if n else 0.0
    return {"seen": n, "right": right, "wrong": wrong, "easy": easy, "pct": pct,
            "status": _status(n, pct, easy, wrong)}


def _graded_rows(c):
    return c.execute(
        "SELECT skill AS cid, band, verdict FROM drill_items "
        "WHERE skill IS NOT NULL AND skill <> '' AND verdict IS NOT NULL "
        "ORDER BY graded_at DESC, id DESC").fetchall()


def concept_stats(with_bands=False):
    """{cid: {seen, right, wrong, pct, status[, by_band]}} for every concept.
    `by_band` maps band index → the same tally, so stats can be sliced by rule,
    by frequency band, or by both."""
    c = store.connect()
    rows = _graded_rows(c)
    c.close()
    muted = muted_ids()
    tot, per_band = {}, {}
    for r in rows:
        cid = r["cid"]
        if cid not in _BY_ID:
            continue
        t = tot.setdefault(cid, {"r": 0, "w": 0, "e": 0})
        if t["r"] + t["w"] < _WINDOW:
            t["r" if _right(r["verdict"]) else "w"] += 1
            if r["verdict"] == "easy":
                t["e"] += 1
        if with_bands and r["band"] is not None:
            b = per_band.setdefault(cid, {}).setdefault(int(r["band"]), {"r": 0, "w": 0})
            b["r" if _right(r["verdict"]) else "w"] += 1
    out = {}
    for cc in CONCEPTS:
        t = tot.get(cc.id, {"r": 0, "w": 0, "e": 0})
        entry = _tally(t["r"], t["w"], t["e"])
        entry["muted"] = cc.id in muted
        # what the learner sees + what counts as "done" for the level %
        entry["mastery"] = "muted" if entry["muted"] else entry["status"]
        if with_bands:
            entry["by_band"] = {str(bi): _tally(v["r"], v["w"])
                                for bi, v in sorted(per_band.get(cc.id, {}).items())}
        out[cc.id] = entry
    return out


def band_progress():
    """Per frequency band: overall accuracy across every graded card (including
    legacy cards not tagged to a current concept)."""
    c = store.connect()
    rows = _graded_rows(c)
    c.close()
    agg = {}
    for r in rows:
        if r["band"] is None:
            continue
        b = agg.setdefault(int(r["band"]), {"r": 0, "w": 0})
        b["r" if _right(r["verdict"]) else "w"] += 1
    return [{"band": bi, **_tally(v["r"], v["w"])} for bi, v in sorted(agg.items())]


def totals():
    """Every graded drill card the learner has ever done (all-time), so the
    grammar page can show 'N cards done'."""
    c = store.connect()
    row = c.execute(
        "SELECT COUNT(*) n, SUM(verdict IN ('right','easy')) r, "
        "MIN(graded_at) first, MAX(graded_at) last FROM drill_items "
        "WHERE verdict IS NOT NULL").fetchone()
    tagged = c.execute(
        "SELECT COUNT(*) n FROM drill_items WHERE verdict IS NOT NULL "
        "AND skill IS NOT NULL AND skill <> ''").fetchone()["n"]
    c.close()
    n = row["n"] or 0
    return {"cards": n, "right": row["r"] or 0,
            "pct": round((row["r"] or 0) / n, 2) if n else 0.0,
            "tagged": tagged, "first": row["first"], "last": row["last"]}


def level_progress():
    st = concept_stats()
    out = []
    for lvl in LEVELS:
        ids = [c.id for c in _BY_LEVEL.get(lvl, [])]
        if not ids:
            continue
        # "done" = learned by the algorithm, rated too-easy, or marked known
        done = sum(st[i]["mastery"] in ("learned", "muted", "easy") for i in ids)
        practising = sum(st[i]["mastery"] == "practising" for i in ids)
        seen = sum(st[i]["mastery"] != "new" for i in ids)
        out.append({"level": lvl, "total": len(ids), "learned": done,
                    "practising": practising, "seen": seen,
                    "pct": round(100 * done / len(ids))})
    return out


def cards_for_concept(cid, limit=60):
    """The drill cards the learner has actually seen for this concept."""
    c = store.connect()
    rows = c.execute(
        "SELECT id, prompt, answer, given, target, note, verdict, graded_at, band, card_id "
        "FROM drill_items WHERE skill=? AND verdict IS NOT NULL "
        "ORDER BY graded_at DESC LIMIT ?", (cid, limit)).fetchall()
    c.close()
    return [dict(r) for r in rows]


def concept_detail(cid):
    cc = _BY_ID.get(cid)
    if not cc:
        return None
    return {**cc.public(), "stats": concept_stats(with_bands=True)[cid],
            "cards": cards_for_concept(cid)}


# ------------------------------------------------------------------ selection

def pick_concepts(n, band_level="b1"):
    """Choose `n` concept ids for the next drill batch — weighted toward the
    learner's working level (+/- one), concepts not yet learned, and away from
    the school basics they already have. Returns a list of concept dicts with
    the fields the LLM needs."""
    import random
    st = concept_stats()
    known = _known_ids()
    order = {l: i for i, l in enumerate(LEVELS)}
    target = order.get((band_level or "b1").lower(), 1)
    muted = muted_ids()
    pool = []
    for cc in CONCEPTS:
        if cc.id in muted:
            continue                        # "I know this" — don't test it at all
        lg = order[cc.level]
        # distance-from-target weighting: same level 6, ±1 level 3, else 1
        d = abs(lg - target)
        w = 6 if d == 0 else 3 if d == 1 else 1
        status = st[cc.id]["status"]
        if status == "easy":
            w *= 0.02                       # you rated it too easy — near-zero, resurfaces only as a rare spot-check; one miss and it's back
        elif status == "learned":
            w *= 0.12                       # rare spaced review — drops back if you slip
        elif status == "practising":
            w *= 2.5                        # push on the shaky ones
        elif status == "new":
            w *= 1.6                        # cover new ground
        if cc.id in known:
            w *= 0.2                        # they've got these from school
        pool.append((cc, w))
    if not pool:
        return []
    # A concept may appear up to twice in one batch when it's still new /
    # practising, so a track record on the rules that matter builds ~2x faster
    # than one-card-per-concept-per-batch would allow.
    cap = {}
    for cc in [p[0] for p in pool]:
        s = st[cc.id]["status"]
        cap[cc.id] = 2 if s in ("new", "practising") else 1  # "easy"/"learned" → at most once
    picks, count = [], {}
    ccs = [p[0] for p in pool]
    ws = [p[1] for p in pool]
    for _ in range(n * 6):
        if len(picks) >= n:
            break
        cc = random.choices(ccs, weights=ws)[0]
        if count.get(cc.id, 0) >= cap[cc.id]:
            continue
        count[cc.id] = count.get(cc.id, 0) + 1
        picks.append({"id": cc.id, "level": cc.level, "category": cc.cat,
                      "title": cc.title, "hint": cc.hint or cc.explain,
                      "traps": cc.traps[:2]})
    return picks
