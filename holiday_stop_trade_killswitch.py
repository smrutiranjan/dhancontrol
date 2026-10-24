#!/usr/bin/env python3
"""
Indian equity trading-day check for trade_count_killswitch.py (and friends).

Used by TradeCountKillSwitch._exit_if_non_trading_day() to skip weekends and
NSE/BSE market holidays before any credentials/API calls are made.
"""

from __future__ import annotations

from datetime import date
from typing import Tuple

_CALENDAR_NAME = "XBOM"  # Bombay Stock Exchange calendar (exchange_calendars alias: "BSE")
_calendar = None


def _get_calendar():
    global _calendar
    if _calendar is None:
        import exchange_calendars as xcals
        _calendar = xcals.get_calendar(_CALENDAR_NAME)
    return _calendar


def is_indian_equity_trading_day(check_date: date, base_dir: str = "") -> Tuple[bool, str]:
    """
    Returns (True, reason) if ``check_date`` is an NSE/BSE trading session,
    (False, reason) if it's a weekend or a market holiday.

    Fails open (returns True) if the exchange_calendars lookup itself fails,
    so a broken/missing calendar dependency can never silently stop trading
    on what might be a real trading day.
    """
    if check_date.weekday() >= 5:
        return False, f"{check_date.isoformat()} is a weekend ({check_date.strftime('%A')})"

    try:
        calendar = _get_calendar()
        is_session = bool(calendar.is_session(check_date.isoformat()))
    except Exception as e:
        return True, f"exchange_calendars lookup failed ({e}); assuming trading day"

    if is_session:
        return True, f"{check_date.isoformat()} is an NSE/BSE trading session"
    return False, f"{check_date.isoformat()} is an NSE/BSE market holiday"
