"""Команды: check · setup-facebook · private-test · public-test · tick · status · simulate · render-workflow."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as config_mod
from .config import LANG_ORDER
from .fb import FacebookError, Graph, redact
from .generate import GenerationError, Generator
from .pipeline import AuthProblem, Pipeline, Report
from .sources import SourceError, fetch_post, parse_link, pick_source
from .state import State
from .tokens import TokenError, load_tokens, setup_facebook
from .workflow import render_cron_block, write_workflow
from .wp import WordPress, WordPressError


def summary(md: str, file: str | None = None, cfg=None):
    """Пишет отчёт в лог, в сводку GitHub Actions и (по желанию) в файл previews/…"""
    print(md)
    gs = os.environ.get("GITHUB_STEP_SUMMARY")
    if gs:
        with open(gs, "a", encoding="utf-8") as f:
            f.write(md + "\n")
    if file and cfg:
        p = cfg.path(file)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(md, encoding="utf-8")


def real_services(cfg):
    import requests
    graph = Graph(cfg.graph_version)
    wp = WordPress(cfg.site["url"], os.environ.get("WP_USER", ""), os.environ.get("WP_APP_PASSWORD", ""),
                   api_key=os.environ.get("WP_API_KEY"))
    return graph, wp, requests.Session()


def download(http, url) -> tuple:
    r = http.get(url, timeout=60)
    if r.status_code != 200 or not r.content:
        raise SourceError(f"не удалось скачать изображение (HTTP {r.status_code})")
    return r.content, (r.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()


def state_of(cfg) -> State:
    return State(cfg.path("state/state.json"))


# ---------------------------------------------------------------- check
def cmd_check(cfg, args) -> int:
    ok, bad = [], []

    def res(good, text):
        (ok if good else bad).append(text)

    probs = config_mod.check_expected_counts(cfg)
    res(not probs, "Конфигурация: 34 страницы (FR 7 · IT 9 · EN 16 · NL 2), как в Make" if not probs
        else "Конфигурация: " + "; ".join(probs))
    res(True, f"Часы публикаций ({cfg.tz}): {', '.join(cfg.slots)} · расписание "
              f"{'ВКЛЮЧЕНО' if cfg.settings.get('schedule_enabled') else 'выключено'}")
    for name in ("FB_APP_ID", "FB_APP_SECRET"):
        res(bool(os.environ.get(name)), f"Секрет {name} {'задан' if os.environ.get(name) else 'НЕ задан'}")
    wp_ok = os.environ.get("WP_API_KEY") or (os.environ.get("WP_USER") and os.environ.get("WP_APP_PASSWORD"))
    res(bool(wp_ok), "WordPress: " + ("ключ Make Connector (WP_API_KEY)" if os.environ.get("WP_API_KEY")
                                      else "пароль приложения (WP_USER + WP_APP_PASSWORD)") + (" задан" if wp_ok else " НЕ задан"))
    has_claude = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
    res(bool(has_claude), "Секрет Claude (CLAUDE_CODE_OAUTH_TOKEN или ANTHROPIC_API_KEY) "
        + ("задан" if has_claude else "НЕ задан"))
    graph, wp, _http = real_services(cfg)
    tokens = {}
    try:
        tokens = load_tokens(cfg)
        res(True, f"Токены Facebook: расшифрованы, страниц: {len(tokens)}")
        renamed, failed = [], []
        for p in cfg.pages:
            tok = tokens.get(p.page_id)
            if not tok:
                failed.append(f"{p.name} — нет токена")
                continue
            try:
                info = graph.get(p.page_id, tok, fields="id,name")
                if info.get("name") != p.name:
                    renamed.append(f"{p.name} → сейчас «{info.get('name')}»")
            except FacebookError as e:
                failed.append(f"{p.name} — {e.short()}")
        res(not failed, "Facebook: все 34 страницы доступны" if not failed
            else "Facebook: проблемы со страницами: " + "; ".join(failed))
        if renamed:
            ok.append("Facebook: названия отличаются от Make (ID совпадают, это не ошибка): " + "; ".join(renamed))
    except TokenError as e:
        res(False, f"Facebook: {e}")
    try:
        me = wp.me()
        caps = me.get("capabilities") or {}
        good = caps.get("publish_posts") or caps.get("administrator") or "administrator" in (me.get("roles") or [])
        res(bool(good), f"WordPress: вход выполнен как «{me.get('name')}»"
            + ("" if good else " — но у пользователя нет права публиковать записи"))
    except WordPressError as e:
        hint = ""
        if "not currently logged in" in str(e) or "не авторизован" in str(e).lower():
            hint = (" — хостинг, похоже, вырезает заголовок авторизации. Решение: секрет WP_API_KEY "
                    "с ключом плагина Make Connector (WordPress → Make → API Key).")
        res(False, f"WordPress: {e}{hint}")
    try:
        used = Generator(cfg).ping()
        res(True, f"Claude: отвечает (способ: {'подписка Max' if used == 'subscription' else used})")
    except GenerationError as e:
        res(False, f"Claude: {e}")
    if tokens:
        st = copy.deepcopy(state_of(cfg).data)
        tmp = State(Path(tempfile.mkdtemp()) / "s.json")
        tmp.data = st
        try:
            src = pick_source(cfg, graph, tokens, tmp, datetime.now(timezone.utc), lambda m: None)
            res(True, "Источник историй: найдена следующая история — " + (src["link"][:100] if src else
                "пока новых нет (опубликуйте историю с фото на любой из страниц или добавьте ссылку в очередь)"))
        except (FacebookError, SourceError) as e:
            res(False, f"Источник историй: {e}")
    md = "## Проверка подключений\n\n" + "\n".join([f"- ✅ {x}" for x in ok] + [f"- ❌ {x}" for x in bad]) + "\n"
    summary(md, "previews/check-latest.md", cfg)
    return 1 if bad else 0


# ---------------------------------------------------------------- setup facebook
def cmd_setup_facebook(cfg, args) -> int:
    graph = Graph(cfg.graph_version)
    try:
        rep = setup_facebook(cfg, graph, os.environ.get("FB_APP_ID", ""), os.environ.get("FB_APP_SECRET", ""),
                             os.environ.get("FB_USER_TOKEN", ""))
    except (TokenError, FacebookError) as e:
        msg = e.short() if isinstance(e, FacebookError) else str(e)
        summary(f"## Подключение Facebook\n\n❌ {msg}\n\nЧастая причина: токен из Graph API Explorer действует "
                f"около часа — получите новый и сразу запустите этот шаг.\n")
        return 1
    st = state_of(cfg)
    st.ensure_source_cutoff(datetime.now(timezone.utc))
    st.save()
    lines = ["## Подключение Facebook", "", f"✅ Получены токены страниц: **{len(rep['ok'])} из 34**"]
    lines.append("Срок действия токенов страниц: " + ("бессрочно" if not rep["expires_at"] else
                 datetime.fromtimestamp(rep["expires_at"], timezone.utc).strftime("%Y-%m-%d")))
    if rep["scopes"]:
        lines.append("Разрешения: " + ", ".join(rep["scopes"]))
    need = {"pages_manage_posts", "pages_read_engagement", "pages_manage_engagement", "pages_show_list"}
    if rep["scopes"] and not need.issubset(rep["scopes"]):
        lines.append("❌ Не хватает разрешений: " + ", ".join(sorted(need - set(rep["scopes"]))))
    if rep["missing"]:
        lines.append("❌ Нет доступа к страницам (добавьте их в портфолио Interes World и назначьте системному "
                     "пользователю autopost): "
                     + "; ".join(p.name for p in rep["missing"]))
    if rep["no_rights"]:
        lines.append("❌ Недостаточно прав на страницах: " + "; ".join(f"{p.name} ({', '.join(t)})" for p, t in rep["no_rights"]))
    if rep["extra_pages"]:
        lines.append("ℹ️ Доступны и другие страницы (не используются): " + "; ".join(n or i for i, n in rep["extra_pages"]))
    lines.append("\nНовыми историями считаются посты, опубликованные на ваших страницах ПОСЛЕ этого момента "
                 "(старые посты и всё, что публиковал Make, не берутся).")
    lines.append("Секрет FB_USER_TOKEN не удаляйте: когда в портфолио добавятся новые страницы, "
                 "этот шаг можно просто запустить ещё раз.")
    summary("\n".join(lines) + "\n", "previews/facebook-setup.md", cfg)
    return 0 if not rep["missing"] and not rep["no_rights"] else 1


# ---------------------------------------------------------------- private test
def _peek_source(cfg, graph, tokens, link):
    if link:
        info = parse_link(link, cfg.profile_to_page())
        post = fetch_post(graph, info, tokens, cfg.fallback_source_page_id)
        from .sources import source_key
        from .state import text_fingerprint
        return {"key": source_key(info["page_id"], info["part"]), "page_id": info["page_id"], "text": post["text"],
                "image_url": post["image_url"], "origin": "manual", "link": link,
                "fingerprint": text_fingerprint(post["text"])}
    tmp = State(Path(tempfile.mkdtemp()) / "s.json")
    tmp.data = copy.deepcopy(state_of(cfg).data)
    tmp.data["source_cutoff"] = None  # для теста подойдёт любая недавняя история с фото
    return pick_source(cfg, graph, tokens, tmp, datetime.now(timezone.utc), lambda m: None)


def cmd_private_test(cfg, args, services=None, tokens=None) -> int:
    graph, wp, http = services or real_services(cfg)
    st = state_of(cfg)
    tz = ZoneInfo(cfg.tz)
    tokens = tokens or load_tokens(cfg)
    page = cfg.page(args.page or cfg.test_page_id())
    if not page:
        print("Страница не найдена в config/pages.yaml")
        return 1
    src = _peek_source(cfg, graph, tokens, args.source)
    if not src:
        summary("## Закрытый тест\n\n❌ Нет новой истории. Опубликуйте историю с фото на любой из 34 страниц "
                "или запустите тест со ссылкой на пост.\n")
        return 1
    test_id = datetime.now(tz).strftime("test-%Y%m%d-%H%M%S")
    gen = Generator(cfg, backend=args.backend)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {lang: pool.submit(gen.generate, lang, src["text"]) for lang in LANG_ORDER}
    texts, errors = {}, {}
    for lang, f in futs.items():
        try:
            texts[lang] = f.result().as_dict()
        except GenerationError as e:
            errors[lang] = str(e)
    tdir = cfg.path(f"content/tests/{test_id}")
    tdir.mkdir(parents=True, exist_ok=True)
    for lang, t in texts.items():
        (tdir / f"{lang}.json").write_text(json.dumps(t, ensure_ascii=False, indent=1), encoding="utf-8")
    checks = []
    rec = {"source": src, "page_id": page.page_id, "created": datetime.now(tz).isoformat(timespec="seconds")}
    # WordPress: фото в медиатеку + черновик (не виден посетителям) → проверка → удаление черновика
    lang = page.lang
    if lang in texts:
        draft_id, fb_id = None, None
        tok = tokens.get(page.page_id)
        try:
            data, mime = download(http, src["image_url"])
            media = wp.upload_media(data, f"{test_id}.jpg", mime)
            rec["wp_media_id"], rec["wp_media_url"] = media["id"], media.get("source_url")
            st.data["tests"][test_id] = rec
            st.save()
            draft = wp.create_post(texts[lang]["title"], texts[lang]["article_html"], status="draft")
            draft_id = draft["id"]
            back = wp.get_post(draft_id)
            checks.append(f"✅ WordPress: черновик создан (id {draft_id}, статус «{back.get('status')}»), "
                          "посетителям не виден")
            # Facebook: отложенный пост через 7 дней (виден только администраторам) → проверка → удаление
            ts = int((datetime.now(timezone.utc) + timedelta(days=7)).timestamp())
            res = graph.create_photo_post(page.page_id, tok, data, "story.jpg", texts[lang]["fb_text"],
                                          scheduled_ts=ts, mime=mime)
            fb_id = res.get("post_id") or res["id"]
            rec["fb_test_post"] = fb_id
            st.save()
            published = graph.is_published(fb_id, tok)
            checks.append(f"{'✅' if not published else '❌'} Facebook «{page.name}»: отложенный пост создан, "
                          f"опубликован: {'да' if published else 'нет'}")
        except Exception as e:  # noqa: BLE001 — показываем любую причину
            msg = e.short() if isinstance(e, FacebookError) else redact(e)
            checks.append(f"❌ {type(e).__name__}: {msg}")
        finally:
            if not args.keep:
                if draft_id:
                    try:
                        wp.delete_post(draft_id)
                        checks.append("✅ Черновик WordPress удалён")
                    except Exception as e:  # noqa: BLE001
                        checks.append(f"❌ Не удалось удалить черновик {draft_id}: {redact(e)} — удалите вручную")
                if fb_id:
                    try:
                        graph.delete(fb_id, tok)
                        rec["fb_test_post_deleted"] = True
                        checks.append("✅ Тестовый отложенный пост Facebook удалён")
                    except Exception as e:  # noqa: BLE001
                        checks.append(f"❌ Не удалось удалить тестовый пост {fb_id}: {redact(e)} — удалите его "
                                      "в Meta Business Suite → Запланированные")
    st.data["tests"][test_id] = rec
    st.save()
    md = [f"## Закрытый тест {test_id}", "", "Ничего не опубликовано публично.", "",
          f"**Источник:** {src['link']}", f"**Фото:** {src['image_url'][:120]}…",
          f"**Тестовая страница:** {page.name} ({page.page_id}, язык {page.lang})", "", *checks, ""]
    for lg in LANG_ORDER:
        names = ", ".join(p.name for p in cfg.pages_for(lg))
        if lg in texts:
            t = texts[lg]
            md += [f"### {lg} → {len(cfg.pages_for(lg))} стр.: {names}", "", f"**Заголовок:** {t['title']}", "",
                   "**Текст для Facebook:**", "", t["fb_text"], "",
                   f"**Статья:** {len(t['article_html'].split())} слов (полностью — content/tests/{test_id}/{lg}.json)"]
            if t["warnings"]:
                md.append("Замечания: " + "; ".join(t["warnings"]))
            md.append("")
        else:
            md += [f"### {lg}: ❌ {errors.get(lg)}", ""]
    summary("\n".join(md), f"previews/{test_id}.md", cfg)
    return 1 if errors or any(c.startswith("❌") for c in checks) else 0


# ---------------------------------------------------------------- public test
def cmd_public_test(cfg, args, services=None, tokens=None) -> int:
    if args.confirm != "PUBLISH":
        print("Для публичного теста введите в поле confirm слово PUBLISH")
        return 1
    graph, wp, http = services or real_services(cfg)
    st = state_of(cfg)
    tests = st.data.get("tests") or {}
    test_id = args.test_id or (sorted(tests)[-1] if tests else None)
    if not test_id or test_id not in tests:
        print("Не найден закрытый тест — сначала запустите «3 · Закрытый тест»")
        return 1
    rec = tests[test_id]
    page = cfg.page(args.page or rec["page_id"])
    tokens = tokens or load_tokens(cfg)
    t = json.loads(cfg.path(f"content/tests/{test_id}/{page.lang}.json").read_text(encoding="utf-8"))
    import html as _html
    img = (f'<figure class="wp-block-image size-full"><img src="{_html.escape(rec.get("wp_media_url") or "")}" '
           f'alt="{_html.escape(t["title"])}"/></figure>\n')
    out = [f"## Публичный тест {test_id}", ""]
    try:
        post = wp.create_post(t["title"], img + t["article_html"], status="publish",
                              featured_media=rec.get("wp_media_id") if cfg.site.get("wp_featured_image") else None)
        link = cfg.site.get("comment_link_format").format(wp_id=post["id"])
        out.append(f"✅ Статья опубликована: {link}")
        data, _mime = download(http, rec.get("wp_media_url") or rec["source"]["image_url"])
        res = graph.create_photo_post(page.page_id, tokens[page.page_id], data, "story.jpg", t["fb_text"])
        target = res.get("post_id") or res["id"]
        out.append(f"✅ Пост опубликован на «{page.name}»: https://www.facebook.com/{target}")
        c = graph.comment(target, tokens[page.page_id], link)
        out.append(f"✅ Первый комментарий со ссылкой добавлен (id {c.get('id')})")
        st.remember_output(target, t["fb_text"])
        st.remember_output(res.get("id"))
        st.mark_source(rec["source"]["key"], status="used", slot=f"public-test {test_id}",
                       link=rec["source"]["link"], fingerprint=rec["source"].get("fingerprint"))
        rec["public"] = {"wp_id": post["id"], "fb_post": target, "comment": c.get("id"), "page": page.page_id}
        st.save()
        code = 0
    except (FacebookError, WordPressError, SourceError) as e:
        out.append(f"❌ {e.short() if isinstance(e, FacebookError) else e}")
        code = 1
    summary("\n".join(out) + "\n", f"previews/{test_id}-public.md", cfg)
    return code


# ---------------------------------------------------------------- tick
def _checkpoint():
    """Промежуточное сохранение состояния в репозиторий (только в GitHub Actions)."""
    if os.environ.get("AUTOPOST_CHECKPOINT") == "1":
        import subprocess
        subprocess.run(["bash", "scripts/commit-state.sh", "автопостинг — промежуточное сохранение"], check=False)


def cmd_tick(cfg, args) -> int:
    st = state_of(cfg)
    enabled = bool(cfg.settings.get("schedule_enabled"))
    unfinished = any(s.get("prepared") and s.get("status") not in ("done", "done_with_errors", "missed")
                     for s in st.slots.values())
    if not enabled and not unfinished:
        if st.data.get("enabled_since"):
            st.data["enabled_since"] = None
            st.save()
        summary("## Автопостинг\n\nРасписание выключено (`schedule_enabled: false`).\n")
        return 0
    graph, wp, http = real_services(cfg)
    try:
        tokens = load_tokens(cfg)
    except TokenError as e:
        summary(f"## Автопостинг\n\n❌ {e}\n")
        return 1
    rep = Pipeline(cfg, st, graph, wp, Generator(cfg), tokens, http=http, checkpoint=_checkpoint).tick()
    summary(rep.markdown())
    return 1 if rep.final_errors else 0


# ---------------------------------------------------------------- status
def cmd_status(cfg, args) -> int:
    st = state_of(cfg)
    lines = ["## Состояние слотов", "", "| Слот | Статус | Источник | Готово страниц |", "|---|---|---|---|"]
    for key in sorted(st.slots)[-20:]:
        s = st.slots[key]
        pages = [e for L in s.get("langs", {}).values() for e in L.get("pages", {}).values()]
        done = sum(1 for e in pages if e["status"] in ("done", "skipped_source"))
        lines.append(f"| {key} | {s.get('status')} | {(s.get('source') or {}).get('origin', '—')} | {done}/{len(pages)} |")
    summary("\n".join(lines) + "\n")
    return 0


# ---------------------------------------------------------------- simulate (сухой прогон)
def cmd_simulate(cfg, args) -> int:
    from .simulate import run_simulation
    ok, md = run_simulation(cfg, days=args.days)
    summary(md, "previews/dry-run.md", cfg)
    return 0 if ok else 1


def cmd_set_schedule(cfg, args) -> int:
    import re
    p = cfg.path("config/settings.yaml")
    text = p.read_text(encoding="utf-8")
    value = "true" if args.mode == "on" else "false"
    new, n = re.subn(r"^schedule_enabled:\s*\w+", f"schedule_enabled: {value}", text, count=1, flags=re.M)
    if n != 1:
        print("В settings.yaml не найдена строка schedule_enabled")
        return 1
    p.write_text(new, encoding="utf-8")
    st = state_of(cfg)
    st.data["enabled_since"] = None  # отсчёт начнётся с первого запуска после включения
    st.ensure_source_cutoff(datetime.now(timezone.utc))
    st.save()
    summary(f"## Расписание\n\nАвтопостинг {'ВКЛЮЧЁН' if value == 'true' else 'выключен'}. "
            f"Часы ({cfg.tz}): {', '.join(cfg.slots)}\n")
    return 0


def cmd_ops(cfg, args) -> int:
    """Команда из файла ops/request.json (её коммитит Claude) — чтобы не нажимать кнопки вручную."""
    req = json.loads(cfg.path("ops/request.json").read_text(encoding="utf-8"))
    cmd, a = req.get("cmd"), req.get("args") or {}
    ns = argparse.Namespace
    if cmd == "check":
        return cmd_check(cfg, ns())
    if cmd == "setup-facebook":
        return cmd_setup_facebook(cfg, ns())
    if cmd == "private-test":
        return cmd_private_test(cfg, ns(source=a.get("source", ""), page=a.get("page", ""), keep=False,
                                         backend=a.get("backend")))
    if cmd == "public-test":
        return cmd_public_test(cfg, ns(test_id=a.get("test_id", ""), page=a.get("page", ""),
                                        confirm=a.get("confirm", "")))
    if cmd == "set-schedule":
        return cmd_set_schedule(cfg, ns(mode=a.get("mode", "off")))
    if cmd == "status":
        return cmd_status(cfg, ns())
    print(f"Неизвестная команда: {cmd}")
    return 1


def cmd_render_workflow(cfg, args) -> int:
    path = write_workflow(cfg)
    print(f"Обновлено расписание в {path}:\n{render_cron_block(cfg)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="autopost")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("setup-facebook")
    p = sub.add_parser("private-test")
    p.add_argument("--source", default="")
    p.add_argument("--page", default="")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--backend", default=None)
    p = sub.add_parser("public-test")
    p.add_argument("--test-id", default="")
    p.add_argument("--page", default="")
    p.add_argument("--confirm", default="")
    sub.add_parser("tick")
    sub.add_parser("status")
    p = sub.add_parser("simulate")
    p.add_argument("--days", type=int, default=2)
    sub.add_parser("render-workflow")
    sub.add_parser("ops")
    p = sub.add_parser("set-schedule")
    p.add_argument("mode", choices=["on", "off"])
    args = ap.parse_args(argv)
    cfg = config_mod.load()
    fn = {"check": cmd_check, "setup-facebook": cmd_setup_facebook, "private-test": cmd_private_test,
          "public-test": cmd_public_test, "tick": cmd_tick, "status": cmd_status, "simulate": cmd_simulate,
          "render-workflow": cmd_render_workflow, "set-schedule": cmd_set_schedule, "ops": cmd_ops}[args.cmd]
    try:
        return fn(cfg, args)
    except (TokenError, AuthProblem) as e:
        summary(f"❌ {e}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
