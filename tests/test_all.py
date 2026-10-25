"""Проверки проекта. Запуск: python -m unittest discover -s tests -v"""
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from autopost import config as config_mod
from autopost.cli import cmd_private_test, cmd_public_test
from autopost.fakes import FakeGraph, FakeHTTP, FakeWP
from autopost.fb import FacebookError, Graph
from autopost.generate import Generator, detect_language, sanitize_article, validate
from autopost.pipeline import Pipeline
from autopost.simulate import STORY, run_simulation
from autopost.sources import SourceError, parse_link
from autopost.state import State
from autopost.tokens import TokenError, load_tokens, save_tokens
from autopost.workflow import workflow_in_sync

ROOT = Path(__file__).resolve().parent.parent

# Точные назначения из Make blueprint (модули 100–403): язык, page_id, название.
MAKE_PAGES = [
    ("FR", "1202025962997153", "Au Cœur des Vies"), ("FR", "1250374694822508", "Le Fil de la Vie"),
    ("FR", "1079295351933311", "Destins Touchants"), ("FR", "1145306638657445", "Larmes de Vie"),
    ("FR", "1032441206625600", "La Vie Telle Qu’elle Est"), ("FR", "1011074408765807", "Émotions Virales | Paris"),
    ("FR", "106591088255968", "Soleil et Amour"),
    ("IT", "1142735408923607", "Quand Tout Bascule"), ("IT", "1160223747182678", "Vite in Primo Piano"),
    ("IT", "997772916762884", "Storie Vere | Lion"), ("IT", "1079964265201139", "Voci di Vita | Paris"),
    ("IT", "1064982806700854", "Racconti Quotidiani | Monaco"), ("IT", "1140835409106424", "Vita Virale | Paris"),
    ("IT", "1058598924006917", "Moments Français | Paris"), ("IT", "1114015015119414", "Frammenti di Vita"),
    ("IT", "1030877436782212", "Una Storia che Ti Toccherà"),
    ("EN", "1235940142932696", "The Daily Report"), ("EN", "1167352663135230", "Human Life Stories"),
    ("EN", "1165836896614851", "Behind Her Smile"), ("EN", "914823471720372", "Life’s Echo"),
    ("EN", "921611111039858", "Beyond the Mask"), ("EN", "1016419758218222", "Whispers of Fate"),
    ("EN", "975714512286925", "Heartfelt Lessons"), ("EN", "236129656261330", "The Golden Touch"),
    ("EN", "104239205742669", "Amazing"), ("EN", "220697507802568", "Pet Lover"),
    ("EN", "117220714573166", "Best Stories"), ("EN", "162144310308183", "Sweet Paws #1"),
    ("EN", "108881571809093", "Sweet Paws #2"), ("EN", "237939756073256", "Love This World"),
    ("EN", "1164984823367955", "Fates Behind Closed Doors"), ("EN", "621364207735841", "Beautiful Babies"),
    ("NL", "633031599904551", "Het is interessant"), ("NL", "715935674934966", "Mooie kinderen"),
]


def temp_cfg():
    tmp = Path(tempfile.mkdtemp(prefix="autopost-test-"))
    for d in ("config", "prompts", ".github"):
        shutil.copytree(ROOT / d, tmp / d)
    (tmp / "state").mkdir()
    cfg = config_mod.load(tmp)
    cfg.settings["slots"] = ["09:00", "12:00", "15:00", "18:00", "21:00"]  # фиксированные часы для тестов
    # тесты конвейера моделируют авто-режим без сети (таблица проверяется отдельным тестом)
    cfg.settings["sources"]["auto"]["enabled"] = True
    cfg.settings["sources"]["google_sheet"]["enabled"] = False
    return cfg, tmp


class ConfigTests(unittest.TestCase):
    def test_exact_page_assignments(self):
        cfg = config_mod.load(ROOT)
        got = [(p.lang, p.page_id, p.name) for p in cfg.pages if p.from_make]
        self.assertEqual(got, MAKE_PAGES)
        # + страницы, добавленные после переноса (RU, TR, DE и новые EN)
        self.assertGreaterEqual(len(cfg.pages), 34)
        self.assertEqual(set(cfg.active_langs()), {"FR", "IT", "EN", "NL", "RU", "TR", "DE"})
        self.assertEqual(config_mod.check_expected_counts(cfg), [])

    def test_workflow_matches_slots(self):
        self.assertTrue(workflow_in_sync(config_mod.load(ROOT)),
                        "расписание в autopost.yml устарело — выполните python -m autopost render-workflow")


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.map = config_mod.load(ROOT).profile_to_page()

    def test_photo_link_with_profile_id(self):
        r = parse_link("https://www.facebook.com/photo.php?fbid=122100&set=pb.61592252135792.-2207520000&type=3",
                       self.map)
        self.assertEqual(r["page_id"], "1235940142932696")  # The Daily Report
        self.assertEqual(r["part"], "122100")

    def test_permalink(self):
        r = parse_link("https://www.facebook.com/permalink.php?story_fbid=555&id=61577900372290", self.map)
        self.assertEqual((r["page_id"], r["part"]), ("633031599904551", "555"))

    def test_posts_and_raw(self):
        self.assertEqual(parse_link("https://facebook.com/1202025962997153/posts/777?x=1", self.map)["part"], "777")
        self.assertEqual(parse_link("1202025962997153_888", self.map)["candidates"][0], "1202025962997153_888")

    def test_short_link_rejected(self):
        with self.assertRaises(SourceError):
            parse_link("https://www.facebook.com/share/p/1ABCdef/", self.map)


class TextTests(unittest.TestCase):
    def test_validation(self):
        src = "word " * 200
        article = "<p>" + "Elle ne savait pas que ce jour-là allait changer sa vie. " * 60 + "</p>"
        fb_ok = "Elle ne savait pas que la vie est belle et que tout change. " * 16
        errs, _ = validate("FR", src, "Titre", fb_ok, article)
        self.assertEqual(errs, [])
        errs, _ = validate("FR", src, "Titre", fb_ok + " https://x.y", article)
        self.assertTrue(any("ссылка" in e for e in errs))
        errs, _ = validate("IT", src, "Titolo", fb_ok, article)
        self.assertTrue(any("язык" in e for e in errs))

    def test_sanitize(self):
        out = sanitize_article('<div><p>Hi <a href="http://x">there</a></p><img src="x"><script>x</script></div>')
        self.assertEqual(out, "<p>Hi there</p>")

    def test_detect(self):
        self.assertEqual(detect_language("Lei non sapeva che quel giorno le avrebbe cambiato la vita"), "IT")
        self.assertEqual(detect_language("Ze wist niet dat die dag haar leven zou veranderen"), "NL")


class TokenTests(unittest.TestCase):
    def test_roundtrip_and_wrong_secret(self):
        cfg, _ = temp_cfg()
        save_tokens(cfg, "s3cret", {"1": "tok"}, {})
        self.assertEqual(load_tokens(cfg, "s3cret"), {"1": "tok"})
        self.assertNotIn(b"tok", cfg.path("state/page_tokens.enc").read_bytes())
        with self.assertRaises(TokenError):
            load_tokens(cfg, "other")


class GraphClientTests(unittest.TestCase):
    class Sess:
        def __init__(self, responses):
            self.responses, self.n = list(responses), 0

        def _next(self, *a, **k):
            self.n += 1
            code, body = self.responses.pop(0)
            return SimpleNamespace(status_code=code, json=lambda: body, text=json.dumps(body))

        get = post = delete = _next

    def test_error_parsing_and_retry_rules(self):
        s = self.Sess([(500, {"error": {"message": "x", "code": 2}}), (200, {"id": "1"})])
        g = Graph(session=s, backoff=0)
        self.assertEqual(g.get("me", "t"), {"id": "1"})  # GET повторяется
        s = self.Sess([(500, {"error": {"message": "x", "code": 2}}), (200, {"id": "1"})])
        g = Graph(session=s, backoff=0)
        with self.assertRaises(FacebookError):
            g.post("p/photos", "t")  # POST не повторяется вслепую
        self.assertEqual(s.n, 1)
        s = self.Sess([(400, {"error": {"message": "expired", "code": 190}})])
        with self.assertRaises(FacebookError) as cm:
            Graph(session=s, backoff=0).get("me", "t")
        self.assertTrue(cm.exception.auth)


class PipelineTests(unittest.TestCase):
    def test_simulation_one_day(self):
        ok, md = run_simulation(config_mod.load(ROOT), days=1)
        self.assertTrue(ok, md)

    def _setup(self, enabled_at):
        cfg, tmp = temp_cfg()
        cfg.settings["schedule_enabled"] = True
        tz = ZoneInfo(cfg.tz)
        clock = {"now": enabled_at.replace(tzinfo=tz)}
        graph, wp = FakeGraph(cfg, lambda: clock["now"]), FakeWP()
        st = State(tmp / "state/state.json")
        st.data["source_cutoff"] = (clock["now"] - timedelta(days=1)).isoformat()
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        page = cfg.pages[0]
        graph.source_posts[page.page_id] = [{
            "id": f"{page.page_id}_42", "message": STORY, "full_picture": "https://scontent.example/x.jpg",
            "attachments": {"data": [{"media_type": "photo"}]}, "_ts": int(clock["now"].timestamp()) - 3600}]

        def run():
            return Pipeline(cfg, st, graph, wp, Generator(cfg, backend="mock"), tokens,
                            now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        return cfg, clock, graph, wp, st, run

    def test_late_slot_published_immediately_with_comment(self):
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 8, 50))
        run()  # 08:50 — подготовка успевает, но ставить в отложенные уже поздно (< 15 мин)
        self.assertEqual(len(graph.posts), 0)
        clock["now"] = clock["now"].replace(hour=9, minute=3)
        rep = run()
        self.assertEqual(rep.final_errors, [])
        self.assertEqual(len(graph.posts), len(cfg.pages) - 1)
        self.assertTrue(all(not p["scheduled_ts"] for p in graph.posts.values()))
        self.assertEqual(sum(len(v) for v in graph.comments_by.values()), len(cfg.pages) - 1)
        self.assertEqual(st.slots["2030-05-01 09:00"]["status"], "done")

    def test_no_source_is_not_fatal(self):
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 6, 0))
        graph.source_posts.clear()
        clock["now"] = clock["now"].replace(hour=6, minute=3)
        rep = run()
        self.assertEqual(rep.final_errors, [])
        self.assertEqual(st.slots["2030-05-01 09:00"]["status"], "no_source")
        self.assertEqual(len(graph.posts), 0)

    def test_disabled_schedule_does_nothing(self):
        cfg, clock, graph, wp, st, run = self._setup(datetime(2030, 5, 1, 6, 0))
        cfg.settings["schedule_enabled"] = False
        run()
        self.assertEqual((len(graph.posts), len(wp.posts)), (0, 0))


class ResilienceTests(PipelineTests):
    """Сценарии из независимой проверки: потеря состояния, «прошедшие» ошибки, нет токена, утечки."""

    def _slot_run(self, start=datetime(2030, 5, 1, 6, 7)):
        cfg, clock, graph, wp, st, run = self._setup(start)
        return cfg, clock, graph, wp, st, run

    def test_state_loss_does_not_duplicate(self):
        import copy
        cfg, clock, graph, wp, st, run = self._slot_run()
        before = copy.deepcopy(st.data)
        run()                                     # 06:07 — подготовка и отложенные посты на 09:00
        self.assertEqual((len(graph.posts), len(wp.posts)), (len(cfg.pages) - 1, len(cfg.active_langs())))
        st.data = before                          # «потеряли» state.json (тексты в content/ остались)
        clock["now"] = clock["now"].replace(hour=8, minute=7)
        run()
        self.assertEqual((len(graph.posts), len(wp.posts)), (len(cfg.pages) - 1, len(cfg.active_langs())))
        st.data = copy.deepcopy(before)           # и ещё раз, уже после выхода
        st.data["source_cutoff"] = before["source_cutoff"]
        clock["now"] = clock["now"].replace(hour=9, minute=7)
        run()
        self.assertEqual((len(graph.posts), len(wp.posts)), (len(cfg.pages) - 1, len(cfg.active_langs())))
        self.assertEqual(sum(len(v) for v in graph.comments_by.values()), len(cfg.pages) - 1)

    def test_comment_error_that_went_through(self):
        cfg, clock, graph, wp, st, run = self._slot_run()
        run()
        target_page = cfg.pages_for("IT")[0].page_id
        err = FacebookError("An unexpected error has occurred", code=2)
        err.after_create = True
        graph.fail_next.append(("comment", target_page, err))
        clock["now"] = clock["now"].replace(hour=9, minute=7)
        run()
        clock["now"] = clock["now"].replace(hour=9, minute=37)
        run()
        self.assertTrue(all(len(graph.comments_by.get(pid, [])) == 1 for pid in graph.posts))

    def test_wp_lookup_failure_does_not_create(self):
        from autopost.wp import WordPressError
        cfg, clock, graph, wp, st, run = self._slot_run()
        wp.fail_next += [("find_by_slug", WordPressError("timeout", None))] * len(cfg.active_langs())
        run()
        self.assertEqual(len(wp.posts), 0)
        clock["now"] = clock["now"].replace(hour=8, minute=7)
        run()
        self.assertEqual(len(wp.posts), len(cfg.active_langs()))

    def test_wp_502_after_save_no_duplicate(self):
        from autopost.wp import WordPressError
        cfg, clock, graph, wp, st, run = self._slot_run()
        err = WordPressError("Bad gateway", 502)
        err.after_create = True
        wp.fail_next.append(("create_post", err))
        run()
        clock["now"] = clock["now"].replace(hour=8, minute=7)
        run()
        self.assertEqual(len(wp.posts), len(cfg.active_langs()))

    def test_one_page_without_token_does_not_stop_others(self):
        cfg, clock, graph, wp, st, run = self._slot_run()
        missing = cfg.pages_for("EN")[3].page_id
        run.__closure__  # noqa: B018
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages if p.page_id != missing}
        rep = Pipeline(cfg, st, graph, wp, Generator(cfg, backend="mock"), tokens,
                       now_fn=lambda: clock["now"], http=FakeHTTP()).tick()
        self.assertEqual(len(graph.posts), len(cfg.pages) - 2)
        self.assertEqual(rep.final_errors, [])

    def test_unconnected_pages_are_skipped_quietly(self):
        """11 страниц ещё не подключены: остальные публикуются, слот закрывается без ошибок и красных запусков."""
        cfg, clock, graph, wp, st, run = self._slot_run()
        missing = {p.page_id for p in cfg.pages[1::3]}  # pages[0] — источник истории
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages if p.page_id not in missing}
        reports = []
        for hh, mm in ((6, 7), (8, 7), (9, 7), (9, 37), (10, 7), (11, 7)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            reports.append(Pipeline(cfg, st, graph, wp, Generator(cfg, backend="mock"), tokens,
                                    now_fn=lambda: clock["now"], http=FakeHTTP()).tick())
        self.assertEqual([e for r in reports for e in r.final_errors], [])
        # предупреждения только о пустой очереди следующих слотов (в тесте одна история), не о страницах
        self.assertEqual([e for r in reports for e in r.errors if "истори" not in e], [])
        self.assertTrue(all(graph.posts[pid]["page"] not in missing for pid in graph.posts))
        self.assertEqual(sum(len(v) for v in graph.comments_by.values()), len(graph.posts))
        self.assertEqual(st.slots["2030-05-01 09:00"]["status"], "done")

    def test_language_without_connected_pages_is_skipped(self):
        """Обе страницы NL не подключены: NL-текст и NL-статья не создаются, остальные языки выходят."""
        cfg, clock, graph, wp, st, run = self._slot_run()
        nl = {p.page_id for p in cfg.pages_for("NL")}
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages if p.page_id not in nl}
        reports = []
        for hh, mm in ((6, 7), (8, 7), (9, 7), (9, 37), (10, 7), (11, 7)):
            clock["now"] = clock["now"].replace(hour=hh, minute=mm)
            reports.append(Pipeline(cfg, st, graph, wp, Generator(cfg, backend="mock"), tokens,
                                    now_fn=lambda: clock["now"], http=FakeHTTP()).tick())
        self.assertEqual(len(wp.posts), len(cfg.active_langs()) - 1)
        self.assertEqual([e for r in reports for e in r.final_errors], [])
        slot = st.slots["2030-05-01 09:00"]
        self.assertEqual(slot["status"], "done")
        self.assertTrue(slot["langs"]["NL"].get("skipped"))
        self.assertEqual(len(graph.posts), len(cfg.pages) - 1 - len(nl))

    def test_tokens_are_redacted(self):
        from autopost.fb import redact
        e = FacebookError("сеть: https://graph.facebook.com/v26.0/me?access_token=EAAB123&x=1 client_secret=abc")
        self.assertNotIn("EAAB123", str(e))
        self.assertNotIn("abc", redact("client_secret=abc"))


class TestCommands(unittest.TestCase):
    def test_private_then_public(self):
        cfg, tmp = temp_cfg()
        clock = lambda: datetime.now(ZoneInfo(cfg.tz))  # noqa: E731
        graph, wp, http = FakeGraph(cfg, clock), FakeWP(), FakeHTTP()
        page = cfg.pages[5]
        graph.source_posts[page.page_id] = [{
            "id": f"{page.page_id}_77", "message": STORY, "full_picture": "https://scontent.example/x.jpg",
            "attachments": {"data": [{"media_type": "photo"}]}, "_ts": int(clock().timestamp()) - 600}]
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        args = SimpleNamespace(source="", page="", keep=False, backend="mock")
        self.assertEqual(cmd_private_test(cfg, args, services=(graph, wp, http), tokens=tokens), 0)
        self.assertEqual(len(graph.posts), 0, "закрытый тест не должен оставлять постов")
        self.assertEqual(len(wp.posts), 0, "черновик удалён")
        preview = sorted(cfg.path("previews").glob("test-*.md"))[-1].read_text(encoding="utf-8")
        self.assertIn("Het is interessant", preview)
        args = SimpleNamespace(test_id="", page="", confirm="no")
        self.assertEqual(cmd_public_test(cfg, args, services=(graph, wp, http), tokens=tokens), 1)
        args.confirm = "PUBLISH"
        self.assertEqual(cmd_public_test(cfg, args, services=(graph, wp, http), tokens=tokens), 0)
        self.assertEqual(len(graph.posts), 1)
        (pid, post), = graph.posts.items()
        self.assertEqual(post["page"], cfg.test_page_id())
        self.assertTrue(graph.comments_by[pid][0]["message"].startswith("https://interes-world.com/?p="))
        st = State(cfg.path("state/state.json"))
        self.assertEqual(st.sources[f"post:{page.page_id}_77"]["status"], "used")


if __name__ == "__main__":
    unittest.main()


class SheetQueueTests(unittest.TestCase):
    """Как watchRows в Make: старые строки таблицы не публикуются, новые берутся по порядку, каждая один раз."""

    def test_baseline_then_new_rows(self):
        from unittest import mock
        from autopost import sources
        cfg, tmp = temp_cfg()
        cfg.settings["sources"]["auto"]["enabled"] = False
        cfg.settings["sources"]["google_sheet"]["enabled"] = True
        now = datetime(2030, 5, 1, 6, 0, tzinfo=ZoneInfo(cfg.tz))
        graph = FakeGraph(cfg, lambda: now)
        tokens = {p.page_id: f"PT-{p.page_id}" for p in cfg.pages}
        pid = cfg.pages[0].page_id
        for n in (1, 2, 3):
            graph.source_posts.setdefault(pid, []).append({
                "id": f"{pid}_{n}", "message": STORY + f" #{n}", "full_picture": "https://x/y.jpg",
                "attachments": {"data": [{"media_type": "photo"}]}})
        st = State(tmp / "state" / "state.json")
        rows = [f"{pid}_1"]                       # старая ссылка, была в таблице до подключения
        with mock.patch.object(sources, "google_sheet_links", lambda cfg: list(rows)):
            self.assertIsNone(sources.pick_source(cfg, graph, tokens, st, now, print))
            rows += [f"{pid}_2", f"{pid}_3"]      # новые строки
            first = sources.pick_source(cfg, graph, tokens, st, now, print)
            self.assertEqual(first["key"], f"post:{pid}_2")
            st.mark_source(first["key"], status="used")
            second = sources.pick_source(cfg, graph, tokens, st, now, print)
            self.assertEqual(second["key"], f"post:{pid}_3")
            st.mark_source(second["key"], status="used")
            self.assertIsNone(sources.pick_source(cfg, graph, tokens, st, now, print))
