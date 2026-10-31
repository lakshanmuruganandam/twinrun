import hashlib
import hmac
import json
from concurrent.futures import Future
from pathlib import Path

from fastapi.testclient import TestClient

from twinrun import render
from twinrun.engine import FunctionResult, Group, Report
from twinrun.github_app import AppConfig, handle_pull_request, verify_signature
from twinrun.server import create_app


def report(divergent=True):
    fr = FunctionResult("m.py", "f", "compared", "body changed", inputs=100)
    if divergent:
        fr.groups = [Group("value change", 7, "f(18)", "'adult'", "'te|en'", True)]
    skipped = FunctionResult("m.py", "K.m", "skipped", "instance method: needs an object to call it on")
    return Report([fr, skipped], ["m.py"], [], [], [], 1.5, "sandbox-exec")


def test_verdicts():
    assert render.verdict(report(True), []).conclusion == "neutral"
    assert render.verdict(report(True), ["refactor"]).conclusion == "failure"
    assert render.verdict(report(True), ["No-Behavior-Change"]).conclusion == "failure"
    assert render.verdict(report(False), ["refactor"]).conclusion == "success"
    assert render.verdict(report(False), []).conclusion == "success"


def test_comment_rendering():
    v = render.verdict(report(True), ["refactor"])
    body = render.comment(report(True), v, head_sha="a" * 40, base_sha="b" * 40)
    assert body.startswith(render.MARKER)
    assert "| function | divergence | smallest input | base output | head output |" in body
    assert "| `f` | value change ×7 | `f(18)` | `'adult'` | `'te\\|en'` |" in body  # pipe escaped
    assert "Behavior lock is on" in body
    assert "instance method" in body and "Skipped (1)" in body
    assert "`bbbbbbb`" in body and "`aaaaaaa`" in body


def test_check_output_respects_limits():
    out = render.check_output(report(True), render.verdict(report(True), []))
    assert out["title"] and len(out["summary"]) < 65536


def sign(secret, body):
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_signature_verification():
    assert verify_signature("s", b"{}", sign("s", b"{}"))
    assert not verify_signature("s", b"{}", sign("t", b"{}"))
    assert not verify_signature("s", b"{}", None)
    assert not verify_signature("", b"{}", sign("", b"{}"))


class FakeClient:
    def __init__(self, base_dir: Path, head_dir: Path):
        self.dirs = {"mb": base_dir, "headsha": head_dir}
        self.comments: dict[int, str] = {}
        self.checks: list[dict] = []

    def create_check(self, head_sha):
        self.checks.append({"head_sha": head_sha, "status": "in_progress"})
        return len(self.checks)

    def complete_check(self, check_id, conclusion, output):
        self.checks[check_id - 1].update(status="completed", conclusion=conclusion, output=output)

    def merge_base(self, base_sha, head_sha):
        return "mb"

    def download(self, sha, dest):
        return self.dirs[sha]

    def upsert_comment(self, pr, body):
        for cid, b in self.comments.items():
            if render.MARKER in b:
                self.comments[cid] = body
                return "updated", cid
        cid = 100 + len(self.comments)
        self.comments[cid] = body
        return "created", cid


def payload(action="opened", labels=()):
    return {
        "action": action, "number": 1, "installation": {"id": 9},
        "repository": {"name": "r", "owner": {"login": "o"}},
        "pull_request": {"number": 1, "head": {"sha": "headsha"}, "base": {"sha": "basesha"},
                         "labels": [{"name": n} for n in labels]},
    }


def test_handle_pull_request_end_to_end_with_fake_github(make_pr):
    b, h = make_pr({"m.py": "def f(x: int) -> int:\n    return x if x < 10 else 10\n"},
                   {"m.py": "def f(x: int) -> int:\n    return x if x <= 10 else 10\n"})
    # x == 10 returns 10 on both sides: a pure refactor -> passes under behavior lock
    fake = FakeClient(b, h)
    r1 = handle_pull_request(payload(labels=["refactor"]), lambda *a: fake)
    assert r1["conclusion"] == "success" and r1["comment"] == "created"
    r2 = handle_pull_request(payload("synchronize", labels=["refactor"]), lambda *a: fake)
    assert r2["comment"] == "updated" and len(fake.comments) == 1
    assert [c["status"] for c in fake.checks] == ["completed", "completed"]


def test_behavior_lock_fails_on_divergence(make_pr):
    b, h = make_pr({"m.py": "def f(x: int) -> int:\n    return x if x < 10 else 10\n"},
                   {"m.py": "def f(x: int) -> int:\n    return x if x < 10 else 11\n"})
    fake = FakeClient(b, h)
    r = handle_pull_request(payload(labels=["refactor"]), lambda *a: fake)
    assert r["conclusion"] == "failure"
    assert "f(10)" in fake.checks[0]["output"]["summary"]


def test_errors_become_a_neutral_check(make_pr):
    b, h = make_pr({"m.py": "x = 1\n"}, {"m.py": "x = 2\n"})
    fake = FakeClient(b, h)
    fake.merge_base = lambda *a: (_ for _ in ()).throw(RuntimeError("compare API down"))
    r = handle_pull_request(payload(), lambda *a: fake)
    assert "compare API down" in r["error"]
    assert fake.checks[0]["conclusion"] == "neutral"


def test_irrelevant_label_events_are_ignored():
    p = payload("labeled")
    p["label"] = {"name": "docs"}
    assert "skipped" in handle_pull_request(p, lambda *a: None)


class InlineExecutor:
    def submit(self, fn, *a):
        f = Future()
        f.set_result(fn(*a))
        return f


def test_webhook_server_signature_and_dispatch(make_pr):
    b, h = make_pr({"m.py": "def f(x: int) -> int:\n    return x\n"},
                   {"m.py": "def f(x: int) -> int:\n    return -x\n"})
    fake = FakeClient(b, h)
    cfg = AppConfig("1", "key", "sekret")
    app = create_app(cfg, client_factory=lambda *a: fake, executor=InlineExecutor())
    c = TestClient(app)
    body = json.dumps(payload()).encode()
    assert c.post("/webhook", content=body, headers={"X-GitHub-Event": "pull_request",
                                                     "X-Hub-Signature-256": sign("wrong", body)}).status_code == 401
    r = c.post("/webhook", content=body, headers={"X-GitHub-Event": "pull_request",
                                                  "X-Hub-Signature-256": sign("sekret", body)})
    assert r.status_code == 202
    assert app.state.jobs[0].result()["conclusion"] == "neutral"
    ping = json.dumps({"zen": "x"}).encode()
    assert c.post("/webhook", content=ping, headers={"X-GitHub-Event": "ping",
                                                     "X-Hub-Signature-256": sign("sekret", ping)}).json() == {"pong": True}


def test_manifest_builder():
    from twinrun.cli import ROOT_MANIFEST, build_manifest, form_page

    m = build_manifest(json.loads(ROOT_MANIFEST.read_text()), name="TwinRun-test",
                       webhook_url="https://smee.io/abc", port=4000)
    assert m["default_permissions"] == {"checks": "write", "pull_requests": "write",
                                        "contents": "read", "metadata": "read"}
    assert m["default_events"] == ["pull_request"] and m["public"] is False
    assert m["hook_attributes"]["url"] == "https://smee.io/abc"
    assert m["redirect_url"] == "http://localhost:4000/callback"
    page = form_page(m, "st8", None)
    assert "https://github.com/settings/apps/new?state=st8" in page and "&quot;TwinRun-test&quot;" in page
