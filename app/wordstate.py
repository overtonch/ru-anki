"""User-assignable verdicts on a word / lemma.

Stored as `resolved_words.reason`. `has_card` is set by the pipeline when a card
exists; the rest are things the learner picks when they remove a card or dismiss
a highlighted word. Every non-card verdict here means "don't suggest this again"
and "stop highlighting it" — they differ only in *why*, which drives the UI
label and the progress stats.

To add a level later (e.g. "mastered", "ignore"): add an entry below. Order is
the order it shows in the pick-a-verdict sheet. `assignable` entries appear
there automatically; the API, stats, and word page pick them up with no other
change. `removes_card` deletes the SRS/Anki card when the verdict is set.
"""

STATES = {
    "has_card": {
        "label": "learning",
        "assignable": False,
        "removes_card": False,
        "tone": "learning",
    },
    "learned": {
        "label": "learned",
        "blurb": "you studied it — you know it now",
        "assignable": True,
        "removes_card": True,
        "tone": "good",
    },
    "known": {
        "label": "not learning",
        "blurb": "too easy, a cognate, or just not worth a card",
        "assignable": True,
        "removes_card": True,
        "tone": "muted",
    },
}

# insertion order = display order
ASSIGNABLE = [k for k, v in STATES.items() if v["assignable"]]

# the historical default when a card is removed without an explicit choice
DEFAULT_REMOVED = "known"


def is_valid(reason):
    return reason in STATES


def is_assignable(reason):
    return reason in STATES and STATES[reason]["assignable"]


def label(reason):
    return STATES.get(reason, {}).get("label", reason)


def public():
    """Shape sent to the client so the verdict sheet renders itself."""
    return {
        "assignable": [
            {"key": k, **{f: STATES[k].get(f) for f in ("label", "blurb", "tone")}}
            for k in ASSIGNABLE
        ],
        "default_removed": DEFAULT_REMOVED,
    }
