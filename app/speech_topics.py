"""A curated bank of realistic speech briefs for the Speech Lab's "suggest one"
mode.

Every entry is a situation this particular learner actually lands in — a mid-20s
software engineer in New York getting to know his girlfriend's Russian-speaking
family. These are the things that genuinely come up across a dinner table or a
one-on-one with her mum, worth having ready cold so the chunks surface on their
own when it counts. `core` items are the highest-frequency ones and are offered
first; the rest round out the range (being a good guest, telling a small story,
staying out of heavy topics, a toast).

Each `brief` is passed straight to `llm.speech_draft` as the topic — the voice
and format live in the prompt, so a brief only needs to set the scene and say
what to cover.
"""


class Topic:
    __slots__ = ("id", "label", "brief", "core")

    def __init__(self, id, label, brief, core=False):
        self.id = id
        self.label = label
        self.brief = " ".join(brief.split())
        self.core = core


TOPICS = [
    Topic("lang-journey", "How you learned Russian", core=True, brief="""
        Someone at the table asks how you learned Russian. Walk through your
        path: a semester of classes for the grammar basics, then years mostly on
        your own — watching YouTube, listening to podcasts, mining sentences and
        doing flashcards every day, one stretch of reading a whole book in
        Russian. And where you're at now: you understand almost everything, but
        speaking is the hard part, and that's exactly what you're working on."""),

    Topic("my-job", "Explaining your job", core=True, brief="""
        Your girlfriend's mum asks what you actually do for work. Explain that
        you're a programmer / software engineer in plain terms a non-technical
        person would follow — roughly what you build, what an ordinary work day
        looks like, that you mostly work from home. Keep it concrete and
        modest, no jargon, a touch of humour about sitting at a computer all
        day."""),

    Topic("how-we-met", "How you and your girlfriend met", core=True, brief="""
        The classic family question — how you and your girlfriend met and got
        together. Tell it as a short story: where you met, how it started, how
        long you've been together now, and that you're living together (with her
        mum around, which is how your Russian is really coming along)."""),

    Topic("my-family", "Your own family", core=True, brief="""
        They ask about your family — where you grew up, your parents and what
        they do, brothers or sisters, whether you're close, how often you see
        them now that you're in New York. A warm, simple picture of where you
        come from."""),

    Topic("life-in-nyc", "Life in New York", core=True, brief="""
        You're asked what it's like living in New York — the pace of it, your
        neighbourhood, what you like about it and what wears you down, how it
        compares to a smaller or quieter place. Casual, a few concrete
        details."""),

    Topic("free-time", "What you do in your free time", core=True, brief="""
        Small-talk staple: what you get up to on weekends and in the evenings —
        what you do to switch off, any sport, going out versus staying in, the
        things you and your girlfriend like to do together."""),

    Topic("why-it-matters", "Why the language matters to you", core=True, brief="""
        You want to say a bit about why learning Russian matters to you and how
        it's going — that you're doing it to really be part of the family and
        the conversations, not just to get by; that it's slow going; that you
        get shy about speaking and make plenty of mistakes, but you're not
        giving up. A little open and warm, not a speech about language
        learning."""),

    Topic("a-toast", "A short toast", core=True, brief="""
        It's a family dinner and it comes round to you to say a few words before
        everyone drinks. A warm, simple toast — that you're glad to be here, to
        the family for taking you in, to everyone's health and to being together.
        Two or three sentences, sincere, not overdone."""),

    Topic("food-and-cooking", "Talking about food", brief="""
        You're complimenting the meal and the talk turns to food — what you like
        to eat, whether you cook and what you can actually make, Russian dishes
        you've tried and liked, something you'd want to learn to cook."""),

    Topic("making-plans", "Making plans together", brief="""
        The conversation is about what's coming up — a visit, a holiday, a trip,
        the next time everyone will see each other. Float a couple of ideas
        loosely, ask what works for them, keep it easy-going rather than
        organising everyone."""),

    Topic("impressions-of-russia", "Your impressions of Russian culture", brief="""
        You're asked what you make of Russian culture — films or shows you've
        watched, music you've come across, books, things that surprised you,
        what you'd still like to see or read. Genuine interest without laying it
        on thick."""),

    Topic("asking-about-them", "Asking about their life", brief="""
        This one is mostly YOU asking and reacting. You want to draw your
        girlfriend's mum out about her own life — her work, where she grew up,
        what things were like then, how she finds it here now. Lots of short
        natural questions and warm reactions («правда?», «а потом что?», «вот это
        да», «как интересно»), with only a sentence or two about yourself as a
        bridge."""),

    Topic("when-russian-slips", "When your Russian slips mid-conversation", brief="""
        A light, graceful thing to say when you lose the thread or fumble a
        sentence in the middle of a conversation — that your Russian is still
        coming along, could they slow down a little or say that again, that you
        understand more than you can get out. Keeps it easy and warm rather than
        awkward."""),

    Topic("staying-out-of-it", "Staying out of heavy topics", brief="""
        The conversation drifts toward politics or the news. A few graceful ways
        to not get pulled in — that you try to stay out of that stuff, that
        you'd rather hear about the family, steering it back to something else
        lightly and kindly, without lecturing anyone."""),

    Topic("checking-in", "Asking after someone", brief="""
        Checking in on how someone has been — an older relative, or someone who
        was recently unwell. Warm and a little careful, asking how they're
        feeling now, and offering to help with something concrete if it would be
        useful."""),

    Topic("a-small-story", "A small story about your week", brief="""
        Tell a short, low-stakes story about something that happened to you
        recently — a mix-up on the subway, something funny or annoying on the way
        somewhere, a small win at work or at home. Lean on the spoken narrative
        connectors: и вот, и тут, оказывается, в итоге, короче."""),

    Topic("being-a-guest", "Being a good guest", brief="""
        You're at their place for the first time in a while. The natural things
        a guest says — thanks for having me, the place looks great, can I help
        with anything, something kind about the meal or the effort. Short, warm,
        the kind of thing that puts everyone at ease."""),

    Topic("the-visit-so-far", "How the visit is going", brief="""
        Someone asks how you're finding the trip / the time together so far.
        Talk about what's been good, something new you tried or saw, how it's
        been staying with everyone, what you're looking forward to in the days
        left."""),

    Topic("learning-to-cook-it", "A dish you want to learn", brief="""
        You've had something at the table you really liked and you want to learn
        to make it. Say so, ask how it's done, ask whether it's hard, offer to
        help next time so you can watch. Ends up being mostly warm questions."""),
]

_BY_ID = {t.id: t for t in TOPICS}


def get(topic_id):
    return _BY_ID.get(topic_id)


def all_ids():
    return [t.id for t in TOPICS]
