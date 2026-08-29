"""Import a web page as readable text for the reading feature.

Two paths:
  * site-specific for a few Russian classic-lit libraries that paginate one
    chapter per URL (ilibrary.ru — Толстой, Тургенев, Достоевский, Чехов …);
  * a generic readability-ish fallback: strip nav/ads/scripts, keep the
    paragraph-shaped text.

Returns {"title", "author", "chapters": [{"title", "paragraphs": [str, …]}]}.
"""
import html as _html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx

_UA = {"user-agent": "Mozilla/5.0 (compatible; ru-anki/1.0; +reading)"}
_MAX_CH = 12


def _get(url):
    r = httpx.get(url, headers=_UA, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return r.text


def _clean(s):
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


# ---------------------------------------------------------------- ilibrary.ru

def _ilibrary(url):
    idx_url = re.sub(r"/p\.\d+/index\.html$", "/index.html", url)
    idx = _get(idx_url)
    m = re.match(r"(https?://[^/]+)(/text/\d+)/", idx_url)
    base, book = m.group(1), m.group(2)
    chap_paths = list(dict.fromkeys(
        re.findall(rf'href="({re.escape(book)}/p\.\d+/index\.html)"', idx)))
    chap_urls = [base + p for p in chap_paths]

    # title / author from the first chapter page (has a clean <h1>)
    p1 = _get(chap_urls[0]) if chap_urls else idx
    title = _clean(_first(re.search(
        r'<div class="title">\s*<h1>(.*?)</h1>', p1, re.S)) or "")
    author = _clean(_first(re.search(
        r'<div class="author">.*?<z>(?:<o></o>)?(.*?)</z>', p1, re.S)) or "") or None
    if not title:
        rt = _clean(_first(re.search(r"<title>([^<]+)</title>", idx, re.I)) or "")
        title = re.sub(r"^(?:[А-ЯЁA-Z]\.\s*)+[А-ЯЁA-Z][а-яёa-z]+\.\s*", "", rt) or rt or "text"
    return title, author, chap_urls


def _ilibrary_chapter(page):
    part = _clean(_first(re.search(r"<h2>(.*?)</h2>", page, re.S)) or "")
    num = _clean(_first(re.search(r"<h3>(.*?)</h3>", page, re.S)) or "")
    label = " · ".join(x for x in (part, num) if x) or num or part
    body = page.split("<h3>", 1)[-1] if "<h3>" in page else page
    paras = [_clean(p) for p in re.findall(r"<z>(.*?)</z>", body, re.S)]
    paras = [p for p in paras if p]
    epi = _clean(_first(re.search(
        r'<div class="epigraf">\s*<z>(.*?)</z>', page, re.S)) or "")
    if epi:
        paras.insert(0, "— " + epi + " —")
    return {"title": label, "paragraphs": paras} if paras else None


# ---------------------------------------------------------------- generic

class _Article(HTMLParser):
    _SKIP = {"script", "style", "head", "noscript", "nav", "header", "footer",
             "aside", "form", "figure", "svg", "button", "select", "iframe"}
    _BLOCK = {"p", "z", "div", "li", "h1", "h2", "h3", "h4", "blockquote",
              "section", "article", "br"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.cur = []
        self.blocks = []
        self.title = ""
        self._title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip += 1
        elif tag == "title":
            self._title = True
        elif tag in self._BLOCK:
            self._flush()

    def handle_startendtag(self, tag, attrs):
        if tag == "br":
            self._flush()

    def handle_endtag(self, tag):
        if tag in self._SKIP and self.skip:
            self.skip -= 1
        elif tag == "title":
            self._title = False
        elif tag in self._BLOCK:
            self._flush()

    def handle_data(self, d):
        if self._title:
            self.title += d
        elif not self.skip:
            self.cur.append(d)

    def _flush(self):
        s = re.sub(r"\s+", " ", "".join(self.cur)).strip()
        self.cur = []
        if s:
            self.blocks.append(s)


def _generic(url):
    a = _Article()
    a.feed(_get(url))
    a._flush()
    good = [b for b in a.blocks
            if len(b) >= 40 and (b.count(" ") >= 6 or b[-1:] in ".!?…»")]
    paras = good or [b for b in a.blocks if len(b) >= 20]
    if not paras:
        raise ValueError("no readable text found on that page")
    title = (a.title.split("|")[0].split("—")[0].strip()
             or urlparse(url).netloc.replace("www.", ""))
    return {"title": title, "author": None,
            "chapters": [{"title": None, "paragraphs": paras}]}


# ---------------------------------------------------------------- entry

def _first(m):
    return m.group(1) if m else None


def import_url(url, max_chapters=5):
    url = url.strip()
    host = urlparse(url).netloc.replace("www.", "")
    if host == "ilibrary.ru":
        title, author, chap_urls = _ilibrary(url)
        n = max(1, min(_MAX_CH, max_chapters or 5))
        chapters = []
        for cu in chap_urls[:n]:
            ch = _ilibrary_chapter(_get(cu))
            if ch:
                chapters.append(ch)
        if not chapters:
            raise ValueError("couldn't find chapter text")
        return {"title": title, "author": author, "chapters": chapters}
    return _generic(url)
