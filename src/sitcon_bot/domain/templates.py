"""會議模板載入與變數替換（HM-5）。

模板為 repo 內 markdown（config/templates/），支援變數 {{title}}、{{date}}、{{team}}、
{{meeting_type}}。客戶可自行修改，`/reload` 後重載。
"""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

TEMPLATE_DIR = Path("config/templates")
TEMPLATE_FILES = {"summit": "meeting_summit.md", "team": "meeting_team.md"}
_VARS = ("title", "date", "team", "meeting_type", "location")


class TemplateStore:
    def __init__(self, templates: dict[str, str]) -> None:
        self._templates = templates

    def reload(self, base_dir: Path = TEMPLATE_DIR) -> None:
        """重新讀取模板檔（/reload 後生效，HM-5）。"""
        self._templates = load_template_store(base_dir)._templates

    def render(
        self,
        kind: str,
        *,
        title: str = "",
        date: str = "",
        team: str = "",
        meeting_type: str = "",
        location: str = "",
    ) -> str:
        text = self._templates.get(kind, "")
        values = {"title": title, "date": date, "team": team, "meeting_type": meeting_type, "location": location}
        for var in _VARS:
            text = text.replace(f"{{{{{var}}}}}", values[var])
        return text


def load_template_store(base_dir: Path = TEMPLATE_DIR) -> TemplateStore:
    templates: dict[str, str] = {}
    for kind, fname in TEMPLATE_FILES.items():
        path = base_dir / fname
        try:
            templates[kind] = path.read_text(encoding="utf-8") if path.exists() else ""
        except OSError:
            log.warning("讀取模板 %s 失敗", path, exc_info=True)
            templates[kind] = ""
    return TemplateStore(templates)
