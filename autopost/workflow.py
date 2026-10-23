"""Расписание запусков GitHub Actions строится из часов публикаций (settings.yaml → slots).

Для каждого слота S: S−2:53 (подготовка), S−0:53 (повтор подготовки), S+0:07 (комментарии),
S+0:37 (повтор комментариев); после последнего слота дня ещё S+1:07 и S+2:07 (догоняющие).
Минуты сдвинуты от :00, когда GitHub перегружен. Cron в UTC — пересчитываем из часового пояса
(в Asia/Yerevan нет перехода на летнее время, поэтому пересчёт постоянный).
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

OFFSETS_MIN = (-173, -53, 7, 37)
LAST_SLOT_EXTRA_MIN = (67, 127)
START, END = "# >>> cron (генерируется: python -m autopost render-workflow)", "# <<< cron"
WORKFLOW = ".github/workflows/autopost.yml"


def tick_times_utc(cfg) -> list:
    tz = ZoneInfo(cfg.tz)
    ref = datetime(2026, 1, 15, tzinfo=tz)
    out = set()
    for s in cfg.slots:
        h, m = map(int, s.split(":"))
        base = ref.replace(hour=h, minute=m)
        offs = OFFSETS_MIN + (LAST_SLOT_EXTRA_MIN if s == max(cfg.slots) else ())
        for off in offs:
            t = (base + timedelta(minutes=off)).astimezone(ZoneInfo("UTC"))
            out.add((t.hour, t.minute))
    return sorted(out)


def render_cron_block(cfg, indent="    ") -> str:
    by_min = defaultdict(list)
    for h, m in tick_times_utc(cfg):
        by_min[m].append(h)
    lines = [f"{indent}{START}",
             f"{indent}# Часы публикаций ({cfg.tz}): {', '.join(cfg.slots)}"]
    for m in sorted(by_min):
        hours = ",".join(str(h) for h in sorted(set(by_min[m])))
        lines.append(f'{indent}- cron: "{m} {hours} * * *"')
    lines.append(f"{indent}{END}")
    return "\n".join(lines)


def write_workflow(cfg) -> str:
    p = cfg.path(WORKFLOW)
    text = p.read_text(encoding="utf-8")
    pat = re.compile(rf"^[ \t]*{re.escape(START)}.*?{re.escape(END)}[ \t]*$", re.S | re.M)
    if not pat.search(text):
        raise RuntimeError(f"в {WORKFLOW} нет блока расписания")
    new = pat.sub(lambda _m: render_cron_block(cfg), text)
    p.write_text(new, encoding="utf-8")
    return str(p)


def workflow_in_sync(cfg) -> bool:
    text = cfg.path(WORKFLOW).read_text(encoding="utf-8")
    return render_cron_block(cfg) in text
