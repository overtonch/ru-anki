"""Minimal EPUB -> plain-text-chapters parser. Stdlib only (zipfile + ElementTree
+ html.parser) — no ebooklib/lxml. Good enough for reading + tap-to-card; we
throw away styling, images, footnotes markup, keeping paragraph structure."""
import html
import io
import posixpath
import re
import zipfile
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

_NS = {"cnt": "urn:oasis:names:tc:opendocument:xmlns:container",
       "opf": "http://www.idpf.org/2007/opf",
       "dc": "http://purl.org/dc/elements/1.1/"}

_BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6",
          "blockquote", "tr", "section", "article"}
_DROP = {"script", "style", "head", "sup", "table"}   # sup: footnote refs


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf = []
        self._skip = 0
        self.title = None
        self._in_h = False
        self._h = []

    def handle_starttag(self, tag, attrs):
        if tag in _DROP:
            self._skip += 1
        if tag in ("h1", "h2", "h3") and self.title is None:
            self._in_h = True
        if tag in _BLOCK and not self._skip:
            self.buf.append("\n")

    def handle_endtag(self, tag):
        if tag in _DROP and self._skip:
            self._skip -= 1
        if tag in ("h1", "h2", "h3") and self._in_h:
            self._in_h = False
            t = re.sub(r"\s+", " ", "".join(self._h)).strip()
            if t:
                self.title = t
        if tag in _BLOCK and not self._skip:
            self.buf.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self.buf.append(data)
        if self._in_h:
            self._h.append(data)

    def text(self):
        raw = "".join(self.buf)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r" *\n *", "\n", raw)
        raw = re.sub(r"\n{2,}", "\n\n", raw)
        # collapse single newlines inside a paragraph to spaces, keep blank lines
        paras = [re.sub(r"\s*\n\s*", " ", p).strip() for p in raw.split("\n\n")]
        return "\n\n".join(p for p in paras if p).strip()


def _xml(zf, name):
    return ET.fromstring(zf.read(name))


def parse(data: bytes):
    """bytes of an .epub -> {title, author, chapters:[{title, body}]}.
    Raises ValueError on anything that isn't a usable EPUB."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise ValueError(f"not a zip/epub: {e}")

    try:
        container = _xml(zf, "META-INF/container.xml")
    except KeyError:
        raise ValueError("no META-INF/container.xml — not an EPUB")
    rootfile = container.find(".//cnt:rootfile", _NS)
    if rootfile is None:
        raise ValueError("EPUB container has no rootfile")
    opf_path = rootfile.get("full-path")
    opf_dir = posixpath.dirname(opf_path)
    opf = _xml(zf, opf_path)

    md = opf.find("opf:metadata", _NS)
    title = author = None
    if md is not None:
        t = md.find("dc:title", _NS)
        a = md.find("dc:creator", _NS)
        title = (t.text or "").strip() if t is not None and t.text else None
        author = (a.text or "").strip() if a is not None and a.text else None

    manifest = {item.get("id"): item.get("href")
                for item in opf.findall("opf:manifest/opf:item", _NS)}
    spine = [it.get("idref") for it in opf.findall("opf:spine/opf:itemref", _NS)]

    chapters = []
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        name = posixpath.normpath(posixpath.join(opf_dir, href.split("#")[0]))
        try:
            raw = zf.read(name).decode("utf-8", "replace")
        except KeyError:
            continue
        p = _Text()
        try:
            p.feed(raw)
        except Exception:  # noqa: BLE001
            continue
        body = p.text()
        if len(body) < 40:          # skip covers, nav pages, blank sections
            continue
        low = body[:400].lower()
        if not chapters and ("project gutenberg" in low
                             and ("this ebook is for the use" in low
                                  or "start of the project gutenberg" in low)):
            continue                 # Gutenberg front-matter / license page
        chapters.append({"title": p.title, "body": body})

    if not chapters:
        raise ValueError("no readable chapters found in EPUB")
    for i, ch in enumerate(chapters, 1):
        if not ch["title"]:
            ch["title"] = f"Chapter {i}"
    return {"title": title or "Untitled", "author": author, "chapters": chapters}


def from_plain(text: str, title: str = None):
    """A pasted / .txt import: split into chapters on runs of blank lines only if
    it's very long, else one chapter."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise ValueError("empty text")
    paras = [re.sub(r"[ \t]+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    paras = [p for p in paras if p]
    body = "\n\n".join(paras)
    title = (title or paras[0][:60]).strip() or "Untitled"
    # one chapter unless it's really long, then break every ~24k chars on a para
    if len(body) < 30000:
        return {"title": title, "author": None,
                "chapters": [{"title": title, "body": body}]}
    chapters, cur, n = [], [], 0
    for p in paras:
        cur.append(p)
        n += len(p)
        if n > 24000:
            chapters.append({"title": f"Part {len(chapters) + 1}", "body": "\n\n".join(cur)})
            cur, n = [], 0
    if cur:
        chapters.append({"title": f"Part {len(chapters) + 1}", "body": "\n\n".join(cur)})
    return {"title": title, "author": None, "chapters": chapters}
