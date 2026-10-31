"""Shift time arithmetic."""
from datetime import datetime, time, timedelta


def parse_hhmm(text):
    h, m = str(text).split(":")[:2]
    return time(int(h), int(m))


def interval(day_iso, start_hhmm, end_hhmm):
    """(start_dt, end_dt) of a shift that starts on ``day_iso``; an end at/before the start means 'next day'."""
    d = datetime.fromisoformat(day_iso)
    s = d.replace(hour=0, minute=0) + timedelta(hours=parse_hhmm(start_hhmm).hour, minutes=parse_hhmm(start_hhmm).minute)
    e = d.replace(hour=0, minute=0) + timedelta(hours=parse_hhmm(end_hhmm).hour, minutes=parse_hhmm(end_hhmm).minute)
    if e <= s:
        e += timedelta(days=1)
    return s, e


def scheduled_hours(start_hhmm, end_hhmm):
    s, e = interval("2000-01-01", start_hhmm, end_hhmm)
    return (e - s).total_seconds() / 3600
