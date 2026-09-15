# lasd-menu

Vegetarian and vegan options from the Los Altos School District breakfast and lunch menus, as a
calendar you can subscribe to and a page you can skim.

Published at <https://swamisun.github.io/lasd-menu/>:

- `menu.ics`: two all-day events per school day, one for breakfast and one for lunch. The title
  lists the veg items with emoji tags, the description adds allergens.
- `index.html` and `menu.md`: the same menu as a list, one section per day.

The repo also keeps `data/YYYY-MM.json` with everything parsed from the PDFs (veg and non-veg), so
the filters and formatting can change without re-downloading.

Covers the current month and the next one, as soon as the district posts them.

## Subscribe to the calendar

```
https://swamisun.github.io/lasd-menu/menu.ics
```

- Google Calendar: Other calendars, `+`, From URL, paste the link.
- Apple Calendar: File, New Calendar Subscription, paste the link.

Google refreshes subscribed calendars on its own schedule, usually every 12 to 24 hours.

## Legend

| Emoji | Meaning |
| --- | --- |
| 🧇 | breakfast |
| 🍴 | lunch |
| 🍕 | vegetarian |
| 🥗 | vegan (the district says vegan meals are gluten free, not certified) |
| 🅴 | only on the elementary menu |
| 🅼 | only on the junior high menu (Blach, Egan) |

No 🅴 or 🅼 means the item is on both menus, which is most of them. Every meal also comes with
fruit, vegetables, and milk. The emoji live in one table in `lasd_menu.py` (`TAG`).

## How it works

1. Scrape <https://www.lasdschools.org/menus> for the menu PDF links. Each link's label says which
   meal and school level it is for. The breakfast PDF is shared by both levels.
2. Parse the "Allergens List" pages of each PDF with [pdfplumber](https://github.com/jsvine/pdfplumber).
   Those pages are a real table with one row per item: date, item, diet flag, allergens. The
   calendar-grid pages are only used for the month title.
3. Merge the elementary and junior high lists per day. Near-identical names (the two lunch PDFs
   differ by the odd typo) are treated as the same item.
4. Save `data/YYYY-MM.json` if the content changed, then render everything under `docs/`.

## Running locally

Needs [uv](https://docs.astral.sh/uv/).

```
uv run lasd_menu.py --verbose            # fetch, parse, render
uv run lasd_menu.py --render             # re-render from data/ without touching the network
uv run lasd_menu.py --today 2026-10-01   # pretend it is another date
```

Downloaded PDFs are cached in `cache/` (gitignored). Delete a cached PDF to force a re-download.

## Automation

`.github/workflows/update.yml` runs every Monday morning Pacific time, on manual dispatch, and
whenever the script changes. It re-runs the pipeline and commits `data/` and `docs/` only if
something changed, so the history shows exactly when the district revised a menu. GitHub Pages
serves `docs/` from the `main` branch, so a commit is all it takes to publish.
