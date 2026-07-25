"""Golden test set runner（AGENTS 7.3）。

- 比對器與資料集驗證：永遠執行（不需 LLM）。
- 實打 LLM 的通過率測試：標 golden，需 `--run-golden` 與 LLM_API_KEY；支援 `--golden-model` 對照。
逐條比對 LLM 的「第一個工具呼叫」名稱與參數子集，輸出通過率（DoD 目標 ≥95%）。
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from sitcon_bot.agent.core import ASK_USER_SPEC
from sitcon_bot.agent.prompts import PromptBuilder, PromptData
from sitcon_bot.agent.tools.drive_tools import DriveSearchTool
from sitcon_bot.agent.tools.gitlab_tools import (
    GitlabCommentIssueTool,
    GitlabCreateIssueTool,
    GitlabGetIssueTool,
    GitlabSearchIssuesTool,
    GitlabUpdateIssueTool,
)
from sitcon_bot.agent.tools.hackmd_tools import (
    HackmdCreateNoteTool,
    HackmdGetNoteTool,
    HackmdSearchNotesTool,
    HackmdUpdateNoteTool,
)
from sitcon_bot.agent.tools.people_tools import ResolvePersonTool
from sitcon_bot.services.llm.base import Message, TextBlock, build_llm_client

GOLDEN_FILE = Path(__file__).parent / "golden.yaml"

TOOL_CLASSES = [
    GitlabCreateIssueTool, GitlabUpdateIssueTool, GitlabCommentIssueTool, GitlabGetIssueTool,
    GitlabSearchIssuesTool, ResolvePersonTool, DriveSearchTool,
    HackmdCreateNoteTool, HackmdSearchNotesTool, HackmdGetNoteTool, HackmdUpdateNoteTool,
]
ALL_SPECS = [c.spec() for c in TOOL_CLASSES] + [ASK_USER_SPEC]
KNOWN_TOOLS = {s.name for s in ALL_SPECS}


def _load_cases() -> list[dict[str, Any]]:
    return yaml.safe_load(GOLDEN_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 比對器（純函式）
# --------------------------------------------------------------------------- #
def match_tool(expected: str | list[str], actual: str) -> bool:
    return actual in expected if isinstance(expected, list) else actual == expected


def match_args(args: dict[str, Any], subset: dict[str, Any] | None) -> tuple[bool, str]:
    for key, val in (subset or {}).items():
        if key.endswith("_one_of"):
            field = key[: -len("_one_of")]
            if args.get(field) not in val:
                return False, f"{field}={args.get(field)!r} 不在 {val}"
        elif key.endswith("_contains"):
            field = key[: -len("_contains")]
            actual = args.get(field) or []
            if not all(v in actual for v in val):
                return False, f"{field}={actual!r} 缺 {val}"
        elif key.endswith("_present"):
            field = key[: -len("_present")]
            if bool(args.get(field)) != bool(val):
                return False, f"{field} present={bool(args.get(field))} 期望 {val}"
        elif args.get(key) != val:
            return False, f"{key}={args.get(key)!r} != {val!r}"
    return True, ""


# --------------------------------------------------------------------------- #
# 資料集與比對器驗證（永遠執行）
# --------------------------------------------------------------------------- #
def test_golden_dataset_valid() -> None:
    cases = _load_cases()
    assert len(cases) >= 25
    ids: set[str] = set()
    for c in cases:
        assert c["id"] not in ids, f"重複 id {c['id']}"
        ids.add(c["id"])
        assert c["input"].strip()
        exp = c["expect"]
        tools = exp["tool"] if isinstance(exp["tool"], list) else [exp["tool"]]
        for t in tools:
            assert t in KNOWN_TOOLS, f"{c['id']}: 未知工具 {t}"


def test_match_tool() -> None:
    assert match_tool("drive_search", "drive_search")
    assert match_tool(["a", "b"], "b")
    assert not match_tool(["a", "b"], "c")


def test_match_args_operators() -> None:
    args = {"iid": 42, "team": "開發組", "labels": ["Status::Doing"], "title": "x"}
    assert match_args(args, {"iid": 42})[0]
    assert match_args(args, {"team_one_of": ["開發", "開發組"]})[0]
    assert match_args(args, {"labels_contains": ["Status::Doing"]})[0]
    assert match_args(args, {"title_present": True})[0]
    assert not match_args(args, {"iid": 99})[0]
    assert not match_args(args, {"labels_contains": ["Status::Inbox"]})[0]
    assert not match_args({}, {"title_present": True})[0]


# --------------------------------------------------------------------------- #
# 實打 LLM 通過率（golden，需 --run-golden）
# --------------------------------------------------------------------------- #
async def _prompt_data() -> PromptData:
    teams = ["場務", "活動", "總召", "紀錄", "編輯", "行銷", "行政", "製播", "議程", "設計", "財務", "開發"]
    labels = [f"Team::{t}組" for t in teams] + [
        "Status::Inbox", "Status::Doing", "Status::Review", "Status::To Do", "Status::Waiting",
        "0913 一籌", "0110 站立會議",
    ]
    roster = [
        {"nickname": "Yuan", "gitlab_username": "yuan_tw", "gitlab_id": 1, "telegram_username": "yuan",
         "telegram_id": 100, "role": "開發組", "position": "組長", "other_role": None},
        {"nickname": "Leaf", "gitlab_username": "leaf", "gitlab_id": 2, "telegram_username": "leaf",
         "telegram_id": 101, "role": "行政組", "position": "組長", "other_role": None},
        {"nickname": "Amy", "gitlab_username": "amy", "gitlab_id": 3, "telegram_username": "amy",
         "telegram_id": 102, "role": None, "position": "總召", "other_role": None},
    ]
    charter = (
        "## 開發組\n官網、報名系統、內部工具與資訊基礎設施的開發維運。\n"
        "## 設計組\n主視覺、宣傳圖像與網站視覺設計。\n"
        "## 行政組\n庶務、行文、保證金匯款、餐飲。\n"
        "## 議程組\n徵稿審稿、講者聯繫。\n"
    )
    return PromptData(labels=labels, roster_rows=roster, charter=charter)


class _Secret:
    def __init__(self, v: str) -> None:
        self._v = v

    def get_secret_value(self) -> str:
        return self._v


@pytest.mark.golden
async def test_golden_pass_rate(request: pytest.FixtureRequest) -> None:
    api_key = os.environ.get("LLM_API_KEY")
    if not api_key:
        pytest.skip("需設定 LLM_API_KEY 才能實打 golden test")

    model = request.config.getoption("--golden-model") or os.environ.get("LLM_MODEL", "claude-sonnet-4-6")
    settings = SimpleNamespace(
        llm_provider=os.environ.get("LLM_PROVIDER", "anthropic"),
        llm_api_key=_Secret(api_key),
        llm_model=model,
        llm_base_url=os.environ.get("LLM_BASE_URL", ""),
    )
    llm = build_llm_client(settings)
    thinking = os.environ.get("LLM_THINKING", "high")
    system = await PromptBuilder(_prompt_data).build()
    cases = _load_cases()

    passed = 0
    failures: list[tuple[str, str]] = []
    for c in cases:
        resp = await llm.chat(
            system=system, messages=[Message("user", [TextBlock(c["input"])])], tools=ALL_SPECS, thinking=thinking
        )
        if not resp.tool_calls:
            failures.append((c["id"], "無工具呼叫"))
            continue
        tc = resp.tool_calls[0]
        exp = c["expect"]
        if not match_tool(exp["tool"], tc.name):
            failures.append((c["id"], f"工具 {tc.name} != {exp['tool']}"))
            continue
        ok, reason = match_args(tc.arguments, exp.get("args_subset"))
        if not ok:
            failures.append((c["id"], reason))
            continue
        passed += 1

    rate = passed / len(cases)
    print(f"\nGolden [{model}]：{passed}/{len(cases)} = {rate:.0%}")
    for fid, reason in failures:
        print(f"  ✗ {fid}: {reason}")
    threshold = float(os.environ.get("GOLDEN_THRESHOLD", "0.95"))
    assert rate >= threshold, f"通過率 {rate:.0%} < 門檻 {threshold:.0%}"
