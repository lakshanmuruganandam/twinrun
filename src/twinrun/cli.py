"""twinrun command line: serve | create-app | analyze."""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

ROOT_MANIFEST = Path(__file__).resolve().parents[2] / "app-manifest.json"


def cmd_analyze(ns) -> int:
    from .engine import Options, analyze
    from .render import body, verdict

    report = analyze(ns.base, ns.head, Options(max_examples=ns.examples, jobs=ns.jobs),
                     log=(lambda m: print(m, file=sys.stderr)) if ns.verbose else None)
    v = verdict(report, ns.label or [])
    print(body(report, v))
    return 1 if v.conclusion == "failure" else 0


def cmd_serve(ns) -> int:
    import uvicorn

    from .server import create_app, load_config

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app(load_config(ns.env))
    uvicorn.run(app, host=ns.host, port=ns.port, log_level="info")
    return 0


def new_smee_channel() -> str:
    r = httpx.get("https://smee.io/new", follow_redirects=False, timeout=20)
    url = r.headers.get("location", "")
    if not url.startswith("https://smee.io/"):
        raise SystemExit(f"could not create a smee.io channel (status {r.status_code})")
    return url


def build_manifest(template: dict, *, name: str, webhook_url: str, port: int) -> dict:
    m = json.loads(json.dumps(template))
    m["name"] = name
    m["hook_attributes"]["url"] = webhook_url
    m["redirect_url"] = f"http://localhost:{port}/callback"
    return m


def form_page(manifest: dict, state: str, org: str | None) -> str:
    target = (f"https://github.com/organizations/{org}/settings/apps/new" if org
              else "https://github.com/settings/apps/new")
    return f"""<!doctype html><meta charset="utf-8"><title>Create TwinRun</title>
<body style="font:16px system-ui;padding:3em;background:#0d1117;color:#e6edf3">
<h2>Creating the TwinRun GitHub App…</h2><p>You'll be asked to confirm on GitHub.</p>
<form id="f" action="{target}?state={state}" method="post">
<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">
<button type="submit">Continue to GitHub</button></form>
<script>document.getElementById('f').submit()</script>"""


def convert_code(code: str) -> dict:
    r = httpx.post(f"https://api.github.com/app-manifests/{code}/conversions",
                   headers={"Accept": "application/vnd.github+json"}, timeout=30)
    r.raise_for_status()
    return r.json()


def save_credentials(data: dict, webhook_url: str, out_dir: Path) -> Path:
    pem = out_dir / f"{data['slug']}.private-key.pem"
    pem.write_text(data["pem"])
    os.chmod(pem, 0o600)
    env = out_dir / ".env"
    env.write_text(
        f"APP_ID={data['id']}\nAPP_SLUG={data['slug']}\nPRIVATE_KEY_PATH={pem}\n"
        f"WEBHOOK_SECRET={data['webhook_secret']}\nCLIENT_ID={data.get('client_id', '')}\n"
        f"SMEE_URL={webhook_url}\n")
    os.chmod(env, 0o600)
    return env


def cmd_create_app(ns) -> int:
    template = json.loads(Path(ns.manifest).read_text())
    webhook_url = ns.webhook_url or new_smee_channel()
    name = ns.name or f"TwinRun-{secrets.token_hex(2)}"
    manifest = build_manifest(template, name=name, webhook_url=webhook_url, port=ns.port)
    state = secrets.token_urlsafe(16)
    result: dict = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code: int, text: str):
            data = text.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                return self._send(200, form_page(manifest, state, ns.org))
            if u.path == "/callback":
                q = parse_qs(u.query)
                if q.get("state", [""])[0] != state or "code" not in q:
                    return self._send(400, "state mismatch, start again with `twinrun create-app`")
                try:
                    data = convert_code(q["code"][0])
                    env = save_credentials(data, webhook_url, Path(ns.out))
                    result.update(data=data, env=str(env))
                    install = f"{data['html_url']}/installations/new"
                    self._send(200, f"<body style='font:16px system-ui;padding:3em'><h2>✅ {html.escape(data['name'])} "
                                    f"created</h2><p>Credentials saved to {html.escape(str(env))}.</p>"
                                    f"<p><a href='{install}'>Install it on a repository →</a></p>")
                except Exception as e:  # noqa: BLE001
                    result["error"] = str(e)
                    self._send(500, f"conversion failed: {html.escape(str(e))}")
                done.set()
                return None
            return self._send(404, "not found")

    srv = HTTPServer(("127.0.0.1", ns.port), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start = f"http://localhost:{ns.port}/"
    print(f"Webhook URL (smee): {webhook_url}")
    print(f"Open {start} to create the app (opening your browser)…")
    if not ns.no_browser:
        webbrowser.open(start)
    done.wait()
    srv.shutdown()
    if "error" in result:
        print("failed:", result["error"], file=sys.stderr)
        return 1
    d = result["data"]
    print(f"Created app '{d['name']}' (id {d['id']}). Credentials in {result['env']}")
    print(f"Install it: {d['html_url']}/installations/new")
    print(f"Then run:   npx smee-client --url {webhook_url} --target http://localhost:8000/webhook")
    print("and:        twinrun serve")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="twinrun", description="Behavior Diff for pull requests")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="compare two local checkouts and print the report")
    a.add_argument("base")
    a.add_argument("head")
    a.add_argument("--label", action="append", help="PR label, e.g. --label refactor")
    a.add_argument("--examples", type=int, default=150)
    a.add_argument("--jobs", type=int, default=8)
    a.add_argument("-v", "--verbose", action="store_true")
    a.set_defaults(fn=cmd_analyze)

    s = sub.add_parser("serve", help="run the webhook server")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--env", default=".env")
    s.set_defaults(fn=cmd_serve)

    c = sub.add_parser("create-app", help="create the GitHub App with the manifest flow (one click)")
    c.add_argument("--name", help="app name (must be unique on GitHub); default TwinRun-xxxx")
    c.add_argument("--org", help="create under an organization instead of your account")
    c.add_argument("--webhook-url", help="webhook URL; default: a new smee.io channel")
    c.add_argument("--port", type=int, default=3456)
    c.add_argument("--manifest", default=str(ROOT_MANIFEST))
    c.add_argument("--out", default=".")
    c.add_argument("--no-browser", action="store_true")
    c.set_defaults(fn=cmd_create_app)

    ns = ap.parse_args(argv)
    return ns.fn(ns)


if __name__ == "__main__":
    raise SystemExit(main())
