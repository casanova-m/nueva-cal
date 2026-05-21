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
from datetime import date, datetime
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


# ---------- filter logic ----------




def classify(event, feed_name: str, cfg: dict):
    """Return (included, reason).

    Order of precedence (highest to lowest):
      1. Match in always_exclude_keywords -> exclude (highest priority,
         use this when a broad include rule is causing a specific false
         positive you want to remove).
      2. Feeds in always_include_feeds -> include (no filtering at all).
      3. Match in include_keywords -> include (this BEATS exclude_keywords).
      4. Match in exclude_keywords -> exclude.
      5. Match in feed_excludes[feed_name] -> exclude.
      6. Fall back to feed_defaults[feed_name], default 'exclude'.
    """
    text = get_searchable_text(event)

    if matches_any(text, cfg.get("always_exclude_keywords", [])):
        return False, "matched always-exclude keyword"

    if feed_name in cfg.get("always_include_feeds", []):
        return True, "always-include feed"

    if matches_any(text, cfg.get("include_keywords", [])):
        return True, "matched include keyword"

    if matches_any(text, cfg.get("exclude_keywords", [])):
        return False, "matched exclude keyword"

    feed_specific = cfg.get("feed_excludes", {}).get(feed_name, [])
    if matches_any(text, feed_specific):
        return False, f"matched {feed_name}-specific exclude"

    default = cfg.get("feed_defaults", {}).get(feed_name, "exclude")
    return (default == "include"), f"feed default: {default}"


# ---------- fetch + build ----------

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
