"""Calendar arithmetic on plain ints."""


def is_leap(year: int) -> bool:
    if year % 400 == 0:
        return True
    if year % 100 == 0:
        return False
    return year % 4 == 0


def days_in_month(year: int, month: int) -> int:
    if not 1 <= month <= 12:
        raise ValueError(f"bad month {month}")
    if month == 2:
        return 29 if is_leap(year) else 28
    return 30 if month in (4, 6, 9, 11) else 31


def quarter(month: int) -> int:
    if not 1 <= month <= 12:
        raise ValueError("bad month")
    return (month - 1) // 3 + 1
