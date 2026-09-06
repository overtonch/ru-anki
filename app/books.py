"""Frequency profiles of whole books the learner is working toward — right now
just Anna Karenina, as a long-term reading goal.

`app/data/books/<id>.freq.json.gz` holds {meta:{title,author,tokens,types,source},
freq:{lemma: count}} — pymorphy-lemmatised counts over the full public-domain
text. Small (~80 KB); loaded lazily and cached.

proficiency.book_readiness() turns this + what the learner knows into a
"how much of it could you read" number; reading_flow feeds the frequent unknowns
into the fiction generator so its stories rehearse the actual vocabulary.
"""
import gzip
import json
import os

_DIR = os.path.join(os.path.dirname(__file__), "data", "books")
_cache = {}

BOOKS = ["anna_karenina"]


def _load(book):
    if book in _cache:
        return _cache[book]
    path = os.path.join(_DIR, f"{book}.freq.json.gz")
    with gzip.open(path, "rt", encoding="utf-8") as f:
        d = json.load(f)
    d["freq"] = {k: int(v) for k, v in d.get("freq", {}).items()}
    _cache[book] = d
    return d


def freq(book="anna_karenina"):
    return _load(book)["freq"]


def meta(book="anna_karenina"):
    return _load(book)["meta"]


def all_meta():
    return [{"id": b, **_load(b)["meta"]} for b in BOOKS if os.path.exists(
        os.path.join(_DIR, f"{b}.freq.json.gz"))]
