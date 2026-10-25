"""Загрузка и проверка конфигурации (config/settings.yaml, config/pages.yaml)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
LANG_ORDER = ["FR", "IT", "EN", "NL", "RU", "TR", "DE"]
LANG_NAMES = {"FR": "French", "IT": "Italian", "EN": "English", "NL": "Dutch",
              "RU": "Russian", "TR": "Turkish", "DE": "German"}
# Страницы, перенесённые из Make (у них есть make_post_module), — проверка точного переноса.
EXPECTED_COUNTS = {"FR": 7, "IT": 9, "EN": 16, "NL": 2}


class ConfigError(Exception):
    pass


@dataclass
class Page:
    page_id: str
    name: str
    lang: str
    profile_id: str = ""
    from_make: bool = False


@dataclass
class Config:
    root: Path
    settings: dict
    languages: dict  # lang -> {"name": str, "pages": [Page]}
    fallback_source_page_id: str = ""
    pages: list = field(default_factory=list)

    # --- удобные свойства -------------------------------------------------
    @property
    def tz(self) -> str:
        return self.settings.get("timezone", "Asia/Yerevan")

    @property
    def slots(self) -> list:
        return list(self.settings.get("slots") or [])

    @property
    def timing(self) -> dict:
        return self.settings.get("timing") or {}

    @property
    def site(self) -> dict:
        return self.settings.get("site") or {}

    @property
    def claude(self) -> dict:
        return self.settings.get("claude") or {}

    @property
    def sources(self) -> dict:
        return self.settings.get("sources") or {}

    @property
    def graph_version(self) -> str:
        return (self.settings.get("facebook") or {}).get("graph_version", "v26.0")

    def lang_name(self, lang: str) -> str:
        return self.languages[lang]["name"]

    def pages_for(self, lang: str) -> list:
        return list(self.languages[lang]["pages"])

    def active_langs(self) -> list:
        """Языки, у которых есть хотя бы одна страница."""
        return [lg for lg in LANG_ORDER if self.languages[lg]["pages"]]

    def page(self, page_id: str) -> Page | None:
        for p in self.pages:
            if p.page_id == str(page_id):
                return p
        return None

    def profile_to_page(self) -> dict:
        return {p.profile_id: p.page_id for p in self.pages if p.profile_id}

    def test_page_id(self) -> str:
        tp = str(self.settings.get("test_page_id") or "").strip()
        if tp:
            return tp
        return self.pages_for("NL")[0].page_id

    def path(self, rel: str) -> Path:
        return self.root / rel


def _validate_slots(slots: list) -> None:
    if not slots:
        raise ConfigError("settings.yaml: список slots пуст")
    seen = set()
    for s in slots:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(s)):
            raise ConfigError(f"settings.yaml: неверное время слота '{s}', нужно ЧЧ:ММ")
        if s in seen:
            raise ConfigError(f"settings.yaml: слот '{s}' повторяется")
        seen.add(s)


def load(root: str | Path | None = None) -> Config:
    root = Path(root or os.environ.get("AUTOPOST_ROOT") or ROOT)
    settings = yaml.safe_load((root / "config/settings.yaml").read_text(encoding="utf-8")) or {}
    pages_doc = yaml.safe_load((root / "config/pages.yaml").read_text(encoding="utf-8")) or {}

    languages = {}
    all_pages = []
    for lang in LANG_ORDER:
        block = (pages_doc.get("languages") or {}).get(lang) or {"name": LANG_NAMES[lang], "pages": []}
        plist = []
        for item in block.get("pages") or []:
            p = Page(page_id=str(item["page_id"]), name=str(item["name"]), lang=lang,
                     profile_id=str(item.get("profile_id") or ""), from_make=bool(item.get("make_post_module")))
            plist.append(p)
            all_pages.append(p)
        languages[lang] = {"name": block.get("name", lang), "pages": plist}

    ids = [p.page_id for p in all_pages]
    if len(ids) != len(set(ids)):
        raise ConfigError("pages.yaml: одна и та же страница указана дважды")
    _validate_slots(settings.get("slots") or [])
    sorted_slots = sorted(settings["slots"])
    settings["slots"] = sorted_slots

    cfg = Config(root=root, settings=settings, languages=languages,
                 fallback_source_page_id=str(pages_doc.get("fallback_source_page_id") or ""),
                 pages=all_pages)
    if cfg.page(cfg.test_page_id()) is None:
        raise ConfigError(f"settings.yaml: test_page_id {cfg.test_page_id()} нет среди страниц pages.yaml")
    return cfg


def check_expected_counts(cfg: Config) -> list:
    """Возвращает список расхождений с исходным файлом Make (пусто — всё совпадает)."""
    problems = []
    for lang, n in EXPECTED_COUNTS.items():
        got = sum(1 for p in cfg.pages_for(lang) if p.from_make)
        if got != n:
            problems.append(f"{lang}: страниц из Make {got}, в Make было {n}")
    made = sum(1 for p in cfg.pages if p.from_make)
    if made != 34:
        problems.append(f"страниц из Make {made}, в Make было 34")
    return problems


def describe(cfg: Config) -> str:
    """Например: «54 страницы: FR 7 · IT 9 · EN 29 · NL 2 · RU 3 · TR 2 · DE 2»."""
    parts = " · ".join(f"{lg} {len(cfg.pages_for(lg))}" for lg in cfg.active_langs())
    return f"{len(cfg.pages)} стр.: {parts}"
