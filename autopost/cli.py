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
from .sources import (SourceError, fetch_post, google_sheet_links, link_key, manual_links, normalize_link, parse_link,
                      pick_source, resolve_share_link, sheet_baseline, source_key)
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
    # site.wp_auth: application_password (пароль приложения WordPress) или make_connector (ключ WP_API_KEY)
    api_key = os.environ.get("WP_API_KEY") if cfg.site.get("wp_auth") == "make_connector" else None
    wp = WordPress(cfg.site["url"], os.environ.get("WP_USER", ""), os.environ.get("WP_APP_PASSWORD", ""),
                   api_key=api_key)
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
    res(not probs, f"Конфигурация: {config_mod.describe(cfg)}; все 34 страницы из Make на месте" if not probs
        else "Конфигурация: " + "; ".join(probs))
    res(True, f"Часы публикаций ({cfg.tz}): {', '.join(cfg.slots)} · расписание "
              f"{'ВКЛЮЧЕНО' if cfg.settings.get('schedule_enabled') else 'выключено'}")
    for name in ("FB_APP_ID", "FB_APP_SECRET"):
        res(bool(os.environ.get(name)), f"Секрет {name} {'задан' if os.environ.get(name) else 'НЕ задан'}")
    mc = cfg.site.get("wp_auth") == "make_connector"
    wp_ok = os.environ.get("WP_API_KEY") if mc else (os.environ.get("WP_USER") and os.environ.get("WP_APP_PASSWORD"))
    res(bool(wp_ok), f"WordPress {cfg.site['url']}: " + ("ключ Make Connector (WP_API_KEY)" if mc
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
        res(not failed, f"Facebook: все {len(cfg.pages)} страниц доступны" if not failed
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
    lines = ["## Подключение Facebook", "", f"✅ Получены токены страниц: **{len(rep['ok'])} из {len(cfg.pages)}**"]
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
        lines.append("ℹ️ Доступны и другие страницы (пока не в config/pages.yaml): "
                     + "; ".join(f"{n or '?'} (page_id {i})" for i, n in rep["extra_pages"]))
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
        summary("## Закрытый тест\n\n❌ Нет новой истории. Опубликуйте историю с фото на любой из ваших страниц "
                "или запустите тест со ссылкой на пост.\n")
        return 1
    test_id = datetime.now(tz).strftime("test-%Y%m%d-%H%M%S")
    gen = Generator(cfg, backend=args.backend)
    langs = cfg.active_langs()
    with ThreadPoolExecutor(max_workers=len(langs)) as pool:
        futs = {lang: pool.submit(gen.generate, lang, src["text"]) for lang in langs}
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
            fb_id = res.get("id")
            rec["fb_test_photo"] = fb_id
            st.save()
            sched = [i for i in graph.scheduled_posts(page.page_id, tok)
                     if int(i.get("scheduled_publish_time") or 0) == ts]
            if sched:
                rec["fb_test_post"] = sched[0]["id"]
                published = graph.is_published(sched[0]["id"], tok)
                checks.append(f"{'✅' if not published else '❌'} Facebook «{page.name}»: пост с фото стоит в "
                              f"отложенных на {datetime.fromtimestamp(ts, tz):%d.%m %H:%M}, опубликован: "
                              f"{'да' if published else 'нет'}")
            else:
                checks.append(f"❌ Facebook «{page.name}»: фото загружено, но в списке отложенных постов его нет")
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
                deleted = False
                for obj in (rec.get("fb_test_post"), fb_id):
                    if obj and not deleted:
                        try:
                            graph.delete(obj, tok)
                            deleted = True
                        except Exception:  # noqa: BLE001
                            pass
                if fb_id:
                    rec["fb_test_post_deleted"] = deleted
                    checks.append("✅ Тестовый отложенный пост Facebook удалён" if deleted else
                                  "❌ Не удалось удалить тестовый пост — удалите его в Meta Business Suite → "
                                  "Планировщик (запланирован на неделю вперёд)")
    st.data["tests"][test_id] = rec
    st.save()
    md = [f"## Закрытый тест {test_id}", "", "Ничего не опубликовано публично.", "",
          f"**Источник:** {src['link']}", f"**Фото:** {src['image_url'][:120]}…",
          f"**Тестовая страница:** {page.name} ({page.page_id}, язык {page.lang})", "", *checks, ""]
    for lg in cfg.active_langs():
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


def _group_states(cfg) -> dict:
    return {g: State(config_mod.group_state_path(cfg, g)) for g in cfg.groups()}


def _unfinished(st: State) -> bool:
    return any(s.get("prepared") and s.get("status") not in ("done", "done_with_errors", "missed", "cancelled")
               for s in st.slots.values())


def cmd_tick(cfg, args) -> int:
    """Один запуск по расписанию: каждая группа страниц — своя история из своей очереди queue/<группа>.txt."""
    enabled = bool(cfg.settings.get("schedule_enabled"))
    states = _group_states(cfg)
    if not enabled and not any(_unfinished(st) for st in states.values()):
        for st in states.values():
            if st.data.get("enabled_since"):
                st.data["enabled_since"] = None
                st.save()
        summary("## Автопостинг\n\nРасписание выключено (`schedule_enabled: false`).\n")
        return 0
    try:
        tokens = load_tokens(cfg)
    except TokenError as e:
        summary(f"## Автопостинг\n\n❌ {e}\n")
        return 1
    import threading
    lock = threading.Lock()

    def checkpoint():  # сохранения в репозиторий — по одному, группы работают параллельно
        with lock:
            _checkpoint()

    def run_group(g):
        gcfg = config_mod.group_view(cfg, g)
        g_graph, g_wp, g_http = real_services(cfg)  # своё соединение у каждой группы
        try:
            rep = Pipeline(gcfg, states[g], g_graph, g_wp, Generator(gcfg), tokens, http=g_http,
                           checkpoint=checkpoint).tick()
        except Exception as e:  # noqa: BLE001 — сбой одной группы не останавливает остальные
            rep = Report()
            rep.error(f"группа {cfg.group_label(g)}: непредвиденная ошибка {type(e).__name__}: {e}", final=True)
        return g, rep

    with ThreadPoolExecutor(max_workers=len(states) or 1) as pool:
        results = list(pool.map(run_group, list(states)))
    md, final = ["## Автопостинг по группам", ""], 0
    for g, rep in results:
        final += len(rep.final_errors)
        md.append(rep.markdown(title=f"{cfg.group_label(g)} · {len(config_mod.group_view(cfg, g).pages)} стр.")
                  .replace("## ", "### ", 1))
    summary("\n".join(md), "previews/tick-latest.md", cfg)
    return 1 if final else 0


# ---------------------------------------------------------------- status
def cmd_status(cfg, args) -> int:
    lines = ["## Состояние выпусков по группам", "",
             "| Группа | Выпуск | Статус | История | Готово страниц |", "|---|---|---|---|---|"]
    for g, st in _group_states(cfg).items():
        for key in sorted(st.slots)[-10:]:
            s = st.slots[key]
            pages = [e for L in s.get("langs", {}).values() for e in L.get("pages", {}).values()]
            done = sum(1 for e in pages if e["status"] in ("done", "skipped_source"))
            link = ((s.get("source") or {}).get("link") or "—")[:60]
            lines.append(f"| {cfg.group_label(g)} | {key} | {s.get('status')} | {link} | {done}/{len(pages)} |")
    summary("\n".join(lines) + "\n", "previews/status.md", cfg)
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


def cmd_wp_diagnose(cfg) -> int:
    """Пошаговая проверка записи в WordPress (тестовые объекты сразу удаляются)."""
    _g, wp, _h = real_services(cfg)
    out = ["## Диагностика WordPress", ""]
    def step(name, fn):
        try:
            r = fn()
            out.append(f"- ✅ {name}: {r}")
            return r
        except Exception as e:  # noqa: BLE001
            out.append(f"- ❌ {name}: {type(e).__name__}: {redact(e)}")
            return None
    step("users/me", lambda: wp.me().get("name"))
    tiny = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffd9")
    h = {"IWC-API-KEY": wp.api_key} if wp.api_key else {}
    def raw_multipart():
        r = wp.http.post(wp.api + "/media", headers=h, auth=wp.auth,
                         files={"file": ("autopost-test.jpg", tiny, "image/jpeg")}, timeout=60)
        return f"HTTP {r.status_code} ct={r.headers.get('Content-Type')} body={r.text[:400]}"
    step("медиа multipart (сырой ответ)", raw_multipart)
    def raw_multipart2():
        r = wp.http.post(wp.api + "/media", headers=h, auth=wp.auth,
                         files={"file": ("autopost-test2.jpg", tiny, "image/jpeg")},
                         data={"title": "autopost test"}, timeout=60)
        return f"HTTP {r.status_code} body={r.text[:400]}"
    step("медиа multipart + title (сырой ответ)", raw_multipart2)
    def find_media():
        r = wp.http.get(wp.api + "/media", headers=h, auth=wp.auth,
                        params={"search": "autopost-test", "per_page": 10, "context": "edit"}, timeout=60)
        items = r.json() if r.status_code == 200 else []
        ids = [m.get("id") for m in items] if isinstance(items, list) else items
        for mid in (ids if isinstance(ids, list) else []):
            try:
                wp.delete_media(mid)
            except Exception:  # noqa: BLE001
                pass
        return f"найдено и удалено: {ids}"
    step("найти и удалить тестовые медиа", find_media)
    summary("\n".join(out) + "\n", "previews/wp-diagnose.md", cfg)
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
    if cmd == "wp-diagnose":
        return cmd_wp_diagnose(cfg)
    if cmd == "sheet-status":
        return cmd_sheet_status(cfg)
    if cmd == "check-queue":
        return cmd_check_queue(cfg)
    if cmd == "site-check":
        return cmd_site_check(cfg, a.get("url", ""))
    if cmd == "peek-links":
        return cmd_peek_links(cfg, a.get("links") or [])
    if cmd == "repost":
        return cmd_repost(cfg, a.get("from", ""), a.get("to", ""))
    if cmd == "inspect-posts":
        return cmd_inspect_posts(cfg, a.get("page", ""), a.get("posts") or [])
    if cmd == "resolve-links":
        return cmd_resolve_links(cfg, a.get("links") or [])
    if cmd == "cancel-slot":
        rc = 0
        for g, st in _group_states(cfg).items():
            if a.get("slot", "") in st.slots:
                rc |= cmd_cancel_slot(config_mod.group_view(cfg, g), a.get("slot", ""), state=st)
        return rc
    if cmd == "cost-test":
        from .costtest import run as cost_test
        return cost_test(cfg, n=int(a.get("n", 10)),
                         models=tuple(a.get("models") or ("claude-sonnet-5-5", "claude-haiku-5-5")),
                         parallel=int(a.get("parallel", 4)))
    print(f"Неизвестная команда: {cmd}")
    return 1


def repost_slot(cfg, st: State, g: str, from_key: str, to_key: str) -> str:
    """Готовит повтор выпуска: новый слот с теми же текстами и ссылкой на ту же статью (без генерации и новой статьи)."""
    import shutil
    old = st.slots.get(from_key)
    if not old or not old.get("prepared"):
        return "нечего повторять (выпуск не готовился)"
    cur = st.slots.get(to_key)
    if cur and (cur.get("source_key") or cur.get("prepared")):
        return f"выпуск {to_key} уже занят — повтор не поставлен"
    tz = ZoneInfo(cfg.tz)
    dt_new = datetime.strptime(to_key, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    dt_old = datetime.strptime(from_key, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
    new = {"slot_iso": dt_new.isoformat(), "status": "prepared", "prepared": True, "repost_of": from_key,
           "source_key": old["source_key"], "source": copy.deepcopy(old.get("source") or {}), "langs": {}}
    for lang, L in (old.get("langs") or {}).items():
        if L.get("generated") and L.get("wp_id"):
            src = cfg.path(f"content/{dt_old:%Y-%m-%d}/{g}/{dt_old:%H%M}-{lang}.json")
            dst = cfg.path(f"content/{dt_new:%Y-%m-%d}/{g}/{dt_new:%H%M}-{lang}.json")
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            new["langs"][lang] = {"generated": True, "title": L.get("title"), "wp_id": L["wp_id"], "wp_live": True,
                                  "pages": {}}
        else:
            new["langs"][lang] = {"pages": {pid: {"status": "no_access", "attempts": 0}
                                            for pid in (L.get("pages") or {})}, "skipped": "не повторяется"}
    st.slots[to_key] = new
    st.save()
    return "поставлен"


def cmd_repost(cfg, from_key: str, to_key: str) -> int:
    out = [f"## Повтор выпуска {from_key} → {to_key}", ""]
    for g, st in _group_states(cfg).items():
        out.append(f"- {cfg.group_label(g)}: {repost_slot(cfg, st, g, from_key, to_key)}")
    summary("\n".join(out) + "\n", "previews/repost.md", cfg)
    return 0


def cmd_site_check(cfg, url: str) -> int:
    """Проверка нового сайта WordPress без ключей: отвечает ли REST API, какие плагины-подключения есть."""
    import requests
    base = (url or cfg.settings["site"]["url"]).rstrip("/")
    out = [f"## Проверка сайта {base}", ""]
    ua = {"User-Agent": "interes-autopost/1.0"}
    for path in ("/wp-json/", "/wp-json/wp/v2/users/me", "/wp-json/wp/v2/posts?per_page=1", "/?rest_route=/"):
        try:
            r = requests.get(base + path, headers=ua, timeout=30)
            ct = r.headers.get("Content-Type", "")
            extra = ""
            if path == "/wp-json/" and "json" in ct:
                try:
                    d = r.json()
                    extra = f" · сайт «{d.get('name')}» · namespaces: {', '.join(d.get('namespaces', []))[:600]}"
                    extra += f" · auth: {', '.join((d.get('authentication') or {}).keys())}"
                except ValueError:
                    pass
            out.append(f"- {path}: HTTP {r.status_code} · {ct[:40]}{extra} · {r.text[:120]!r}")
        except requests.RequestException as e:
            out.append(f"- {path}: ошибка {e}")
    # Доходит ли заголовок авторизации до WordPress: заведомо несуществующий логин должен дать invalid_username,
    # а rest_not_logged_in значит, что хостинг вырезает заголовок. Затем — код ответа с секретами (без самих секретов).
    probes = [("пробный несуществующий логин", ("autopost-probe-no-such-user", "abcd efgh ijkl mnop qrst uvwx"))]
    if os.environ.get("WP_USER") and os.environ.get("WP_APP_PASSWORD"):
        probes.append(("секреты WP_USER + WP_APP_PASSWORD", (os.environ["WP_USER"], os.environ["WP_APP_PASSWORD"])))
    for label, auth in probes:
        try:
            r = requests.get(base + "/wp-json/wp/v2/users/me", headers=ua, auth=auth, timeout=30)
            try:
                d = r.json()
                code = d.get("code") or f"вход выполнен, пользователь «{d.get('name')}»"
            except ValueError:
                code = r.text[:80]
            out.append(f"- авторизация ({label}): HTTP {r.status_code} · {code}")
        except requests.RequestException as e:
            out.append(f"- авторизация ({label}): ошибка {e}")
    out += ["", "Расшифровка: invalid_username — такого логина на сайте нет; incorrect_password — пароль приложения "
            "неверный; rest_not_logged_in — заголовок авторизации не доходит до WordPress."]
    summary("\n".join(out) + "\n", "previews/site-check.md", cfg)
    return 0


def cmd_peek_links(cfg, links: list) -> int:
    """Показывает начало текста по ссылкам (ничего не ставит в очередь и не публикует)."""
    from .generate import detect_language
    graph = Graph(cfg.graph_version)
    tokens = load_tokens(cfg)
    st = State(Path(tempfile.mkdtemp()) / "s.json")
    out = ["## Ссылки: что внутри", ""]
    for i, link in enumerate(links, 1):
        try:
            info = parse_link(normalize_link(link, st), cfg.profile_to_page())
            post = fetch_post(graph, info, tokens, cfg.fallback_source_page_id)
            first = " ".join(post["text"].split())[:150]
            out.append(f"{i}. ({detect_language(post['text']) or '?'}, {len(post['text'])} знаков) «{first}…»")
        except SourceError as e:
            out.append(f"{i}. ❌ {e}")
    summary("\n".join(out) + "\n", "previews/peek-links.md", cfg)
    return 0


def cmd_inspect_posts(cfg, page_id: str, posts: list) -> int:
    """Диагностика видимости: режим приложения, лента страницы, поля наших постов. Ничего не меняет."""
    import requests
    graph = Graph(cfg.graph_version)
    tokens = load_tokens(cfg)
    tok = tokens.get(page_id)
    out = ["## Проверка видимости постов", ""]
    app_id, secret = os.environ.get("FB_APP_ID", ""), os.environ.get("FB_APP_SECRET", "")
    base = f"https://graph.facebook.com/{cfg.graph_version}"
    try:
        r = requests.get(f"{base}/{app_id}", params={"access_token": f"{app_id}|{secret}",
                         "fields": "name,link,category,company,restrictions"}, timeout=30)
        out.append(f"Приложение: {redact(r.text[:400])}")
        r = requests.get(f"{base}/{app_id}/roles", params={"access_token": f"{app_id}|{secret}"}, timeout=30)
        out.append(f"Роли приложения: {redact(r.text[:600])}")
    except Exception as e:  # noqa: BLE001
        out.append(f"Приложение: ошибка {e}")
    for pid in posts:
        try:
            info = graph.get(pid, tok, fields="id,created_time,is_published,is_hidden,timeline_visibility,"
                                               "status_type,privacy,permalink_url,is_eligible_for_promotion")
            out.append(f"- пост {pid}: {redact(json.dumps(info, ensure_ascii=False))[:500]}")
        except FacebookError as e:
            out.append(f"- пост {pid}: ошибка {e.short()}")
    for edge in ("published_posts", "feed"):
        try:
            data = graph.get(f"{page_id}/{edge}", tok, fields="id,created_time,message", limit=6).get("data", [])
            out.append(f"\n{edge}: " + "; ".join(f"{d['id']} {d.get('created_time', '')[:16]} "
                                                f"«{(d.get('message') or '')[:40]}»" for d in data))
        except FacebookError as e:
            out.append(f"{edge}: ошибка {e.short()}")
    summary("\n".join(out) + "\n", "previews/inspect-posts.md", cfg)
    return 0


def cmd_resolve_links(cfg, links: list) -> int:
    """Раскрывает короткие ссылки facebook.com/share/… — без браузера."""
    p2p = cfg.profile_to_page()
    names = {p.page_id: p.name for p in cfg.all_pages}
    out = ["## Раскрытие ссылок", ""]
    for link in links:
        info = resolve_share_link(link)
        pid = p2p.get(info.get("id", ""), info.get("id", ""))
        out.append(f"- {link} → url={info.get('url', '')[:150]} | story_fbid={info.get('story_fbid', '')} "
                   f"| id={info.get('id', '')} ({names.get(pid, '?')}) | post_id={info.get('post_id', '')} "
                   f"| title={info.get('title', '')}")
    summary("\n".join(out) + "\n", "previews/resolve-links.md", cfg)
    return 0


def cmd_cancel_slot(cfg, key: str, services=None, tokens=None, state=None) -> int:
    """Отменяет подготовленный слот: удаляет отложенные посты Facebook и статьи WordPress, слот больше не трогается."""
    st = state or state_of(cfg)
    s = st.slots.get(key)
    if not s:
        print(f"Слот {key} не найден")
        return 1
    graph, wp, _http = services or real_services(cfg)
    tokens = tokens or load_tokens(cfg)
    ts = int(datetime.fromisoformat(s["slot_iso"]).timestamp())
    removed, kept, errors = 0, 0, []
    for lang, L in (s.get("langs") or {}).items():
        try:
            sub = f"{cfg.group}/" if cfg.group else ""
            txt = json.loads(cfg.path(f"content/{key[:10]}/{sub}{key[11:13]}{key[14:16]}-{lang}.json")
                             .read_text(encoding="utf-8"))["fb_text"]
        except (OSError, KeyError, ValueError):
            txt = ""
        for pid, e in (L.get("pages") or {}).items():
            if e.get("status") not in ("scheduled", "sending", "published", "failed"):
                continue
            tok = tokens.get(pid)
            if not tok:
                continue
            name = (cfg.page(pid).name if cfg.page(pid) else pid)
            gone = False
            try:
                for sp in graph.scheduled_posts(pid, tok):
                    same_time = int(sp.get("scheduled_publish_time") or 0) == ts
                    ours = not txt or (sp.get("message") or "")[:60] == txt[:60]
                    if same_time and ours:
                        graph.delete(sp["id"], tok)
                        gone = True
                if not gone and e.get("post_id"):
                    graph.delete(e["post_id"], tok)
                    gone = True
                if not gone and e.get("photo_id"):
                    graph.delete(e["photo_id"], tok)
                    gone = True
            except FacebookError as err:
                errors.append(f"{name}: {err.short()}")
            if gone:
                e["status"] = "cancelled"
                removed += 1
            elif e.get("status") in ("scheduled", "sending", "published"):
                kept += 1
            st.save()
        if L.get("wp_id") and not L.get("wp_cancelled"):
            try:
                wp.delete_post(L["wp_id"], force=True)
                L["wp_cancelled"] = True
            except WordPressError as err:
                errors.append(f"статья {lang} ({L['wp_id']}): {err}")
    s["status"] = "cancelled"
    st.save()
    left = []
    for lang, L in (s.get("langs") or {}).items():
        for pid in (L.get("pages") or {}):
            tok = tokens.get(pid)
            if not tok:
                continue
            try:
                if any(int(sp.get("scheduled_publish_time") or 0) == ts for sp in graph.scheduled_posts(pid, tok)):
                    left.append(cfg.page(pid).name if cfg.page(pid) else pid)
            except FacebookError:
                pass
    wp_gone = sum(1 for L in (s.get("langs") or {}).values() if L.get("wp_cancelled"))
    md = [f"## Отмена выпуска {key}", "",
          f"{'✅' if removed else '⚠️'} Удалено отложенных постов Facebook: **{removed}**",
          f"✅ Удалено статей на сайте: **{wp_gone}**"]
    if kept:
        md.append(f"❌ Не удалось удалить: {kept}")
    if left:
        md.append("❌ На этот час ещё что-то запланировано на страницах: " + "; ".join(left))
    else:
        md.append("✅ Проверка: на этот час на страницах больше ничего не запланировано")
    md += [f"- {e}" for e in errors[:20]]
    grp = f"-{cfg.group}" if cfg.group else ""
    summary("\n".join(md) + "\n", f"previews/cancel-{key[:10]}-{key[11:13]}{key[14:16]}{grp}.md", cfg)
    return 1 if (kept or left) else 0


def cmd_check_queue(cfg) -> int:
    """Проверяет очереди queue/<группа>.txt: читается ли каждый пост (текст + фото). Ничего не публикует."""
    graph, _wp, _http = real_services(cfg)
    tokens = load_tokens(cfg)
    p2p = cfg.profile_to_page()
    out = ["## Очереди ссылок по группам", ""]
    bad = 0
    for g, st in _group_states(cfg).items():
        gcfg = config_mod.group_view(cfg, g)
        lines, waiting = [], 0
        for link in manual_links(gcfg):
            try:
                info = parse_link(normalize_link(link, st), p2p)
                key = source_key(info["page_id"], info["part"])
                rec = st.sources.get(key) or {}
                if st.source_used(key) or rec.get("status") == "reserved":
                    lines.append(f"- ⏭ уже взята ({rec.get('status')}, {rec.get('slot', '')}): {link[:90]}")
                    continue
                post = fetch_post(graph, info, tokens, cfg.fallback_source_page_id)
                page = cfg.page(post.get("owner_page_id") or info["page_id"])
                waiting += 1
                first = " ".join(post["text"].split())[:100]
                lines.append(f"- ✅ {page.name if page else 'чужая страница'} · {len(post['text'])} знаков, "
                             f"фото есть · «{first}…»")
            except SourceError as e:
                bad += 1
                lines.append(f"- ❌ {link[:90]}: {e}")
        out.append(f"### {cfg.group_label(g)} ({len(gcfg.pages)} стр.) — ждут публикации: {waiting}")
        out += lines or ["- (очередь пуста)"]
        out.append("")
    summary("\n".join(out) + "\n", "previews/queue.md", cfg)
    return 1 if bad else 0


def cmd_sheet_status(cfg) -> int:
    """Проверка Google-таблицы: доступна ли, сколько старых строк пропущено, какие новые ссылки ждут."""
    st = state_of(cfg)
    out = ["## Google-таблица FB_QUEUE", ""]
    try:
        links = google_sheet_links(cfg)
    except Exception as e:  # noqa: BLE001
        out.append(f"❌ {e}")
        summary("\n".join(out) + "\n", "previews/sheet-status.md", cfg)
        return 1
    out.append(f"✅ Таблица читается, ссылок в колонке A: {len(links)}")
    if not st.data.get("sheet_baseline_at"):
        n = sheet_baseline(st, links, datetime.now(timezone.utc))
        out.append(f"✅ Подключено: {n} старых ссылок помечены как уже бывшие — они не публикуются. "
                   "Теперь добавляйте новые ссылки в конец колонки A.")
    else:
        out.append(f"Подключена с {st.data['sheet_baseline_at']}")
    waiting = [ln for ln in links if not st.source_used(link_key(ln))]
    out.append(f"Новых ссылок в очереди: {len(waiting)}")
    out += [f"- {ln[:100]}" for ln in waiting[:20]]
    summary("\n".join(out) + "\n", "previews/sheet-status.md", cfg)
    return 0


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
