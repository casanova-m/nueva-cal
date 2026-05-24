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
    """Stable s
