"""Build a vegetarian/vegan menu calendar from the Los Altos School District menu PDFs.

Pipeline:
  1. Scrape https://www.lasdschools.org/menus for the monthly menu PDFs.
  2. Parse the "Allergens List" tables in each PDF (one row per item, per day).
  3. Merge elementary (E) and junior high (M) menus into one list per day.
  4. Save the parsed month to data/YYYY-MM.json (all items, not just veg).
  5. Render docs/menu.md, docs/menu.ics and docs/index.html for the current and next month.

Run:  uv run lasd_menu.py            # fetch + parse + render
      uv run lasd_menu.py --render   # render from data/ only, no network
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import re
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from zoneinfo import ZoneInfo

import pdfplumber
from icalendar import Alarm, Calendar, Event, vDuration

MENU_PAGE = "https://www.lasdschools.org/menus"
TZ = ZoneInfo("America/Los_Angeles")
ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / "cache"
OUT_DIR = ROOT / "docs"            # served by GitHub Pages
MD_OUT = OUT_DIR / "menu.md"
ICS_OUT = OUT_DIR / "menu.ics"
HTML_OUT = OUT_DIR / "index.html"
ARCHIVE_OUT = OUT_DIR / "archive.html"
PAGES_URL = "https://swamisun.github.io/lasd-menu/"
REPO_URL = "https://github.com/swamisun/lasd-menu"

LEVEL_ORDER = ["E", "M"]
TAG = {
    "BF": "🧇", "L": "🍴",            # meal
    "E": "🅴", "M": "🅼",             # elementary, junior high; omitted when on both
    "Vegetarian": "🍕", "Vegan": "🥗", "Vegan/GF": "🥗",
}
MEAL_NAME = {"BF": "Breakfast", "L": "Lunch"}
MONTHS = {
    m[:3].lower(): i
    for i, m in enumerate(
        ["January", "February", "March", "April", "May", "June", "July",
         "August", "September", "October", "November", "December"],
        start=1,
    )
}
MONTH_NAMES = {v: k for k, v in MONTHS.items()}

log = logging.getLogger("lasd_menu")
logging.getLogger("pdfminer").setLevel(logging.ERROR)


# ---------------------------------------------------------------- domain model

@dataclass
class Item:
    meal: str                      # "BF" or "L"
    name: str
    diet: str                      # "", "Vegetarian", "Vegan", "Vegan/GF"
    allergens: list[str]
    levels: list[str] = field(default_factory=list)   # subset of ["E", "M"]

    @property
    def is_veg(self) -> bool:
        return self.diet != ""

    @property
    def key(self) -> tuple[str, str]:
        return self.meal, re.sub(r"[^a-z0-9]+", " ", self.name.lower()).strip()


@dataclass
class Day:
    note: str | None = None        # e.g. "Holiday!"
    items: list[Item] = field(default_factory=list)


@dataclass
class MenuSource:
    url: str
    meal: str
    levels: list[str]


@dataclass
class MonthMenu:
    month: str                     # "2026-09"
    sources: list[MenuSource]
    days: dict[str, Day]           # "2026-09-01" -> Day
    fetched_at: str = ""

    def content_hash(self) -> str:
        payload = json.dumps(
            {"month": self.month, "days": {d: asdict(v) for d, v in self.days.items()}},
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def to_json(self) -> dict:
        return {
            "month": self.month,
            "fetched_at": self.fetched_at,
            "sources": [asdict(s) for s in self.sources],
            "days": {d: asdict(v) for d, v in sorted(self.days.items())},
        }

    @classmethod
    def from_json(cls, raw: dict) -> MonthMenu:
        return cls(
            month=raw["month"],
            fetched_at=raw.get("fetched_at", ""),
            sources=[MenuSource(**s) for s in raw.get("sources", [])],
            days={
                d: Day(note=v.get("note"), items=[Item(**i) for i in v.get("items", [])])
                for d, v in raw["days"].items()
            },
        )


# ---------------------------------------------------------------- scraping

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "lasd-menu/1.0 (+github.com/swamisun)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def find_menu_links(page_html: str) -> list[MenuSource]:
    """Return one MenuSource per distinct PDF, with the levels that link to it."""
    by_url: dict[str, MenuSource] = {}
    for m in re.finditer(r'<a\b[^>]*href="([^"]+\.pdf)"[^>]*>(.*?)</a>', page_html, re.DOTALL | re.IGNORECASE):
        url, label = m.group(1), re.sub(r"<[^>]+>", " ", html.unescape(m.group(2)))
        label = label.lower()
        meal = "BF" if "breakfast" in label else "L" if "lunch" in label else None
        level = "E" if "elementary" in label else "M" if re.search(r"jr|junior|middle", label) else None
        if not meal or not level:
            continue
        src = by_url.setdefault(url, MenuSource(url=url, meal=meal, levels=[]))
        if level not in src.levels:
            src.levels.append(level)
    return list(by_url.values())


def download(src: MenuSource) -> Path:
    CACHE_DIR.mkdir(exist_ok=True)
    path = CACHE_DIR / src.url.rsplit("/", 1)[-1]
    if not path.exists():
        log.info("downloading %s", src.url)
        path.write_bytes(fetch(src.url))
    return path


# ---------------------------------------------------------------- pdf parsing

def _cell_text(page, bbox) -> str:
    return " ".join((page.crop(bbox, strict=False).extract_text() or "").split())


def _explicit_table(page, xs: list[float], ys: list[float]) -> list[tuple[float, float, list[str]]]:
    """Rows of the table bounded by the given rules: (top, bottom, [cell text, ...])."""
    table = page.find_table({
        "vertical_strategy": "explicit",
        "horizontal_strategy": "explicit",
        "explicit_vertical_lines": xs,
        "explicit_horizontal_lines": ys,
    })
    if table is None:
        return []
    return [
        (row.bbox[1], row.bbox[3], [" ".join((c or "").split()) for c in cells])
        for row, cells in zip(table.rows, table.extract())
    ]


def _header_columns(page) -> list[float] | None:
    """Vertical cell edges of the 'Date | Menu Item | Vegetarian | Allergens' table, or None."""
    if "allergens list" not in (page.extract_text() or "").lower():
        return None
    for table in page.find_tables():
        cells = [c for c in table.rows[0].cells if c]
        texts = [_cell_text(page, c).lower() for c in cells]
        if any("date" in t for t in texts) and any("allergen" in t for t in texts):
            return [c[0] for c in cells] + [cells[-1][2]]
    return None


def _row_lines(page, x0: float, x1: float) -> list[float]:
    """Y positions of horizontal rules spanning (almost) the full table width.

    The PDF draws every cell border as its own short segment, so a row rule is
    recognised by the union of segment lengths at that y, not by any one segment.
    """
    bins: dict[float, list[tuple[float, float]]] = {}
    for e in page.edges:
        if e["orientation"] != "h" or e["x1"] < x0 - 2 or e["x0"] > x1 + 2:
            continue
        bins.setdefault(round(e["top"] * 2) / 2, []).append((max(e["x0"], x0), min(e["x1"], x1)))

    clusters: list[tuple[float, list[tuple[float, float]]]] = []
    for top in sorted(bins):
        if clusters and top - clusters[-1][0] <= 3:
            clusters[-1][1].extend(bins[top])
        else:
            clusters.append((top, list(bins[top])))

    lines = []
    for top, segs in clusters:
        covered, cur_x0, cur_x1 = 0.0, None, None
        for s0, s1 in sorted(segs):
            if cur_x1 is None or s0 > cur_x1:
                if cur_x1 is not None:
                    covered += cur_x1 - cur_x0
                cur_x0, cur_x1 = s0, s1
            else:
                cur_x1 = max(cur_x1, s1)
        if cur_x1 is not None:
            covered += cur_x1 - cur_x0
        if covered >= 0.8 * (x1 - x0):
            lines.append(top)
    return lines


DATE_RE = re.compile(r"\b([A-Za-z]{3,9})\.?\s*(\d{1,2})\b")
TITLE_RE = re.compile(r"\b([A-Z][a-z]+)\s+(\d{4})\b")
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NOTE_RE = re.compile(r"(holiday|no school|minimum day)", re.IGNORECASE)


def parse_diet(text: str) -> str:
    t = text.lower()
    if "vegan" in t:
        return "Vegan/GF" if "gf" in t or "gluten" in t else "Vegan"
    if "vegetarian" in t:
        return "Vegetarian"
    return ""


def parse_allergens(text: str) -> list[str]:
    return [a.strip() for a in text.split(",") if a.strip()]


def parse_pdf(path: Path, meal: str) -> tuple[str, dict[str, Day]]:
    """Return (month "YYYY-MM", {date: Day}) parsed from the allergens list pages."""
    with pdfplumber.open(path) as pdf:
        title = pdf.pages[0].extract_text() or ""
        m = TITLE_RE.search(title)
        if not m or m.group(1)[:3].lower() not in MONTHS:
            raise ValueError(f"{path.name}: no 'Month YYYY' title found")
        year, month = int(m.group(2)), MONTHS[m.group(1)[:3].lower()]

        columns: list[float] | None = None
        col_idx: dict[str, int] = {}
        days: dict[str, Day] = {}
        current: date | None = None

        for page in pdf.pages:
            if columns is None:
                columns = _header_columns(page)
                if columns is None:
                    continue
            row_lines = _row_lines(page, columns[0], columns[-1])
            if len(row_lines) < 2:
                continue
            rows = _explicit_table(page, columns, row_lines)

            # The date cell is merged across all of a day's rows and its text sits
            # mid-cell, so read dates from the date column's own row structure and
            # map each item row to the day band that contains its centre.
            day_lines = _row_lines(page, columns[0], columns[1]) or row_lines
            day_bands = _explicit_table(page, columns[:2], day_lines)

            for top, bottom, cells in rows:
                centre = (top + bottom) / 2
                date_txt = next((c[0] for y0, y1, c in day_bands if y0 <= centre < y1), "")
                if not col_idx:
                    low = [c.lower() for c in cells]
                    for name, needle in (("date", "date"), ("item", "menu item"),
                                         ("diet", "veg"), ("allergens", "allergen")):
                        col_idx[name] = next((i for i, c in enumerate(low) if needle in c), -1)
                    if min(col_idx.values()) < 0:
                        raise ValueError(f"{path.name}: unexpected header {cells}")
                    continue

                item_txt = cells[col_idx["item"]]
                diet_txt = cells[col_idx["diet"]]
                allergen_txt = cells[col_idx["allergens"]]

                dm = DATE_RE.search(date_txt)
                weekday = next((i for i, w in enumerate(WEEKDAYS) if w in date_txt.lower()), None)
                if dm and dm.group(1)[:3].lower() in MONTHS:
                    dmonth, dyear = MONTHS[dm.group(1)[:3].lower()], year
                    if dmonth < month and month == 12:   # January days on a December menu
                        dyear += 1
                    current = date(dyear, dmonth, int(dm.group(2)))
                elif weekday is not None and current is not None and current.weekday() != weekday:
                    # A day whose "Sept. 10" half landed on the next page: only the weekday is here.
                    current += timedelta(days=(weekday - current.weekday()) % 7)
                if current is None or not item_txt:
                    continue

                day = days.setdefault(current.isoformat(), Day())
                note = NOTE_RE.search(item_txt)
                if note:
                    day.note = note.group(1).capitalize() + ("!" if "!" in item_txt else "")
                    continue
                day.items.append(Item(
                    meal=meal,
                    name=item_txt,
                    diet=parse_diet(diet_txt),
                    allergens=parse_allergens(allergen_txt),
                ))

        if columns is None:
            raise ValueError(f"{path.name}: no allergens table found")
    return f"{year:04d}-{month:02d}", days


# ---------------------------------------------------------------- merging

def _same_item(existing: dict[tuple[str, str], Item], it: Item) -> Item | None:
    """Exact key match, else a near-identical name in the same meal (typos differ between PDFs)."""
    if it.key in existing:
        return existing[it.key]
    for (meal, name), other in existing.items():
        if meal == it.meal and SequenceMatcher(None, name, it.key[1]).ratio() >= 0.9:
            return other
    return None


def merge_sources(parsed: list[tuple[MenuSource, str, dict[str, Day]]]) -> dict[str, MonthMenu]:
    """Combine every parsed PDF into one MonthMenu per month, tagging items with levels."""
    months: dict[str, MonthMenu] = {}
    for src, month, days in parsed:
        mm = months.setdefault(month, MonthMenu(month=month, sources=[], days={}))
        mm.sources.append(src)
        for d, day in days.items():
            target = mm.days.setdefault(d, Day())
            if day.note and not target.note:
                target.note = day.note
            existing = {it.key: it for it in target.items}
            for it in day.items:
                hit = _same_item(existing, it)
                if hit:
                    hit.levels = sorted(set(hit.levels) | set(src.levels), key=LEVEL_ORDER.index)
                    if not hit.diet and it.diet:
                        hit.diet = it.diet
                else:
                    it.levels = sorted(src.levels, key=LEVEL_ORDER.index)
                    target.items.append(it)
                    existing[it.key] = it
    for mm in months.values():
        for day in mm.days.values():
            day.items.sort(key=lambda i: 0 if i.meal == "BF" else 1)   # stable: keeps PDF order
    return months


# ---------------------------------------------------------------- persistence

def save_month(mm: MonthMenu, now: datetime) -> bool:
    """Write data/YYYY-MM.json only if the menu content changed. Returns True if written."""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{mm.month}.json"
    if path.exists():
        old = MonthMenu.from_json(json.loads(path.read_text()))
        if old.content_hash() == mm.content_hash():
            log.info("%s unchanged", path.name)
            return False
    mm.fetched_at = now.isoformat(timespec="seconds")
    path.write_text(json.dumps(mm.to_json(), indent=2, ensure_ascii=False) + "\n")
    log.info("wrote %s (%d days)", path.name, len(mm.days))
    return True


def load_months() -> dict[str, MonthMenu]:
    return {
        p.stem: MonthMenu.from_json(json.loads(p.read_text()))
        for p in sorted(DATA_DIR.glob("????-??.json"))
    }


def wanted_months(today: date) -> list[str]:
    nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    return [today.strftime("%Y-%m"), nxt.strftime("%Y-%m")]


# ---------------------------------------------------------------- rendering

LEGEND = (
    "🧇 breakfast, 🍴 lunch, 🍕 vegetarian, 🥗 vegan. "
    "🅴 or 🅼 marks an item that is only on the elementary or only on the junior high "
    "(Blach, Egan) menu; no mark means it is on both. Allergens follow the colon. "
    "Every meal comes with fruit, vegetables, and 1% or non-fat milk. Menus are subject to change."
)


def item_label(it: Item) -> str:
    """'🍴 Sbj Sammie 🥗🅴' : meal emoji, name, diet emoji, level emoji when not on both menus."""
    levels = "" if set(it.levels) >= {"E", "M"} else "".join(TAG[l] for l in it.levels)
    return f"{TAG[it.meal]} {it.name} {TAG.get(it.diet, '')}{levels}"


def item_detail(it: Item) -> str:
    """Label plus allergens, for the lists and the calendar description."""
    allergens = ", ".join(it.allergens) if it.allergens else "none listed"
    return f"{item_label(it)}: {allergens}"


def month_title(month: str) -> str:
    y, m = month.split("-")
    return date(int(y), int(m), 1).strftime("%B %Y")


def veg_days(mm: MonthMenu) -> list[tuple[date, Day, list[Item]]]:
    """(date, day, vegetarian items) for every day worth showing, in date order."""
    out = []
    for d, day in sorted(mm.days.items()):
        veg = [it for it in day.items if it.is_veg]
        if veg or day.note:
            out.append((date.fromisoformat(d), day, veg))
    return out


def render_markdown(months: dict[str, MonthMenu], wanted: list[str], updated: str) -> str:
    out = [
        "# LASD vegetarian and vegan menu",
        "",
        f"Source: [{MENU_PAGE}]({MENU_PAGE}). Menu data last updated {updated}.",
        "",
        f"Legend: {LEGEND}",
        "",
        f"Calendar subscription: `{PAGES_URL}menu.ics`",
        "",
    ]
    for month in wanted:
        out += [f"## {month_title(month)}", ""]
        mm = months.get(month)
        if mm is None:
            out += ["_Not posted yet._", ""]
            continue
        for dt, day, veg in veg_days(mm):
            out += [f"### {dt.strftime('%a, %b')} {dt.day}", ""]
            if day.note:
                out.append(f"- {day.note}")
            out += [f"- {item_detail(it)}" for it in veg]
            out.append("")
    return "\n".join(out).rstrip() + "\n"


ICS_URL = PAGES_URL + "menu.ics"
SUBSCRIBE_HTML = f"""<details class="subscribe"><summary>Subscribe to the calendar</summary>
<p>Calendar link: <code>{ICS_URL}</code> (<a href="{ICS_URL.replace('https://', 'webcal://')}">open in
Apple Calendar</a> · <a href="menu.ics">download</a> · <a href="menu.md">markdown</a>)</p>
<ul>
<li><strong>Google Calendar:</strong> Other calendars, <code>+</code>, From URL, paste the link.
Google refreshes subscribed calendars on its own schedule, usually every 12 to 24 hours.</li>
<li><strong>Apple Calendar:</strong> File, New Calendar Subscription, paste the link (or use the
"open in Apple Calendar" link above).</li>
</ul>
<h4>Reminders</h4>
<p>Every event carries two alarms: 9 PM the night before and 7 AM the day of. Whether you see them
depends on the calendar app:</p>
<ul>
<li><strong>Apple Calendar</strong> strips alarms from subscriptions by default. Right-click the
calendar, Get Info, and uncheck "Remove: Alerts".</li>
<li><strong>Google Calendar</strong> ignores alarms inside subscribed feeds. Instead, open the
calendar's settings, find "All-day event notifications", and add two: "the day before at 9:00 PM"
and "the same day at 7:00 AM". Same result.</li>
<li><strong>Outlook</strong> uses the embedded alarms as is.</li>
</ul>
</details>"""


PAGE_CSS = (
    "body{font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:44rem;margin:2rem auto;padding:0 1rem;color:#222}"
    "h2{display:inline;font-size:1.4rem}h3{margin:1.2rem 0 .2rem}"
    ".month{margin-top:1.5rem}.month>summary{cursor:pointer;border-bottom:1px solid #ddd;padding:.3rem 0}"
    ".today{scroll-margin-top:1rem}"
    "ul{margin:0;padding-left:1.2rem}li{margin:.15rem 0}.meta,.legend{color:#555;font-size:.9rem}"
    ".today{background:#fff8dc;margin:0 -.6rem;padding:.1rem .6rem;border-radius:.4rem}"
    ".subscribe{border:1px solid #ddd;border-radius:.4rem;padding:.4rem .8rem;margin:1rem 0;font-size:.95rem}"
    ".subscribe summary{cursor:pointer;font-weight:600}.subscribe h4{margin:.8rem 0 .2rem}"
    ".nav{font-size:.95rem}.nav a{margin-right:.2rem}.toc ul{list-style:none;padding:0}"
    "footer{margin-top:3rem;padding-top:1rem;border-top:1px solid #ddd;font-size:.9rem;color:#555}"
    "@media(prefers-color-scheme:dark){body{background:#111;color:#ddd}.meta,.legend,footer{color:#aaa}"
    ".month>summary,footer{border-color:#333}.today{background:#333300}a{color:#8cf}.subscribe{border-color:#333}}"
)
PAGE_SCRIPT = (
    "function openTarget(){const t=location.hash&&document.querySelector(location.hash);"
    "if(!t)return;const d=t.closest('details');if(d)d.open=true;t.scrollIntoView();}"
    "addEventListener('hashchange',openTarget);openTarget();"
)


def html_page(title: str, body: list[str]) -> str:
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head><body>\n"
        + "\n".join(body)
        + f"\n<script>{PAGE_SCRIPT}</script>\n</body></html>\n"
    )


def month_section(month: str, mm: MonthMenu | None, today: date, open_: bool) -> list[str]:
    esc = html.escape
    opener = f'<details class="month" id="month-{month}"{" open" if open_ else ""}>'
    out = [f"{opener}<summary><h2>{month_title(month)}</h2></summary>"]
    if mm is None:
        return out + ["<p><em>Not posted yet.</em></p></details>"]
    for dt, day, veg in veg_days(mm):
        attrs = ' class="day today" id="today"' if dt == today else ' class="day"'
        out.append(f"<section{attrs}><h3>{dt.strftime('%a, %b')} {dt.day}</h3><ul>")
        if day.note:
            out.append(f"<li><strong>{esc(day.note)}</strong></li>")
        out += [f"<li>{esc(item_detail(it))}</li>" for it in veg]
        out.append("</ul></section>")
    return out + ["</details>"]


def render_html(months: dict[str, MonthMenu], wanted: list[str], updated: str, today: date) -> str:
    body = [
        "<h1>LASD vegetarian and vegan menu</h1>",
        f'<p class="meta">Source: <a href="{MENU_PAGE}">{MENU_PAGE}</a>. Menu data last updated {updated}.</p>',
        f'<p class="legend">{html.escape(LEGEND)}</p>',
        SUBSCRIBE_HTML,
    ]
    nav = [f'<a href="#month-{m}">{month_title(m)}</a>' for m in wanted]
    if any(today.isoformat() in mm.days for m, mm in months.items() if m in wanted):
        nav.insert(0, '<a href="#today">Today</a>')
    body.append(f'<p class="nav">Jump to: {" · ".join(nav)}</p>')
    for month in wanted:
        body += month_section(month, months.get(month), today, open_=True)
    body.append(
        '<footer><a href="archive.html">Past months</a> · <a href="menu.md">markdown</a> · '
        f'<a href="{REPO_URL}">source</a></footer>'
    )
    return html_page("LASD veg menu", body)


def render_archive(months: dict[str, MonthMenu], wanted: list[str], today: date) -> str:
    """Every month not on the main page, newest first, collapsed, with a year/month table of contents."""
    past = sorted((m for m in months if m not in wanted), reverse=True)
    body = ["<h1>LASD veg menu archive</h1>",
            '<p class="meta"><a href="index.html">Back to the current menu</a></p>']
    if not past:
        body.append("<p><em>Nothing archived yet. Months move here once they are no longer current.</em></p>")
    else:
        body.append('<nav class="toc"><ul>')
        for year in sorted({m[:4] for m in past}, reverse=True):
            links = " · ".join(f'<a href="#month-{m}">{date(int(m[:4]), int(m[5:]), 1).strftime("%B")}</a>'
                               for m in past if m.startswith(year))
            body.append(f"<li><strong>{year}:</strong> {links}</li>")
        body.append("</ul></nav>")
        for month in past:
            body += month_section(month, months[month], today, open_=False)
    body.append(f'<footer><a href="index.html">Current menu</a> · <a href="{REPO_URL}">source</a></footer>')
    return html_page("LASD veg menu archive", body)


REMINDERS = (timedelta(hours=-3), timedelta(hours=7))   # 9 PM the night before, 7 AM the day of


def render_ics(months: dict[str, MonthMenu], wanted: list[str], stamp: datetime) -> bytes:
    """One all-day event per meal per school day, each with the reminders above."""
    cal = Calendar()
    cal.add("prodid", "-//swamisun//lasd-menu//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", "LASD veg menu")
    cal.add("x-wr-timezone", "America/Los_Angeles")
    cal.add("x-published-ttl", "P1D")
    cal.add("refresh-interval", vDuration(timedelta(days=1)), parameters={"VALUE": "DURATION"})
    dtstamp = stamp.astimezone(UTC)

    for month in wanted:
        mm = months.get(month)
        if mm is None:
            continue
        for dt, _day, veg in veg_days(mm):
            for meal in ("BF", "L"):
                items = [it for it in veg if it.meal == meal]
                if not items:
                    continue
                summary = " · ".join(item_label(it) for it in items)
                ev = Event()
                ev.add("uid", f"{dt.isoformat()}-{meal.lower()}@lasd-menu.swamisun")
                ev.add("dtstamp", dtstamp)
                ev.add("dtstart", dt)
                ev.add("dtend", dt + timedelta(days=1))
                ev.add("summary", summary)
                ev.add("description", "\n".join(item_detail(it) for it in items))
                ev.add("categories", [MEAL_NAME[meal]])
                ev.add("url", MENU_PAGE)
                ev.add("transp", "TRANSPARENT")
                for trigger in REMINDERS:
                    alarm = Alarm()
                    alarm.add("action", "DISPLAY")
                    alarm.add("trigger", trigger)
                    alarm.add("description", f"{MEAL_NAME[meal]} {dt.strftime('%a %b')} {dt.day}: {summary}")
                    ev.add_component(alarm)
                cal.add_component(ev)
    return cal.to_ical()


def render(today: date) -> None:
    months = load_months()
    wanted = wanted_months(today)
    stamps = [mm.fetched_at for m, mm in months.items() if m in wanted and mm.fetched_at]
    latest = datetime.fromisoformat(max(stamps)) if stamps else datetime.now(TZ)
    updated = latest.strftime("%Y-%m-%d")
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / ".nojekyll").touch()
    MD_OUT.write_text(render_markdown(months, wanted, updated))
    HTML_OUT.write_text(render_html(months, wanted, updated, today))
    ARCHIVE_OUT.write_text(render_archive(months, wanted, today))
    ICS_OUT.write_bytes(render_ics(months, wanted, latest))
    log.info("rendered %s for %s", ", ".join(p.name for p in (MD_OUT, HTML_OUT, ARCHIVE_OUT, ICS_OUT)), ", ".join(wanted))


# ---------------------------------------------------------------- entry point

def update(today: date, now: datetime) -> None:
    page = fetch(MENU_PAGE).decode("utf-8", errors="replace")
    sources = find_menu_links(page)
    if not sources:
        raise SystemExit(f"no menu PDFs found on {MENU_PAGE}")
    parsed = []
    for src in sources:
        month, days = parse_pdf(download(src), src.meal)
        log.info("%s: %s %s %s, %d days", Path(src.url).name, month, src.meal, src.levels, len(days))
        parsed.append((src, month, days))
    for month, mm in merge_sources(parsed).items():
        if month not in wanted_months(today):
            log.info("skipping %s (not current or next month)", month)
            continue
        save_month(mm, now)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--render", action="store_true", help="render from data/ only; skip download and parse")
    ap.add_argument("--today", type=date.fromisoformat, help="override today's date (YYYY-MM-DD)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(message)s")

    now = datetime.now(TZ)
    today = args.today or now.date()
    if not args.render:
        update(today, now)
    render(today)


if __name__ == "__main__":
    main()
