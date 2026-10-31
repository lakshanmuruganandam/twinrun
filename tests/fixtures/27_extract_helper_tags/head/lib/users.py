"""User-facing helpers."""
from typing import Optional


def display_name(first: str, last: Optional[str] = None) -> str:
    if last:
        return f"{first} {last}"
    return first


def age_group(age: int) -> str:
    if age < 13:
        return "child"
    if age < 18:
        return "teen"
    if age < 65:
        return "adult"
    return "senior"


def _unique(seq):
    seen = []
    for t in seq:
        if t not in seen:
            seen.append(t)
    return seen


def merge_tags(a: list[str], b: list[str]) -> list[str]:
    return _unique(a + b)


def password_ok(pw: str, min_len: int = 8) -> bool:
    return len(pw) >= min_len and any(c.isdigit() for c in pw)
