"""Токены страниц Facebook.

Один раз: короткий токен пользователя (из официального окна входа Facebook в Graph API Explorer)
→ долгосрочный токен → токены 34 страниц (у таких токенов страниц нет срока действия).
Токены страниц хранятся ЗАШИФРОВАННЫМИ в state/page_tokens.enc; ключ шифрования выводится
из секрета FB_APP_SECRET, который есть только в GitHub Secrets.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from cryptography.fernet import Fernet, InvalidToken

NEEDED_TASKS = {"CREATE_CONTENT", "MODERATE"}
TOKENS_FILE = "state/page_tokens.enc"


class TokenError(Exception):
    pass


def _fernet(app_secret: str) -> Fernet:
    if not app_secret:
        raise TokenError("не задан секрет FB_APP_SECRET")
    key = hashlib.sha256(("interes-world-autopost/page-tokens/v1:" + app_secret).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def save_tokens(cfg, app_secret: str, tokens: dict, meta: dict) -> None:
    blob = json.dumps({"tokens": tokens, "meta": meta}).encode()
    p = cfg.path(TOKENS_FILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_fernet(app_secret).encrypt(blob))


def load_tokens(cfg, app_secret: str | None = None) -> dict:
    app_secret = app_secret if app_secret is not None else os.environ.get("FB_APP_SECRET", "")
    p = cfg.path(TOKENS_FILE)
    if not p.exists():
        raise TokenError("токены страниц ещё не получены — запустите workflow «2 · Подключить Facebook»")
    try:
        data = json.loads(_fernet(app_secret).decrypt(p.read_bytes()))
    except InvalidToken:
        raise TokenError("не удаётся расшифровать токены: FB_APP_SECRET изменился — повторите «2 · Подключить Facebook»")
    return data["tokens"]


def setup_facebook(cfg, graph, app_id: str, app_secret: str, user_token: str) -> dict:
    """Получает и сохраняет токены страниц. Возвращает отчёт для пользователя."""
    if not (app_id and app_secret and user_token):
        raise TokenError("нужны секреты FB_APP_ID, FB_APP_SECRET и FB_USER_TOKEN")
    long_user = graph.exchange_long_lived(app_id, app_secret, user_token)
    pages = graph.my_pages(long_user)
    by_id = {p["id"]: p for p in pages}
    tokens, missing, no_rights, ok = {}, [], [], []
    for page in cfg.pages:
        got = by_id.get(page.page_id)
        if not got:
            missing.append(page)
            continue
        tasks = set(got.get("tasks") or [])
        if tasks and not NEEDED_TASKS.issubset(tasks):
            no_rights.append((page, sorted(NEEDED_TASKS - tasks)))
        tokens[page.page_id] = got["access_token"]
        ok.append(page)
    expires = None
    if tokens:
        any_tok = next(iter(tokens.values()))
        try:
            info = graph.debug_token(any_tok, app_id, app_secret)
            expires = info.get("expires_at")
            scopes = info.get("scopes") or []
        except Exception:  # noqa: BLE001 — отчёт не должен падать из-за диагностики
            scopes = []
    else:
        scopes = []
    meta = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pages": len(tokens), "expires_at": expires}
    if tokens:
        save_tokens(cfg, app_secret, tokens, meta)
    extra = sorted(set(by_id) - {p.page_id for p in cfg.pages})
    return {"ok": ok, "missing": missing, "no_rights": no_rights, "expires_at": expires,
            "scopes": scopes, "extra_pages": [(i, by_id[i].get("name")) for i in extra]}
