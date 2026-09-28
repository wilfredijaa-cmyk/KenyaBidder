"""Kenya is on East Africa Time (UTC+3) all year — no DST. Servers usually run UTC; users must never see that."""
from datetime import datetime, timedelta, timezone

EAT = timezone(timedelta(hours=3), "EAT")


def eat(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, EAT)
