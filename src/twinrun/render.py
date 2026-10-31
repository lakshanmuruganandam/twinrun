"""Turn a Report into the check-run output and the (single, updated-in-place) PR comment."""

from __future__ import annotations

from dataclasses import dataclass

from .engine import Report

MARKER = "<!-- twinrun:behavior-diff -->"
LOCK_LABELS = {"refactor", "no-behavior-change"}
CHECK_NAME = "Behavior Diff"


@dataclass
class Verdict:
    conclusion: str   # "success" | "neutral" | "failure"
    title: str
    locked: bool


def verdict(report: Report, labels: set[str] | list[str]) -> Verdict:
    locked = bool(LOCK_LABELS & {lbl.lower() for lbl in labels})
    n = len(report.divergent)
    groups = sum(len(f.groups) for f in report.functions)
    if n == 0:
        title = f"No behavior change in {len(report.compared)} compared function(s)"
        if not report.compared:
            title = "No changed functions could be compared"
        return Verdict("success", title, locked)
    title = f"{n} function(s) behave differently ({groups} kind(s) of divergence)"
    if locked:
        return Verdict("failure", "Behavior lock: " + title, locked)
    return Verdict("neutral", title, locked)


def _cell(s: str) -> str:
    s = s.replace("\r", " ").replace("\n", "⏎").replace("|", "\\|").replace("`", "ˋ")
    return f"`{s}`" if s else " "


def table(report: Report) -> str:
    rows = ["| function | divergence | smallest input | base output | head output |",
            "|---|---|---|---|---|"]
    for f in report.divergent:
        for g in f.groups:
            note = "" if g.shrunk else " (not shrunk)"
            rows.append(f"| `{f.qualname}` | {g.kind} ×{g.count} | {_cell(g.call)}{note} | "
                        f"{_cell(g.base)} | {_cell(g.head)} |")
    return "\n".join(rows)


def body(report: Report, v: Verdict, *, head_sha: str = "", base_sha: str = "") -> str:
    lines: list[str] = []
    icon = {"success": "✅", "neutral": "🟡", "failure": "❌"}[v.conclusion]
    lines.append(f"### {icon} TwinRun · {v.title}")
    if base_sha and head_sha:
        lines.append(f"Ran every changed function at merge-base `{base_sha[:7]}` and head `{head_sha[:7]}` "
                     "on the same generated inputs, in a sandbox.")
    if v.locked:
        lines.append("🔒 **Behavior lock is on** (label `refactor` / `no-behavior-change`): "
                     "any divergence fails this check.")
    lines.append("")
    if report.divergent:
        lines.append(table(report))
        lines.append("")
        lines.append("<sub>Smallest input was shrunk by Hypothesis unless marked (not shrunk). "
                     "×N = how many generated inputs showed that divergence.</sub>")
        lines.append("")
    stats = (f"**{len(report.compared)}** compared · **{len(report.divergent)}** diverged · "
             f"**{len(report.skipped)}** skipped · "
             f"{sum(f.inputs for f in report.compared)} inputs · {report.seconds}s · sandbox: {report.sandbox}")
    lines.append(stats)
    same = [f for f in report.compared if not f.diverged]
    if same:
        lines.append("")
        lines.append("<details><summary>Same behavior on every input "
                     f"({len(same)})</summary>\n")
        for f in same:
            lines.append(f"- `{f.file}::{f.qualname}` — {f.inputs} inputs ({f.reason})")
        lines.append("\n</details>")
    if report.skipped:
        lines.append("")
        lines.append(f"<details><summary>Skipped ({len(report.skipped)}) — not checked, so not covered "
                     "by this report</summary>\n")
        for f in report.skipped:
            lines.append(f"- `{f.file}::{f.qualname}` — {f.reason}")
        lines.append("\n</details>")
    notes = [(f, n) for f in report.functions for n in f.notes]
    if notes or report.parse_errors:
        lines.append("")
        lines.append("<details><summary>Notes</summary>\n")
        for f, n in notes:
            lines.append(f"- `{f.qualname}`: {n}")
        for e in report.parse_errors:
            lines.append(f"- syntax error, file not analyzed: {e}")
        lines.append("\n</details>")
    return "\n".join(lines)


def comment(report: Report, v: Verdict, **kw) -> str:
    return MARKER + "\n" + body(report, v, **kw)


def check_output(report: Report, v: Verdict, **kw) -> dict:
    text = body(report, v, **kw)
    if len(text) > 65000:  # GitHub's limit for check-run summary
        text = text[:64000] + "\n\n…truncated"
    return {"title": v.title[:250], "summary": text}
