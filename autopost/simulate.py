"""Сухой прогон: N дней расписания на поддельных Facebook/WordPress и тестовых текстах.

Проверяет: 5 слотов в день × все страницы, отсутствие дублей, первый комментарий у каждого поста,
повтор только неудавшихся шагов (внедряются сбои), повторный запуск ничего не задваивает.
"""
from __future__ import annotations

import shutil
import tempfile
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config as config_mod
from .config import LANG_ORDER
from .fakes import FakeGraph, FakeHTTP, FakeWP
from .fb import FacebookError
from .generate import Generator
from .pipeline import Pipeline
from .state import State
from .workflow import LAST_SLOT_EXTRA_MIN, OFFSETS_MIN, last_slot_of_day
from .wp import WordPressError

STORY = ("When Anna found an old letter hidden behind the kitchen cupboard, she did not expect it to change "
         "everything she believed about her family. The letter was written by her grandmother many years ago, "
         "and it told a story that nobody had ever dared to tell. ") * 4


def run_simulation(cfg, days=2):
    tmp = Path(tempfile.mkdtemp(prefix="autopost-sim-"))
    for d in ("config", "prompts"):
        shutil.copytree(cfg.path(d), tmp / d)
    (tmp / "state").mkdir()
    sim_cfg = config_mod.load(tmp)
    sim_cfg.settings["schedule_enabled"] = True
    sim_cfg.settings["sources"]["auto"]["lookback_hours"] = 24 * (days + 2)
    tz = ZoneInfo(sim_cfg.tz)
    start = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    clock = {"now": start}
    graph = FakeGraph(sim_cfg, lambda: clock["now"])
    wp = FakeWP()
    gen = Generator(sim_cfg, backend="mock")
    state = State(tmp / "state/state.json")
    state.data["source_cutoff"] = (start - timedelta(days=1)).isoformat()  # как после «2 · Подключить Facebook»
    tokens = {p.page_id: f"PT-{p.page_id}" for p in sim_cfg.pages}

    # истории, «опубликованные вручную» на ваших страницах
    pages = sim_cfg.pages
    for i in range(5 * days + 3):
        page = pages[(i * 7) % len(pages)]
        ts = (start - timedelta(hours=10) + timedelta(minutes=13 * i)).timestamp()
        graph.source_posts.setdefault(page.page_id, []).append({
            "id": f"{page.page_id}_{700000 + i}", "message": f"Story #{i}. " + STORY,
            "full_picture": f"https://scontent.example/{i}.jpg", "created_time": f"{int(ts)}",
            "attachments": {"data": [{"media_type": "photo"}]}, "_ts": int(ts)})

    # внедряем сбои
    some = [p.page_id for p in pages]
    graph.fail_next += [
        ("create_photo_post", some[3], FacebookError("Service temporarily unavailable", code=2)),
        ("comment", some[10], FacebookError("Please retry", code=1)),
    ]
    lost = FacebookError("сеть: connection reset")
    lost.after_create = True
    graph.fail_next.append(("create_photo_post", some[20], lost))
    wp.fail_next.append(("create_post", WordPressError("Bad gateway", 502)))

    ticks = []
    for d in range(days):
        day = start + timedelta(days=d)
        for s in sim_cfg.slots:
            h, m = map(int, s.split(":"))
            base = day.replace(hour=h, minute=m)
            offs = OFFSETS_MIN + (LAST_SLOT_EXTRA_MIN if s == last_slot_of_day(sim_cfg.slots) else ())
            ticks += [base + timedelta(minutes=o) for o in offs]
    ticks = sorted(set(ticks))
    log = []
    for t in ticks:
        clock["now"] = t
        for _rep in range(2):  # каждый запуск дважды — проверка идемпотентности
            r = Pipeline(sim_cfg, state, graph, wp, gen, tokens, now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
            log.append((t, r))
    # хвост: пусть пройдёт время после последнего слота
    clock["now"] = ticks[-1] + timedelta(hours=4)
    Pipeline(sim_cfg, state, graph, wp, gen, tokens, now_fn=lambda: clock["now"], http=FakeHTTP()).tick()

    # ---- проверки
    problems = []
    slots = state.slots
    expected_slots = max(days * len(sim_cfg.slots), len(slots))  # слот 00:00 следующего дня тоже готовится
    done = [k for k, s in slots.items() if s.get("status") == "done"]
    if len(done) != expected_slots:
        problems.append(f"завершено слотов {len(done)} из {expected_slots}: "
                        + str({k: s.get('status') for k, s in slots.items()}))
    per_page_ts = Counter((p["page"], p["scheduled_ts"] or p["message"][:30]) for p in graph.posts.values())
    dups = [k for k, c in per_page_ts.items() if c > 1]
    if dups:
        problems.append(f"дубли постов: {dups[:3]}")
    no_comment = [pid for pid, p in graph.posts.items() if len(graph.comments_by.get(pid, [])) != 1]
    if no_comment:
        problems.append(f"постов без ровно одного комментария: {len(no_comment)}")
    sources = [s.get("source_key") for s in slots.values()]
    if len(sources) != len(set(sources)):
        problems.append("одна история использована дважды")
    total_expected = 0
    for s in slots.values():
        if not s.get("source"):
            continue
        src_page = s["source"]["page_id"]
        total_expected += sum(1 for lg in LANG_ORDER for p in sim_cfg.pages_for(lg) if p.page_id != src_page)
    if len(graph.posts) != total_expected:
        problems.append(f"постов {len(graph.posts)}, ожидалось {total_expected}")
    wp_pub = [p for p in wp.posts.values() if p["status"] == "publish"]
    if len(wp_pub) != expected_slots * len(sim_cfg.active_langs()):
        problems.append(f"статей опубликовано {len(wp_pub)}, ожидалось {expected_slots * len(sim_cfg.active_langs())}")
    links_ok = all(graph.comments_by[pid][0]["message"].startswith("https://interes-world.com/?p=")
                   for pid in graph.posts if graph.comments_by.get(pid))
    if not links_ok:
        problems.append("ссылка в комментарии не того формата")
    on_time = sum(1 for p in graph.posts.values() if p["scheduled_ts"])
    retried = sum(1 for _t, r in log for e in r.errors)
    finals = [e for _t, r in log for e in r.final_errors]
    if finals:
        problems.append(f"окончательные ошибки: {finals[:3]}")

    md = ["## Сухой прогон (без сети, тестовые тексты)", "",
          f"Период: {days} дн. × {len(sim_cfg.slots)} слотов ({', '.join(sim_cfg.slots)}, {sim_cfg.tz}); "
          f"запусков: {len(ticks)} (каждый выполнен дважды для проверки дублей).", "",
          f"- Постов в Facebook: **{len(graph.posts)}** (из них ушли в отложенные Facebook точно ко времени: {on_time})",
          f"- Первых комментариев: **{sum(len(v) for v in graph.comments_by.values())}**",
          f"- Статей WordPress: **{len(wp.posts)}** (опубликовано: {len(wp_pub)})",
          f"- Внедрено сбоев: 4 (FB-пост, комментарий, обрыв связи после создания поста, ошибка WordPress) — "
          f"временных ошибок в журналах: {retried}, все повторены автоматически",
          "- Повторные запуски не создали ни одного дубля" if not dups else "- ❌ найдены дубли", ""]
    md += ["### Итог: ✅ всё сходится" if not problems else "### Итог: ❌ проблемы", *[f"- {p}" for p in problems]]
    shutil.rmtree(tmp, ignore_errors=True)
    return not problems, "\n".join(md) + "\n"
