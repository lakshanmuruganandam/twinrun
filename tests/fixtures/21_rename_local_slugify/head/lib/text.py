"""String helpers."""


def slugify(title: str) -> str:
    chars = []
    for ch in title.lower():
        if ch.isalnum():
            chars.append(ch)
        elif chars and chars[-1] != "-":
            chars.append("-")
    return "".join(chars).strip("-")


def truncate(s: str, width: int = 10) -> str:
    if len(s) <= width:
        return s
    return s[: width - 1] + "…"


def word_count(text):
    return len([w for w in text.split() if w])


def initials(name: str) -> str:
    parts = name.split()
    return "".join(p[0].upper() for p in parts)
