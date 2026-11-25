"""People to contact when there's no SEC filing.

Form D lists a company's executive officers. A London or Bengaluru startup has
no Form D, so for those I look in two places:
  - the raise article itself (only articles on the direct feeds - Inc42,
    Entrackr, YourStory - since google news links are redirects)
  - the company's own /about, /team, /company and /leadership pages

Never guessed: a name only counts if a founder/ceo/coo-style title sits right
next to it in the text ("Ajeet Singh, founder and CEO", "co-founder and CEO
Ajeet Singh", or a team card with the name on one line and the title on the
next). Every person keeps the url they were found on.
"""
import html
import re

from src.fetch import _web

TITLE = (r"(?:co-?founder|founder|ceo|coo|cto|cfo|chief executive officer|chief operating officer|"
         r"chief technology officer|chief financial officer|president|managing director|md)")
# only the title ignores case - names have to be capitalised, or "Oren Michaely is" sneaks in
TITLE_PHRASE = rf"(?i:(?:(?:co-?)?founder|{TITLE})(?:\s*(?:and|&|,|/)\s*(?:{TITLE}))*)"
NAME = r"[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?(?:\s(?:[A-Z]\.\s)?[A-Z][a-z]+(?:[-'][A-Z][a-z]+)?){1,2}"
NOT_NAMES = set("""The This That Series Seed Funding Round Capital Ventures Partners Fund Inc Ltd Llc Pvt Labs
Group Technologies Technology Our Team About Company India Bengaluru Bangalore Mumbai Delhi Hyderabad London New York San Francisco
Read More Founded Founder Chief Officer Executive Investors Investor Led Backed Startup Startups Platform
News Today Monday Tuesday Wednesday Thursday Friday Saturday Sunday Also According Speaking Commenting""".split())

AFTER = re.compile(rf"(?P<name>{NAME}),?\s+(?:its\s+|the\s+|our\s+)?(?P<title>{TITLE_PHRASE})\b")
BEFORE = re.compile(rf"\b(?P<title>{TITLE_PHRASE})\s+(?:and\s+)?(?P<name>{NAME})\b")
PAGES = ("/about", "/about-us", "/team", "/company", "/leadership", "/our-team")


def _text_lines(page):
    page = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", page or "")
    page = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|li|span|td|a)>", "\n", page)
    text = html.unescape(re.sub(r"<[^>]+>", " ", page))
    return [re.sub(r"\s+", " ", l).strip() for l in text.split("\n") if l.strip()]


def _good_name(name):
    words = name.replace("-", " ").split()
    return 2 <= len(words) <= 3 and not any(w.strip(".") in NOT_NAMES for w in words)


def _tidy_title(title):
    title = re.sub(r"\s+", " ", title).strip(" ,")
    return re.sub(r"\b(ceo|coo|cto|cfo|md)\b", lambda m: m.group(1).upper(), title.lower()).replace("co founder", "co-founder")


def people_in_text(page, url):
    """[{'name', 'title', 'source'}] found in a page. Only names with a title next to them."""
    lines = _text_lines(page)
    found = {}

    def add(name, title):
        name = name.strip()
        if _good_name(name) and name.lower() not in found:
            found[name.lower()] = {"name": name, "title": _tidy_title(title), "source": url}

    for line in lines:
        if len(line) > 600:
            line = line[:600]
        for rx in (AFTER, BEFORE):
            for m in rx.finditer(line):
                add(m.group("name"), m.group("title"))
    # team cards: "Jane Doe" on one line, "Co-founder & CEO" on the next
    for a, b in zip(lines, lines[1:]):
        if re.fullmatch(NAME, a) and len(b) < 60 and re.fullmatch(rf"\s*{TITLE_PHRASE}\s*", b, re.I):
            add(a, b)
    return list(found.values())


def find_people(domain=None, article_urls=(), limit=5):
    """Founders/ceo/coo from raise articles and the company's own pages, with sources."""
    urls = [u for u in article_urls if u and "news.google.com" not in u]
    if domain:
        urls += [f"https://{domain}{p}" for p in PAGES]
    people, seen = [], set()
    for url in urls:
        status, final, page = _web(url)
        if status != 200 or not page:
            continue
        for p in people_in_text(page, final):
            if p["name"].lower() not in seen:
                seen.add(p["name"].lower())
                people.append(p)
        if len(people) >= limit:
            break
    return people[:limit]
