"""Make web results honest about time: label the dates they mention against today, and clean their links.

On Oct 26 a search snippet said "Saturday, Oct. 17" (no year) and the model called it "this Saturday, Oct 31",
then confirmed it when asked to double-check. Models are bad at noticing that a date has passed; code isn't.
Every web_search and web_fetch result passes through annotate() in the agent loop, so the model sees e.g.
"Oct. 17 = Saturday 2026-10-17, 9 days ago (no year given)" right next to the snippet.
"""
import re
from datetime import date
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

MONTHS = {m: i for i, names in enumerate(
    [("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",), ("jun", "june"),
     ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"), ("oct", "october"), ("nov", "november"),
     ("dec", "december")], start=1) for m in names}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|" \
         r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?"
_DAY = r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
_WEEKDAY = r"(?:(?P<weekday>mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,?\s+)?"
_YEAR = r"(?:,?\s+(?P<year>\d{4}))?"
PATTERNS = [
    re.compile(rf"\b{_WEEKDAY}{_MONTH.replace('(', '(?P<month>', 1)}\s+{_DAY}\b{_YEAR}", re.I),  # Sat, Oct. 17
    re.compile(rf"\b{_WEEKDAY}{_DAY}\s+(?:of\s+)?{_MONTH.replace('(', '(?P<month>', 1)}{_YEAR}", re.I),  # 17 Oct 2026
    re.compile(r"\b(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})\b"),  # 2026-10-31
]
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
FULL_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
TRACKING = re.compile(r"^(utm_\w+|srsltid|gclid|fbclid|mc_cid|mc_eid|_hsenc|_hsmktracking|ref_src)$", re.I)
MAX_PER_RESULT, MAX_PER_PAGE = 3, 8


def mentioned_dates(text: str, today: date, limit: int = MAX_PER_RESULT) -> list[str]:
    """Each date in the text, worked out against today. Without a year: the nearest one (within ~6 months).
    A weekday that doesn't match ("Saturday, Oct 18" when Oct 18 is a Sunday this year) flags an old listing."""
    found, seen = [], set()
    for pattern in PATTERNS:
        for match in pattern.finditer(text or ""):
            year = match.group("year")
            try:
                when = date(int(year or today.year), _month(match.group("month")), int(match.group("day")))
                if not year and abs((when - today).days) > 183:  # "March 3", read in October: next March
                    when = when.replace(year=when.year + (1 if when < today else -1))
            except (TypeError, ValueError):
                continue  # "May 45", a month name that isn't one...
            if when in seen:
                continue
            seen.add(when)
            notes = [_relative(when, today)] + ([] if year else ["no year given"])
            weekday = (match.groupdict().get("weekday") or "").lower()
            if weekday and WEEKDAYS.index(weekday) != when.weekday():
                notes.append(f"it says {FULL_WEEKDAYS[WEEKDAYS.index(weekday)]} but that date is a "
                             f"{when.strftime('%A')}: probably a listing from another year")
            found.append((match.start(), f"{match.group(0).strip()} = {when.strftime('%A %Y-%m-%d')} "
                                         f"({'; '.join(notes)})"))
    return [label for _, label in sorted(found)][:limit]


def weekday_slips(text: str, today: date, near_days: int = 60) -> list[str]:
    """Dates in a reply whose weekday is wrong, as corrections: "October 28, 2026 is a Wednesday, not a Saturday".
    On Oct 27 a reply said "Saturday October 28 2026" and, a sentence later, "Saturday October 31". Only dates that
    name a weekday are checked: with a year, always; without one, only within `near_days` of today, where the
    nearest year is surely the one meant ("Tuesday, January 28" about a 2020 ticket is left alone)."""
    slips = []
    for pattern in PATTERNS:
        for match in pattern.finditer(text or ""):
            weekday, year = (match.groupdict().get("weekday") or "").lower(), match.group("year")
            if not weekday:
                continue
            try:
                when = date(int(year or today.year), _month(match.group("month")), int(match.group("day")))
                if not year:
                    when = min((when.replace(year=y) for y in (today.year - 1, today.year, today.year + 1)),
                               key=lambda d: abs((d - today).days))
            except (TypeError, ValueError):
                continue
            if not year and abs((when - today).days) > near_days:
                continue
            said = FULL_WEEKDAYS[WEEKDAYS.index(weekday)]
            if WEEKDAYS.index(weekday) != when.weekday():
                slip = f"{when.strftime('%B')} {when.day}, {when.year} is a {when.strftime('%A')}, not a {said}"
                if slip not in slips:
                    slips.append(slip)
    return slips


def clean_url(url: str) -> str:
    """Drop tracking codes (utm_*, srsltid...) so links are shorter and point to the page itself."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def annotate(tool: str, result: dict, today: date) -> dict:
    """web_search: clean each link and add the dates its title and snippet mention. web_fetch: the page's dates."""
    if "error" in result:
        return result
    if tool == "web_search":
        for item in result.get("results", []):
            item["url"] = clean_url(item.get("url", ""))
            if dates := mentioned_dates(f"{item.get('title', '')} {item.get('snippet', '')}", today):
                item["dates"] = dates
    elif tool == "web_fetch" and (dates := mentioned_dates(result.get("content", ""), today, MAX_PER_PAGE)):
        result["dates"] = dates
    return result


def _month(token: str) -> int | None:
    token = token.lower().rstrip(".")
    return int(token) if token.isdigit() else MONTHS.get(token) or MONTHS.get(token[:3])


def _relative(when: date, today: date) -> str:
    days = (when - today).days
    return {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days) or (
        f"in {days} days" if days > 0 else f"{-days} days ago, already past")
