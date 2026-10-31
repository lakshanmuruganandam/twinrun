"""FastAPI webhook server."""

from __future__ import annotations

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request

from .engine import Options
from .github_app import AppConfig, RepoClient, handle_pull_request, verify_signature

log = logging.getLogger("twinrun")


def load_config(env_file: str | None = ".env") -> AppConfig:
    if env_file and Path(env_file).exists():
        load_dotenv(env_file, override=False)
    missing = [k for k in ("APP_ID", "PRIVATE_KEY_PATH", "WEBHOOK_SECRET") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"missing in .env / environment: {', '.join(missing)} (run `twinrun create-app`)")
    key = Path(os.environ["PRIVATE_KEY_PATH"]).expanduser().read_text()
    return AppConfig(os.environ["APP_ID"], key, os.environ["WEBHOOK_SECRET"])


def create_app(cfg: AppConfig, client_factory=None, opts: Options | None = None, executor=None) -> FastAPI:
    app = FastAPI(title="TwinRun")
    pool = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="twinrun-pr")
    factory = client_factory or (lambda inst, owner, repo: RepoClient(cfg, inst, owner, repo))
    app.state.jobs = []

    @app.get("/")
    def health():
        return {"ok": True, "app": "twinrun"}

    @app.post("/webhook", status_code=202)
    async def webhook(request: Request,
                      x_hub_signature_256: str | None = Header(default=None),
                      x_github_event: str | None = Header(default=None)):
        body = await request.body()
        if not verify_signature(cfg.webhook_secret, body, x_hub_signature_256):
            raise HTTPException(status_code=401, detail="bad signature")
        payload = json.loads(body)
        if x_github_event == "ping":
            return {"pong": True}
        if x_github_event != "pull_request":
            return {"ignored": x_github_event}
        fut = pool.submit(handle_pull_request, payload, factory, opts)
        app.state.jobs.append(fut)
        return {"queued": payload.get("action"), "pr": payload.get("number")}

    return app
