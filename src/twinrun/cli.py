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


PAGE_CSS = """
:root{color-scheme:light dark;--bg:#ffffff;--fg:#1f2328;--muted:#59636e;--accent:#0969da;--ok:#1a7f37;--bad:#cf222e;--card:#f6f8fa;--line:#d1d9e0}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#f0f6fc;--muted:#9198a1;--accent:#4493f8;--ok:#3fb950;--bad:#f85149;--card:#151b23;--line:#3d444d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:40rem;margin:0 auto;padding:3rem 1rem}h1{font-size:1.6rem;line-height:1.3;margin:0 0 .75rem}
p{margin:.5rem 0}.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.25rem 1.5rem}
.ok{color:var(--ok)}.bad{color:var(--bad)}code{font:0.9em ui-monospace,SFMono-Regular,Menlo,monospace}
.btn{display:inline-block;margin-top:1rem;padding:.65rem 1.1rem;border-radius:8px;border:0;background:var(--accent);color:#fff;font:inherit;font-weight:600;text-decoration:none;cursor:pointer}
a{color:var(--accent)}:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""


def page(title: str, body: str, *, head_extra: str = "") -> str:
    """Small accessible HTML shell: language, viewport, real title, light/dark contrast, visible focus."""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{html.escape(title)}</title><style>{PAGE_CSS}</style>{head_extra}</head>"
            f"<body><main>{body}</main></body></html>")


def form_page(manifest: dict, state: str, org: str | None) -> str:
    target = (f"https://github.com/organizations/{org}/settings/apps/new" if org
              else "https://github.com/settings/apps/new")
    body = f"""<h1>Create your TwinRun GitHub App</h1>
<div class="card" role="status" aria-live="polite">
<p>Sending you to GitHub to create <strong>{html.escape(manifest["name"])}</strong>. GitHub will ask you to confirm.</p>
<p class="muted">Permissions: checks (write), pull requests (write), contents (read), metadata (read).
Event: pull_request. The app is private to your account.</p>
<form id="f" action="{target}?state={state}" method="post">
<input type="hidden" name="manifest" value="{html.escape(json.dumps(manifest))}">
<button class="btn" type="submit">Continue to GitHub</button>
</form>
<noscript><p>JavaScript is off, so press <strong>Continue to GitHub</strong> above.</p></noscript>
</div>
<script>document.getElementById('f').submit()</script>"""
    return page("Create TwinRun – GitHub App setup", body)


def done_page(data: dict, env: str) -> str:
    install = f"{data['html_url']}/installations/new"
    body = f"""<h1><span class="ok" aria-hidden="true">✓</span> {html.escape(data['name'])} is ready</h1>
<div class="card" role="status">
<p>The app was created and its credentials were saved to <code>{html.escape(env)}</code> (keep this file private).</p>
<p><strong>Next:</strong> install it on the repositories you want checked.</p>
<a class="btn" href="{html.escape(install)}">Install {html.escape(data['name'])} on a repository</a>
<p class="muted">Then run <code>twinrun serve</code> and the smee relay printed in your terminal.</p>
</div>"""
    return page(f"{data['name']} created – TwinRun", body)


def error_page(message: str) -> str:
    body = f"""<h1><span class="bad" aria-hidden="true">✕</span> Setup did not finish</h1>
<div class="card" role="alert"><p>{html.escape(message)}</p>
<p>Run <code>twinrun create-app</code> again to start over.</p></div>"""
    return page("Setup failed – TwinRun", body)


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
                    return self._send(400, error_page("The request did not come from this setup session "
                                                      "(state mismatch)."))
                try:
                    data = convert_code(q["code"][0])
                    env = save_credentials(data, webhook_url, Path(ns.out))
                    result.update(data=data, env=str(env))
                    self._send(200, done_page(data, str(env)))
                except Exception as e:  # noqa: BLE001
                    result["error"] = str(e)
                    self._send(500, error_page(f"GitHub did not accept the app code: {e}"))
                done.set()
                return None
            return self._send(404, error_page("Page not found."))

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
