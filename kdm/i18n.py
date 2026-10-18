"""Translations (lang/*.json, format of TwitchDropsMiner). English fills missing keys."""
from __future__ import annotations

import copy
import json
import logging

from .config import BASE_DIR

log = logging.getLogger("kdm.i18n")
LANG_PATH = BASE_DIR / "lang"
DEFAULT_LANG = "English"


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Translator:
    def __init__(self) -> None:
        self._langs: dict[str, dict] = {}
        for path in sorted(LANG_PATH.glob("*.json")):
            try:
                data = json.loads(path.read_text("utf-8"))
                self._langs[data["language_name"]] = data
            except Exception as e:  # pragma: no cover
                log.warning("Bad language file %s: %s", path, e)
        self._langs = dict(sorted(self._langs.items()))
        self.current_language = DEFAULT_LANG
        self.t: dict = self._langs.get(DEFAULT_LANG, {})

    def get_languages(self) -> list[str]:
        return list(self._langs)

    def set_language(self, language: str) -> None:
        if language not in self._langs:
            raise ValueError(f"Unknown language {language}")
        self.current_language = language
        self.t = _merge(self._langs.get(DEFAULT_LANG, {}), self._langs[language])


_ = Translator()
