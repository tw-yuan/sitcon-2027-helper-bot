"""工具定義與 schema 驗證（NFR-5 程式層防線）。

每個工具以 pydantic model 定義參數；LLM 產生的參數一律先經 model_validate 驗證才執行。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel

from ...services.llm.base import ToolSpec


@dataclass(slots=True)
class ToolContext:
    """單次觸發互動的執行脈絡（觸發者資訊，供 attribution 與稽核）。"""

    chat_id: int
    thread_id: int | None
    user_id: int
    username: str | None
    text: str


def _strip_titles(schema: dict[str, Any]) -> dict[str, Any]:
    """移除 pydantic 產生的 title 欄位，讓 tool schema 更精簡。"""
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            prop.pop("title", None)
    return schema


class Tool(ABC):
    """工具基底。子類宣告 name/description/args_model 並實作 run。"""

    name: ClassVar[str]
    description: ClassVar[str]
    args_model: ClassVar[type[BaseModel]]

    @classmethod
    def spec(cls) -> ToolSpec:
        return ToolSpec(
            name=cls.name,
            description=cls.description,
            input_schema=_strip_titles(cls.args_model.model_json_schema()),
        )

    @abstractmethod
    async def run(self, args: BaseModel, ctx: ToolContext) -> str:
        """執行工具，回傳給 LLM 的結果字串。"""


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools: dict[str, Tool] = {t.name: t for t in tools}

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)
