"""AnkiConnect client. Desktop Anki must be running with the AnkiConnect add-on
(localhost:8765) for any card creation to work."""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from store import bold  # noqa: E402

ANKI_URL = os.environ.get("ANKICONNECT_URL", "http://127.0.0.1:8765")
DECK = os.environ.get("ANKI_DECK", "Russian::YouTube mining")
MODEL_NAME = "RU context recognition"

_CSS = (
    ".card{font-size:20px;text-align:center}"          # inherit Anki's theme (light/night)
    ".sent{margin:14px}.tr{font-size:22px}"
    ".src{margin-top:18px;font-size:13px;opacity:.6}.src a{color:inherit}"
    "b{font-weight:700;color:inherit}"
)
_FRONT = '<div class="sent">{{Front}}</div>'
_BACK = ('{{FrontSide}}<hr id="answer"><div class="tr">{{Back}}</div>'
         '<div class="src">{{Source}}</div>')


class AnkiError(RuntimeError):
    pass


def invoke(action, **params):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode()
    req = urllib.request.Request(ANKI_URL, data=payload,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.loads(r.read())
    except urllib.error.URLError as e:
        raise AnkiError(
            f"cannot reach AnkiConnect at {ANKI_URL} — is desktop Anki open "
            f"with the AnkiConnect add-on? ({e})"
        )
    if resp.get("error"):
        raise AnkiError(resp["error"])
    return resp.get("result")


def ping():
    return invoke("version")


def ensure_model():
    if MODEL_NAME not in (invoke("modelNames") or []):
        invoke("createModel", modelName=MODEL_NAME,
               inOrderFields=["Front", "Back", "Source"],
               css=_CSS,
               cardTemplates=[{"Name": "Recognition", "Front": _FRONT, "Back": _BACK}])
        return
    # Model already exists (e.g. imported from an earlier .apkg with older
    # styling) — force it to the current CSS/templates so cards aren't red.
    invoke("updateModelStyling", model={"name": MODEL_NAME, "css": _CSS})
    invoke("updateModelTemplates",
           model={"name": MODEL_NAME, "templates": {"Recognition": {"Front": _FRONT, "Back": _BACK}}})


def ensure_deck(deck=DECK):
    if deck not in (invoke("deckNames") or []):
        invoke("createDeck", deck=deck)


_HMS = re.compile(r"(\d+):(\d\d):(\d\d)")


def source_html(title, channel, url, timestamp):
    """The Source field: 'channel · title · ▶ 1:16' where the whole thing links
    to the exact moment in the video."""
    link = url or ""
    label = ""
    m = _HMS.search(timestamp or "")
    if m:
        secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        sep = "&" if "?" in link else "?"
        link = f"{link}{sep}t={secs}s"
        mm, ss = divmod(secs, 60)
        label = f" · ▶ {mm}:{ss:02d}"
    parts = " · ".join(p for p in (channel, title) if p)
    inner = html.escape(parts) + label
    return f'<a href="{html.escape(link)}">{inner}</a>' if link else inner


def front_html(sentence, span, is_phrase):
    marked = bold(sentence, span, bool(is_phrase), "\x00")
    bolded = "\x00" in marked
    marked = marked.replace("\x00", "<b>", 1).replace("\x00", "</b>", 1)
    return marked, bolded


def add_card(sentence, span, is_phrase, translation, source, deck=DECK, tags=None):
    """Create the note in Anki. Returns dict(note_id, bolded, front)."""
    ensure_model()
    ensure_deck(deck)
    front, bolded = front_html(sentence, span, is_phrase)
    note = {
        "deckName": deck,
        "modelName": MODEL_NAME,
        "fields": {"Front": front, "Back": translation or "", "Source": source or ""},
        "tags": tags or ["ru-anki"],
        "options": {"allowDuplicate": False,
                    "duplicateScope": "deck"},
    }
    try:
        note_id = invoke("addNote", note=note)
    except AnkiError as e:
        if "duplicate" in str(e).lower():
            # a card with this exact Front already exists — treat as done, not
            # an error (the user has it; the candidate should stop nagging)
            return {"note_id": None, "bolded": bolded, "front": front,
                    "duplicate": True}
        raise
    # guarantee the card is in the right deck even if addNote fell back to Default
    try:
        cids = invoke("findCards", query=f"nid:{note_id}")
        if cids:
            invoke("changeDeck", cards=cids, deck=deck)
    except AnkiError:
        pass
    return {"note_id": note_id, "bolded": bolded, "front": front,
            "duplicate": False}


def sync():
    invoke("sync")


def try_sync():
    """Best-effort AnkiWeb sync. Returns None on success, or an error string.
    A card is created locally by addNote regardless; sync only pushes it to
    AnkiWeb (and needs an AnkiWeb login configured in the desktop app:
    Preferences -> Syncing)."""
    try:
        invoke("sync")
        return None
    except AnkiError as e:
        msg = str(e)
        if "auth not configured" in msg:
            return "no AnkiWeb login in desktop Anki (Preferences -> Syncing)"
        if "not one of [0, 1]" in msg or "ChangesRequired" in msg:
            return "full sync required — click Sync in desktop Anki once and choose an upload/download direction"
        return msg
