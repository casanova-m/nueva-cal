#!/usr/bin/env python3
"""
Family calendar filter.

Reads config.yaml, fetches each source iCalendar feed (URLs supplied via
environment variables to keep them out of the repo), filters events using
keyword/category rules, deduplicates events that appear in multiple feeds,
and writes:
  - output/family-<token>.ics   The combined, filtered calendar
  - output/index.html           A human-readable preview page showing
                                 what was included, what was excluded,
                                 and why -- so you can tune the rules
                                 without flying blind.
"""

import html
import hashlib
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
import yaml
from icalendar import Calendar


# ---------- small helpers ----------

def webcal_to_https(url: str) -> str:
    """Calendar tools sometimes hand you webcal://, but HTTP fetchers need https://."""
    if url.startswith("webcal://"):
        return "https://" + url[len("webcal://"):]
    return url


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lower())


def stringify_datetime(value) -> str:
    """Stable string form of a DTSTART value (which may be a date OR a datetime)."""
    if value is None:
        return ""
    try:
        dt = value.dt
    except AttributeError:
        return str(value)
    if isinstance(dt, (datetime, date)):
        return dt.isoformat()
    return str(dt)


def get_event_date(event):
    """Return the event's start as a date object, or None if unparseable."""
    dtstart = event.get("DTSTART")
    if dtstart is None:
        return None
    try:
        dt = dtstart.dt
        if isinstance(dt, datetime):
            return dt.date()
        if isinstance(dt, date):
            return dt
    except AttributeError:
        pass
    return None


def dedupe_key(event) -> str:
    """Stable key for cross-feed dedup. Same SUMMARY + DTSTART => same event,
    regardless of which feed it came from or what UID it happens to have."""
    summary = normalize(event.get("SUMMARY", ""))
    dt = stringify_datetime(event.get("DTSTART"))
    return hashlib.sha256(f"{summary}|{dt}".encode("utf-8")).hexdigest()


def get_searchable_text(event) -> str:
    """Pulls together all text fields we'll match keywords against."""
    parts = []
    for key in ("SUMMARY", "DESCRIPTION", "LOCATION", "CATEGORIES"):
        val = event.get(key)
        if val:
            parts.append(str(val))
    return normalize(" ".join(parts))


def matches_any(text: str, patterns) -> bool:
    """Case-insensitive match. Uses word boundaries for short tokens
    (<=6 chars, no spaces) to avoid 'g5' matching 'g500', and substring
    matching for longer phrases."""
    if not patterns:
        return False
    for raw in patterns:
        pat = normalize(raw)
        if not pat:
            continue
        if len(pat) <= 6 and " " not in pat:
            if re.search(r"\b" + re.escape(pat) + r"\b", text):
                return True
        else:
            if pat in text:
                return True
    return False


# ---------- grade computation ----------
#
# The script computes each student's current grade from their graduation year
# and the event's own date. This means grade rules work correctly for ALL
# years automatically -- no annual config edits required.
#
# Formula: school years run Aug-Jun. An event in Sep 2026 is in the school
# year ending spring 2027. A student graduating in 2027 is therefore in
# 12th grade for that event. One graduating in 2033 would be in 6th.

# Full-text keywords for each grade (include matching).
# Bare ordinals (5th, 6th ...) use word-boundary matching in matches_any().
GRADE_INCLUDE_KEYWORDS = {
    5:  ["5th grade", "grade 5", "5th-grade", "g5", "5th", "gr. 5"],
    6:  ["6th grade", "grade 6", "6th-grade", "g6", "6th", "gr. 6", "6th gr"],
    7:  ["7th grade", "grade 7", "7th-grade", "g7", "7th", "gr. 7", "7th gr"],
    8:  ["8th grade", "grade 8", "8th-grade", "g8", "8th", "gr. 8", "[gr. 8]", "8th gr"],
    9:  ["9th grade", "grade 9", "9th-grade", "g9", "9th", "gr. 9", "[gr. 9]", "9th gr",
         "freshman", "freshmen"],
    10: ["10th grade", "grade 10", "10th-grade", "g10", "10th", "gr. 10", "[gr. 10]",
         "sophomore", "sophomores"],
    11: ["11th grade", "grade 11", "11th-grade", "g11", "11th", "gr. 11",
         "junior", "juniors"],
    12: ["12th grade", "grade 12", "12th-grade", "g12", "12th", "gr. 12",
         "senior", "seniors"],
}

# Title-only keywords for each grade (exclude matching -- no bare ordinals
# to avoid false positives from ordinal use in event titles).
GRADE_TITLE_KEYWORDS = {
    5:  ["5th grade", "grade 5", "5th-grade", "gr. 5"],
    6:  ["6th grade", "grade 6", "6th-grade", "gr. 6", "6th gr"],
    7:  ["7th grade", "grade 7", "7th-grade", "gr. 7", "7th gr"],
    8:  ["8th grade", "grade 8", "8th-grade", "gr. 8", "[gr. 8]", "8th gr"],
    9:  ["9th grade", "grade 9", "9th-grade", "gr. 9", "[gr. 9]", "9th gr"],
    10: ["10th grade", "grade 10", "10th-grade", "gr. 10", "[gr. 10]"],
    11: ["11th grade", "grade 11", "11th-grade", "gr. 11"],
    12: ["12th grade", "grade 12", "12th-grade", "gr. 12"],
}

ALL_GRADES = list(range(5, 13))  # grades 5 through 12


def school_year_spring(event_date: date) -> int:
    """Return the calendar year the school year ends.
    Aug-Dec of year N → school year ends spring N+1.
    Jan-Jul of year N → school year ends spring N."""
    return event_date.year + 1 if event_date.month >= 8 else event_date.year


def student_grade(graduation_year: int, event_date: date) -> int:
    """Return the grade (clamped 5-12) a student is in for the given date."""
    grade = 12 - (graduation_year - school_year_spring(event_date))
    return max(5, min(12, grade))


# Regex patterns that map a grade-ordinal token to a grade number.
# Used to detect grades named in an event TITLE so we can gate by cohort.
_ORDINAL_PATTERNS = [
    (1,  r"\b1st\b|\bgrade\s*1\b|\bgr\.?\s*1\b|\bfirst\s+grade\b"),
    (2,  r"\b2nd\b|\bgrade\s*2\b|\bgr\.?\s*2\b|\bsecond\s+grade\b"),
    (3,  r"\b3rd\b|\bgrade\s*3\b|\bgr\.?\s*3\b|\bthird\s+grade\b"),
    (4,  r"\b4th\b|\bgrade\s*4\b|\bgr\.?\s*4\b|\bfourth\s+grade\b"),
    (5,  r"\b5th\b|\bgrade\s*5\b|\bgr\.?\s*5\b|\bfifth\s+grade\b"),
    (6,  r"\b6th\b|\bgrade\s*6\b|\bgr\.?\s*6\b|\bsixth\s+grade\b"),
    (7,  r"\b7th\b|\bgrade\s*7\b|\bgr\.?\s*7\b|\bseventh\s+grade\b"),
    (8,  r"\b8th\b|\bgrade\s*8\b|\bgr\.?\s*8\b|\beighth\s+grade\b"),
    (9,  r"\b9th\b|\bgrade\s*9\b|\bgr\.?\s*9\b|\bninth\s+grade\b|\bfreshman\b|\bfreshmen\b"),
    (10, r"\b10th\b|\bgrade\s*10\b|\bgr\.?\s*10\b|\btenth\s+grade\b|\bsophomore"),
    (11, r"\b11th\b|\bgrade\s*11\b|\bgr\.?\s*11\b|\beleventh\s+grade\b|\bjunior"),
    (12, r"\b12th\b|\bgrade\s*12\b|\bgr\.?\s*12\b|\btwelfth\s+grade\b|\bsenior"),
]


def grades_named_in_title(summary: str) -> set:
    """Return the set of grade numbers (5-12) explicitly named in a title.
    Grades 1-4 are detected but mapped out (we never want them), so a title
    naming only K-4 returns those low numbers -- which will never intersect
    our students' grades, correctly excluding the event.
    Ranges like '7th-8th' or '5th & 6th' yield both numbers."""
    found = set()
    for grade, pattern in _ORDINAL_PATTERNS:
        if re.search(pattern, summary):
            found.add(grade)
    return found


def compute_grades(cfg: dict, event_date) -> tuple:
    """Return (current_grades: set, upcoming_grades: set).
    upcoming_grades = each student's current grade + 1, used to handle
    'Rising X Grade' events which use NEXT year's grade number."""
    if event_date is None:
        return set(), set()
    current, upcoming = set(), set()
    for s in cfg.get("students", []):
        yr = s.get("graduation_year")
        if yr:
            g = student_grade(yr, event_date)
            current.add(g)
            if g < 12:
                upcoming.add(g + 1)
    return current, upcoming


# ---------- filter logic ----------


def classify(event, feed_name: str, cfg: dict):
    """Return (included, reason).

    Order of precedence (highest to lowest):
      1. always_exclude_keywords (full text)  -> exclude, no rescue.
      2. transition_keywords (full text, date-aware) -> exclude if before
         transition_date, include if on/after.  Used for senior milestone
         events that flip from wrong-class to right-class on a known date.
      3. always_include_feeds                 -> include, no filtering.
      4. Dynamic grade logic (computed from students' graduation years):
         a. Title contains a grade that is neither current NOR upcoming
            -> exclude, unless rescued by an active-grade title match,
               an upcoming-grade-with-'rising' match, or a static
               title_rescue_keyword.
         b. Full text matches a current-grade keyword -> include.
      5. title_excludes (static, TITLE ONLY)  -> exclude unless rescued
         by title_rescue_keywords.  Handles K-4, wrong class years, etc.
      6. include_keywords (full text)         -> include.
      7. exclude_keywords (full text)         -> exclude.
      8. feed_excludes[feed_name]             -> exclude.
      9. feed_defaults[feed_name]             -> include or exclude.
    """
    text = get_searchable_text(event)
    summary = normalize(str(event.get("SUMMARY", "")))
    event_date_val = get_event_date(event)

    # 1. Always exclude
    if matches_any(text, cfg.get("always_exclude_keywords", [])):
        return False, "matched always-exclude keyword"

    # 2. Date-based transition keywords (senior milestones etc.)
    transition_date_str = cfg.get("transition_date")
    if transition_date_str:
        td = date.fromisoformat(transition_date_str)
        transition_kws = cfg.get("transition_keywords", [])
        if transition_kws and event_date_val:
            if event_date_val < td:
                if matches_any(text, transition_kws):
                    return False, f"pre-transition exclude (before {transition_date_str})"
            else:
                if matches_any(text, transition_kws):
                    return True, f"post-transition include (on/after {transition_date_str})"

    # 3. Always include feeds
    if feed_name in cfg.get("always_include_feeds", []):
        return True, "always-include feed"

    # 4. Dynamic grade logic
    current_grades, upcoming_grades = compute_grades(cfg, event_date_val)
    if current_grades:
        active_title_kws   = [kw for g in current_grades
                              for kw in GRADE_TITLE_KEYWORDS.get(g, [])]
        upcoming_title_kws = [kw for g in upcoming_grades
                              for kw in GRADE_TITLE_KEYWORDS.get(g, [])]
        inactive_title_kws = [kw for g in ALL_GRADES
                              if g not in current_grades
                              for kw in GRADE_TITLE_KEYWORDS.get(g, [])]
        active_include_kws = [kw for g in current_grades
                              for kw in GRADE_INCLUDE_KEYWORDS.get(g, [])]

        # Detect grade ordinals appearing ANYWHERE in the title (e.g. "5th
        # Parent Social", "7th-8th STUCO Social"). We scan for bare ordinals
        # 1st-12th plus "kindergarten"/"K". If a title names grade(s) and NONE
        # of them are active/upcoming for our students, the event belongs to
        # another cohort -- exclude it, regardless of include keywords like
        # "parent" or grade mentions buried in the description.
        title_grades = grades_named_in_title(summary)
        if title_grades:
            rising_context = bool(matches_any(summary, ["rising", "new ", "welcome", "incoming"]))
            # Current grades always count. Upcoming grades count ONLY when the
            # title has a rising/welcome/incoming cue -- otherwise a plain
            # "7th-8th Social" would wrongly match a 6th grader via upcoming=7.
            relevant = set(current_grades)
            if rising_context:
                relevant |= upcoming_grades
            named_relevant = title_grades & relevant

            if not named_relevant:
                if not matches_any(summary, cfg.get("title_rescue_keywords", [])):
                    return False, f"title names non-active grades {sorted(title_grades)}"
            else:
                # "rising X" where X is a CURRENT grade = previous cohort, wrong.
                if matches_any(summary, ["rising"]):
                    if (title_grades & current_grades) and not (title_grades & upcoming_grades):
                        return False, "rising event for current grade (wrong cohort)"
                return True, f"title names active grade {sorted(named_relevant)}"

        if matches_any(summary, inactive_title_kws):
            active_rescue_kws  = [kw for g in current_grades
                                  for kw in GRADE_INCLUDE_KEYWORDS.get(g, [])]
            rescued_by_active   = matches_any(summary, active_rescue_kws)
            rescued_by_rising   = (matches_any(summary, ["rising"]) and
                                   matches_any(summary, upcoming_title_kws))
            rescued_by_static   = matches_any(summary, cfg.get("title_rescue_keywords", []))

            if not (rescued_by_active or rescued_by_rising or rescued_by_static):
                return False, f"grade not relevant to {sorted(current_grades)}"
            if rescued_by_rising and not rescued_by_active:
                return True, f"rising-grade event (upcoming: {sorted(upcoming_grades)})"
            # rescued_by_active or rescued_by_static: fall through

        # "Rising X" events where X is the student's CURRENT grade are for
        # the PREVIOUS cohort -- e.g., "Rising 6th Grade Coffee" when Zander
        # is ALREADY in 6th means it's for families of current 5th graders.
        if matches_any(summary, ["rising"]):
            current_title_kws = [kw for g in current_grades
                                 for kw in GRADE_TITLE_KEYWORDS.get(g, [])]
            if matches_any(summary, current_title_kws):
                return False, f"rising event for current grade (wrong cohort)"

        if matches_any(text, active_include_kws):
            return True, f"matched grade {sorted(current_grades)} keyword"

    # 5. Static title excludes (K-4, wrong class years, etc.)
    if matches_any(summary, cfg.get("title_excludes", [])):
        if not matches_any(summary, cfg.get("title_rescue_keywords", [])):
            return False, "title-excluded (no rescue match)"

    # 6. Static include keywords
    if matches_any(text, cfg.get("include_keywords", [])):
        return True, "matched include keyword"

    # 7. Static exclude keywords
    if matches_any(text, cfg.get("exclude_keywords", [])):
        return False, "matched exclude keyword"

    # 8. Feed-specific excludes
    feed_specific = cfg.get("feed_excludes", {}).get(feed_name, [])
    if matches_any(text, feed_specific):
        return False, f"matched {feed_name}-specific exclude"

    # 9. Feed defaults
    default = cfg.get("feed_defaults", {}).get(feed_name, "exclude")
    return (default == "include"), f"feed default: {default}"


# ---------- fetch + build ----------

def fix_phantom_allday(ev):
    """Some source feeds publish all-day observances (holidays, etc.) with a
    bogus midnight start time -- e.g. DTSTART:20261101T000000,
    DTEND:20261101T010000 -- so they render as a 12-1am sliver instead of an
    all-day banner. Detect that exact shape and rewrite the event as a proper
    all-day (VALUE=DATE) event.

    Only triggers when:
      - DTSTART is a datetime (not already a date) at exactly 00:00:00, AND
      - DTEND is missing, or is exactly midnight (any later day), or is
        exactly 01:00 the same day.
    A real timed event (e.g. Intersession 8:55am, a 7pm social) never starts
    at exactly midnight, so this leaves those untouched.
    """
    dtstart = ev.get("DTSTART")
    if dtstart is None:
        return
    start = getattr(dtstart, "dt", None)
    # Already a pure date (true all-day) -> nothing to do.
    if not isinstance(start, datetime):
        return
    if (start.hour, start.minute, start.second) != (0, 0, 0):
        return  # not a midnight start; leave real timed events alone

    dtend = ev.get("DTEND")
    end = getattr(dtend, "dt", None) if dtend is not None else None

    start_date = start.date()
    end_date = None
    if end is None:
        end_date = start_date + timedelta(days=1)
    elif isinstance(end, datetime):
        if (end.hour, end.minute, end.second) == (0, 0, 0):
            # midnight-to-midnight: spans (end_date - start_date) full days
            end_date = end.date()
            if end_date <= start_date:
                end_date = start_date + timedelta(days=1)
        elif (end.hour, end.minute, end.second) == (1, 0, 0) and end.date() == start_date:
            # the phantom "12-1am" shape -> single all-day event
            end_date = start_date + timedelta(days=1)
        else:
            return  # real timed event ending at some other time; leave alone
    else:
        # end is already a date
        end_date = end if end > start_date else start_date + timedelta(days=1)

    # Rewrite as all-day (VALUE=DATE). icalendar emits DATE value type when
    # the value is a datetime.date that is not a datetime.
    del ev["DTSTART"]
    ev.add("DTSTART", start_date)
    if "DTEND" in ev:
        del ev["DTEND"]
    ev.add("DTEND", end_date)


def fetch_feed(url: str, timeout: int = 30) -> Calendar:
    real_url = webcal_to_https(url)
    resp = requests.get(
        real_url,
        timeout=timeout,
        headers={"User-Agent": "FamilyCalendarFilter/1.0"},
    )
    resp.raise_for_status()
    return Calendar.from_ical(resp.content)


def main():
    cfg_path = os.environ.get("CONFIG_PATH", "config.yaml")
    with open(cfg_path) as f:
        cfg = yaml.safe_load(f)

    out_cal = Calendar()
    out_cal.add("prodid", "-//Family Filtered Calendar//EN")
    out_cal.add("version", "2.0")
    out_cal.add("x-wr-calname", cfg.get("output_name", "Family Filtered"))
    out_cal.add("x-wr-timezone", cfg.get("timezone", "America/Los_Angeles"))

    seen_keys = set()
    preview_rows = []
    stats = {"feeds": 0, "events": 0, "duplicates": 0,
             "included": 0, "excluded": 0, "errors": 0}
    timezones_added = set()

    for feed in cfg.get("feeds", []):
        name = feed["name"]
        url_env = feed.get("url_env")
        url = os.environ.get(url_env, "") if url_env else feed.get("url", "")
        if not url:
            print(f"WARN: feed '{name}' has no URL "
                  f"(env var {url_env} not set)", file=sys.stderr)
            continue

        print(f"Fetching feed: {name}", file=sys.stderr)
        try:
            cal = fetch_feed(url)
        except Exception as e:
            print(f"  ERROR fetching {name}: {e}", file=sys.stderr)
            stats["errors"] += 1
            continue
        stats["feeds"] += 1

        # Copy VTIMEZONE blocks once so downstream clients can resolve times
        for tz in cal.walk("VTIMEZONE"):
            tzid = str(tz.get("TZID", ""))
            if tzid and tzid not in timezones_added:
                out_cal.add_component(tz)
                timezones_added.add(tzid)

        for ev in cal.walk("VEVENT"):
            stats["events"] += 1
            included, reason = classify(ev, name, cfg)
            key = dedupe_key(ev)

            preview_rows.append({
                "feed": name,
                "summary": str(ev.get("SUMMARY", "")).strip(),
                "dtstart": stringify_datetime(ev.get("DTSTART")),
                "included": included,
                "reason": reason,
                "duplicate": included and key in seen_keys,
            })

            if not included:
                stats["excluded"] += 1
                continue
            if key in seen_keys:
                stats["duplicates"] += 1
                continue

            seen_keys.add(key)
            stats["included"] += 1

            # Deterministic UID so Google Calendar tracks the same logical
            # event across runs even if dedup picks a different source next time.
            stable_uid = f"{key}@family-filter"
            if "UID" in ev:
                del ev["UID"]
            ev.add("UID", stable_uid)
            ev.add("X-FAMILY-FILTER-SOURCE", name)

            # Repair all-day observances that the source feed published with a
            # bogus midnight start time (so they render as all-day banners).
            fix_phantom_allday(ev)

            out_cal.add_component(ev)

    # ---- write outputs ----
    output_dir = Path(cfg.get("output_dir", "output"))
    output_dir.mkdir(parents=True, exist_ok=True)

    out_name = cfg.get("output_filename", "family.ics")
    token = os.environ.get("OUTPUT_TOKEN", "").strip()
    if token:
        stem, _, ext = out_name.rpartition(".")
        out_name = f"{stem}-{token}.{ext}"
    out_path = output_dir / out_name
    with open(out_path, "wb") as f:
        f.write(out_cal.to_ical())

    write_preview(output_dir / "index.html", out_name, preview_rows, stats, cfg)

    print(f"Stats: {stats}", file=sys.stderr)
    print(f"Wrote: {out_path}", file=sys.stderr)


def write_preview(path: Path, ics_filename: str, rows, stats, cfg):
    included = sorted([r for r in rows if r["included"]],
                      key=lambda r: r["dtstart"])
    excluded = sorted([r for r in rows if not r["included"]],
                      key=lambda r: r["dtstart"])

    def row_html(r):
        dup = " <small>(duplicate, kept once)</small>" if r.get("duplicate") else ""
        return (
            f"<tr><td>{html.escape(r['dtstart'][:16])}</td>"
            f"<td>{html.escape(r['summary'])}</td>"
            f"<td class='feed'>{html.escape(r['feed'])}</td>"
            f"<td class='reason'>{html.escape(r['reason'])}{dup}</td></tr>"
        )

    body_in = "\n".join(row_html(r) for r in included)
    body_out = "\n".join(row_html(r) for r in excluded)
    now = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    title = cfg.get("output_name", "Family Filtered")

    doc = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>{html.escape(title)} — preview</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 1000px;
          margin: 2em auto; padding: 0 1em; color: #222; }}
  h1 {{ margin-bottom: .1em; }}
  .sub {{ color: #666; margin-top: 0; }}
  .link {{ background: #eef; padding: .8em 1em; border-radius: 6px;
           word-break: break-all; margin: 1em 0; }}
  .stats {{ background: #f4f6f8; padding: 1em 1.2em; border-radius: 8px;
            margin: 1em 0; }}
  h2 {{ border-bottom: 2px solid #ccc; padding-bottom: .3em; margin-top: 1.5em; }}
  .ok {{ color: #2a7a2a; }} .no {{ color: #a23030; }}
  table {{ border-collapse: collapse; width: 100%; font-size: 14px; }}
  th, td {{ padding: .4em .6em; border-bottom: 1px solid #eee;
            text-align: left; vertical-align: top; }}
  th {{ background: #f0f0f0; }}
  .feed {{ color: #555; font-size: 90%; }}
  .reason {{ color: #777; font-style: italic; font-size: 90%; }}
  code {{ background: #eee; padding: 1px 4px; border-radius: 3px; }}
</style></head>
<body>
<h1>{html.escape(title)}</h1>
<p class="sub">Last built: {now}</p>

<div class="link">
  <b>Subscription file:</b> <a href="./{html.escape(ics_filename)}">./{html.escape(ics_filename)}</a><br>
  <small>To subscribe in Google Calendar (on a computer):
  <b>Other calendars → + → From URL</b>, then paste the full URL of the
  <code>.ics</code> file (right-click the link above and copy address).</small>
</div>

<div class="stats">
  <b>Feeds fetched:</b> {stats['feeds']} ·
  <b>Events seen:</b> {stats['events']} ·
  <b>Included:</b> <span class="ok">{stats['included']}</span> ·
  <b>Excluded:</b> <span class="no">{stats['excluded']}</span> ·
  <b>Duplicates skipped:</b> {stats['duplicates']} ·
  <b>Errors:</b> {stats['errors']}
</div>

<h2 class="ok">✓ Included ({len(included)})</h2>
<table><thead><tr><th>Date</th><th>Event</th><th>Source feed</th><th>Reason</th></tr></thead>
<tbody>
{body_in}
</tbody></table>

<h2 class="no">✗ Excluded ({len(excluded)})</h2>
<p>If anything here SHOULD be on your calendar, add a distinctive word from
its title to <code>include_keywords</code> in <code>config.yaml</code>.</p>
<table><thead><tr><th>Date</th><th>Event</th><th>Source feed</th><th>Reason</th></tr></thead>
<tbody>
{body_out}
</tbody></table>
</body></html>"""

    with open(path, "w") as f:
        f.write(doc)


if __name__ == "__main__":
    main()
