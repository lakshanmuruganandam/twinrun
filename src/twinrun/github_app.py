"""GitHub side: turn a pull_request webhook into a Behavior Diff check run + one PR comment."""

from __future__ import annotations

import hashlib
import hmac
import io
import logging
import shutil
import tarfile
import tempfile
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path

import httpx
from githubkit import AppAuthStrategy, AppInstallationAuthStrategy, GitHub

from . import render
from .engine import Options, analyze

log = logging.getLogger("twinrun")

HANDLED_ACTIONS = {"opened", "synchronize", "reopened", "labeled", "unlabeled"}


def verify_signature(secret: str, body: bytes, header: str | None) -> bool:
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header)


@dataclass
class AppConfig:
    app_id: str
    private_key: str
    webhook_secret: str


class RepoClient:
    """The handful of GitHub calls TwinRun needs, behind one small interface (easy to fake in tests)."""

    def __init__(self, cfg: AppConfig, installation_id: int, owner: str, repo: str):
        self.owner, self.repo = owner, repo
        self.gh = GitHub(AppInstallationAuthStrategy(cfg.app_id, cfg.private_key, installation_id))
        self._app = GitHub(AppAuthStrategy(cfg.app_id, cfg.private_key))
        self._installation_id = installation_id

    def create_check(self, head_sha: str) -> int:
        r = self.gh.rest.checks.create(self.owner, self.repo, data={
            "name": render.CHECK_NAME, "head_sha": head_sha, "status": "in_progress",
            "output": {"title": "Running both versions…", "summary": "TwinRun is comparing base and head."},
        })
        return r.json()["id"]

    def complete_check(self, check_id: int, conclusion: str, output: dict) -> None:
        self.gh.rest.checks.update(self.owner, self.repo, check_id, data={
            "status": "completed", "conclusion": conclusion, "output": output,
        })

    def merge_base(self, base_sha: str, head_sha: str) -> str:
        r = self.gh.rest.repos.compare_commits(self.owner, self.repo, f"{base_sha}...{head_sha}")
        return r.json()["merge_base_commit"]["sha"]

    def download(self, sha: str, dest: Path) -> Path:
        token = self._app.rest.apps.create_installation_access_token(self._installation_id).json()["token"]
        url = f"https://api.github.com/repos/{self.owner}/{self.repo}/tarball/{sha}"
        with httpx.Client(follow_redirects=True, timeout=120) as c:
            resp = c.get(url, headers={"Authorization": f"Bearer {token}",
                                       "Accept": "application/vnd.github+json"})
            resp.raise_for_status()
        return extract_tarball(resp.content, dest)

    def upsert_comment(self, pr_number: int, body: str) -> tuple[str, int]:
        page = 1
        while True:
            r = self.gh.rest.issues.list_comments(self.owner, self.repo, pr_number, per_page=100, page=page)
            items = r.json()
            for c in items:
                if render.MARKER in (c.get("body") or "") and (c.get("user") or {}).get("type") == "Bot":
                    self.gh.rest.issues.update_comment(self.owner, self.repo, c["id"], data={"body": body})
                    return "updated", c["id"]
            if len(items) < 100:
                break
            page += 1
        r = self.gh.rest.issues.create_comment(self.owner, self.repo, pr_number, data={"body": body})
        return "created", r.json()["id"]


def extract_tarball(content: bytes, dest: Path) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as tf:
        tf.extractall(dest, filter="data")  # rejects absolute paths, .., device files, unsafe links
    subdirs = [p for p in dest.iterdir() if p.is_dir()]
    return subdirs[0] if len(subdirs) == 1 else dest


_pr_locks: dict[tuple, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(key: tuple) -> threading.Lock:
    with _locks_guard:
        return _pr_locks.setdefault(key, threading.Lock())


def handle_pull_request(payload: dict, client_factory, opts: Options | None = None) -> dict:
    """Process one pull_request event. ``client_factory(installation_id, owner, repo)`` -> RepoClient."""
    action = payload.get("action")
    if action not in HANDLED_ACTIONS:
        return {"skipped": f"action {action}"}
    pr = payload["pull_request"]
    if action in ("labeled", "unlabeled"):
        name = (payload.get("label") or {}).get("name", "").lower()
        if name not in render.LOCK_LABELS:
            return {"skipped": f"label {name} does not affect the check"}
    owner = payload["repository"]["owner"]["login"]
    repo = payload["repository"]["name"]
    number = pr["number"]
    head_sha = pr["head"]["sha"]
    base_sha = pr["base"]["sha"]
    labels = [lbl["name"] for lbl in pr.get("labels", [])]
    client = client_factory(payload["installation"]["id"], owner, repo)

    with _lock_for((owner, repo, number)):
        check_id = client.create_check(head_sha)
        work = Path(tempfile.mkdtemp(prefix="twinrun-pr-"))
        try:
            mb = client.merge_base(base_sha, head_sha)
            base_dir = client.download(mb, work / "base")
            head_dir = client.download(head_sha, work / "head")
            report = analyze(base_dir, head_dir, opts, log=log.info)
            v = render.verdict(report, labels)
            client.complete_check(check_id, v.conclusion,
                                  render.check_output(report, v, head_sha=head_sha, base_sha=mb))
            how, cid = client.upsert_comment(number, render.comment(report, v, head_sha=head_sha, base_sha=mb))
            log.info("PR #%s %s: %s; comment %s (%s)", number, head_sha[:7], v.conclusion, cid, how)
            return {"conclusion": v.conclusion, "comment": how, "comment_id": cid,
                    "divergent": len(report.divergent), "seconds": report.seconds}
        except Exception as e:  # report the failure on the PR instead of dying silently
            log.error("TwinRun failed on PR #%s: %s", number, traceback.format_exc())
            client.complete_check(check_id, "neutral", {
                "title": "TwinRun could not finish", "summary": f"```\n{type(e).__name__}: {e}\n```"[:6000]})
            return {"error": str(e)}
        finally:
            shutil.rmtree(work, ignore_errors=True)
