"""併發模型：KeyedLock、gateway 的同群序列／跨群並行、四個快取的 single-flight。

行為契約：
  - 同一對話（chat + topic）的 agent 回合一次只跑一個，回覆順序不亂；
  - 不同群／不同 topic 完全並行，一則長查詢不會擋住其他群；
  - 全域 agent 回合上限確實生效；
  - 快取冷掉時併發請求只打一次外部 API。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from sitcon_bot.concurrency import KeyedLock
from sitcon_bot.services.drive_client import DriveSearchService
from sitcon_bot.services.gitlab_client import GitLabClient
from sitcon_bot.services.photo_index import PhotoIndexService
from sitcon_bot.services.sheets_roster import RosterService
from sitcon_bot.settings import Settings
from sitcon_bot.telegram.gateway import BusinessRequest, BusinessResult, Gateway

TIMEOUT = 5.0  # 併發測試若真的被序列化會卡住 → 以逾時讓它失敗而不是掛住


# ------------------------------------------------------------------ #
# KeyedLock
# ------------------------------------------------------------------ #
async def test_keyed_lock_same_key_serializes() -> None:
    lock = KeyedLock()
    peak = current = 0

    async def worker() -> None:
        nonlocal peak, current
        async with lock("k"):
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.01)
            current -= 1

    await asyncio.gather(*(worker() for _ in range(4)))
    assert peak == 1


async def test_keyed_lock_different_keys_run_in_parallel() -> None:
    lock = KeyedLock()
    gate = asyncio.Event()
    entered = 0

    async def worker(key: str) -> None:
        nonlocal entered
        async with lock(key):
            entered += 1
            if entered == 3:
                gate.set()
            await gate.wait()  # 三個都進得來才會放行；被序列化就會逾時

    await asyncio.wait_for(asyncio.gather(worker("a"), worker("b"), worker("c")), timeout=TIMEOUT)
    assert entered == 3


async def test_keyed_lock_releases_entries() -> None:
    """鎖以引用計數回收，長期執行下不會累積成無上限的 dict。"""
    lock = KeyedLock()

    async def worker(key: str) -> None:
        async with lock(key):
            await asyncio.sleep(0)

    await asyncio.gather(*(worker(f"k{i}") for i in range(20)))
    assert lock.active_keys() == 0


# ------------------------------------------------------------------ #
# Gateway：同群序列、跨群並行
# ------------------------------------------------------------------ #
@dataclass
class _User:
    id: int = 7
    username: str | None = "tester"
    is_bot: bool = False


@dataclass
class _Chat:
    id: int
    title: str | None = "群"
    type: str = "supergroup"


@dataclass
class _Msg:
    chat: _Chat
    message_id: int
    from_user: _User = field(default_factory=_User)
    message_thread_id: int | None = None
    reply_to_message: Any = None
    text: str = "小石 查一下"


class _FakeAudit:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    async def record(self, **kw: Any) -> None:
        self.records.append(kw)


class _Probe:
    """業務處理器替身：記錄併發峰值與進出順序。"""

    def __init__(self, hold: float = 0.02) -> None:
        self.hold = hold
        self.peak = 0
        self.current = 0
        self.order: list[int] = []

    async def __call__(self, req: BusinessRequest) -> BusinessResult:
        self.current += 1
        self.peak = max(self.peak, self.current)
        self.order.append(req.trigger_message_id)
        await asyncio.sleep(self.hold)
        self.current -= 1
        return BusinessResult(reply=f"done {req.trigger_message_id}")


def _gateway(handler: Any, *, agent_turns: int = 8, serialize: bool = True) -> tuple[Gateway, list[int]]:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        telegram_bot_token="123:abc",
        telegram_admin_id=1,
        llm_api_key="k",
        gitlab_token="g",
        hackmd_token="h",
        hackmd_team_path="sitcon",
        max_concurrent_agent_turns=agent_turns,
        serialize_per_chat=serialize,
    )
    gw = Gateway(settings, groups=None, audit=_FakeAudit(), commands=None, business_handler=handler)  # type: ignore[arg-type]

    reacted: list[int] = []

    async def _react(message: Any, emoji: str) -> None:
        reacted.append(message.message_id)

    async def _reply(message: Any, text: str) -> int | None:
        return message.message_id + 1000

    def _typing(chat_id: int, thread_id: int | None) -> Any:
        return _null_ctx()

    gw._react = _react  # type: ignore[method-assign]
    gw._reply = _reply  # type: ignore[method-assign]
    gw._typing = _typing  # type: ignore[method-assign,assignment]
    return gw, reacted


def _null_ctx() -> Any:
    import contextlib

    @contextlib.asynccontextmanager
    async def _cm() -> Any:
        yield

    return _cm()


async def test_same_chat_is_serialized() -> None:
    probe = _Probe()
    gw, _ = _gateway(probe)
    msgs = [_Msg(_Chat(-100), 1), _Msg(_Chat(-100), 2), _Msg(_Chat(-100), 3)]

    await asyncio.wait_for(
        asyncio.gather(*(gw._handle_business(m, m.text) for m in msgs)), timeout=TIMEOUT
    )

    assert probe.peak == 1  # 同一對話一次只跑一個
    assert probe.order == [1, 2, 3]  # 且維持送達順序


async def test_same_chat_parallel_when_serialization_disabled() -> None:
    """SERIALIZE_PER_CHAT=false → SPEC EC-16 的同群併發觸發彼此不阻塞。"""
    probe = _Probe(hold=0.05)
    gw, _ = _gateway(probe, serialize=False)
    msgs = [_Msg(_Chat(-100), 1), _Msg(_Chat(-100), 2), _Msg(_Chat(-100), 3)]

    await asyncio.wait_for(
        asyncio.gather(*(gw._handle_business(m, m.text) for m in msgs)), timeout=TIMEOUT
    )
    assert probe.peak == 3


async def test_different_chats_run_in_parallel() -> None:
    probe = _Probe(hold=0.05)
    gw, _ = _gateway(probe)
    msgs = [_Msg(_Chat(-100), 1), _Msg(_Chat(-200), 2), _Msg(_Chat(-300), 3)]

    started = time.monotonic()
    await asyncio.wait_for(
        asyncio.gather(*(gw._handle_business(m, m.text) for m in msgs)), timeout=TIMEOUT
    )
    elapsed = time.monotonic() - started

    assert probe.peak == 3
    assert elapsed < 0.05 * 3  # 相加就代表沒有並行


async def test_forum_topics_in_same_chat_run_in_parallel() -> None:
    """同一群的不同 topic 視為不同對話（各組各跑各的）。"""
    probe = _Probe(hold=0.05)
    gw, _ = _gateway(probe)
    msgs = [
        _Msg(_Chat(-100), 1, message_thread_id=11),
        _Msg(_Chat(-100), 2, message_thread_id=22),
    ]

    await asyncio.wait_for(
        asyncio.gather(*(gw._handle_business(m, m.text) for m in msgs)), timeout=TIMEOUT
    )
    assert probe.peak == 2


async def test_global_agent_turn_cap() -> None:
    probe = _Probe(hold=0.02)
    gw, _ = _gateway(probe, agent_turns=2)
    msgs = [_Msg(_Chat(-100 * i), i) for i in range(1, 6)]

    await asyncio.wait_for(
        asyncio.gather(*(gw._handle_business(m, m.text) for m in msgs)), timeout=TIMEOUT
    )
    assert probe.peak == 2  # 五個不同群，但全域只放行兩個


async def test_queued_messages_get_immediate_receipt_reaction() -> None:
    """排隊中的訊息也要先收到 👀：使用者不能看起來像被忽略。"""
    probe = _Probe(hold=0.05)
    gw, reacted = _gateway(probe)
    msgs = [_Msg(_Chat(-100), 1), _Msg(_Chat(-100), 2)]

    task = asyncio.gather(*(gw._handle_business(m, m.text) for m in msgs))
    await asyncio.sleep(0.01)  # 第二則此時還卡在 per-chat 鎖上
    assert set(reacted) >= {1, 2}
    await asyncio.wait_for(task, timeout=TIMEOUT)


# ------------------------------------------------------------------ #
# 快取 single-flight
# ------------------------------------------------------------------ #
ROSTER_HEADER = [
    "nickname", "gitlab_username", "gitlab_id", "telegram_username", "telegram_id",
    "role", "position", "other_role", "email",
]
ROSTER_ROW = ["阿石", "shi", "1001", "shi_tg", "555", "開發組", "組長", "", "a@b.c"]


class _CountingRosterFetcher:
    def __init__(self, delay: float = 0.02) -> None:
        self.calls = 0
        self.delay = delay

    async def fetch(self) -> tuple[str, list[str], list[list[str]]]:
        self.calls += 1
        await asyncio.sleep(self.delay)
        return ("表", ROSTER_HEADER, [ROSTER_ROW])


async def test_roster_single_flight() -> None:
    fetcher = _CountingRosterFetcher()
    svc = RosterService(fetcher, ttl_seconds=600)

    rosters = await asyncio.gather(*(svc.get() for _ in range(5)))

    assert fetcher.calls == 1  # 冷快取下五個併發請求只抓一次
    assert all(r is rosters[0] for r in rosters)


async def test_roster_force_still_refetches() -> None:
    """/reload 的 force 不能被 single-flight 吃掉。"""
    fetcher = _CountingRosterFetcher(delay=0)
    svc = RosterService(fetcher, ttl_seconds=600)
    await svc.get()
    await svc.reload()
    assert fetcher.calls == 2


PHOTO_HEADER = [
    "photo_id", "photo_url", "image_preview_url", "album_title", "subject_type", "photographer",
    "scene_tags", "mood_tags", "recommended_uses", "orientation", "visual_description", "people_count",
]
PHOTO_ROW = ["1", "https://flickr/1", "https://img/1", "Camp", "people", "p", "講者", "", "", "landscape", "台上", "1"]


class _CountingPhotoFetcher:
    def __init__(self) -> None:
        self.calls = 0

    async def fetch(self) -> tuple[list[str], list[list[str]]]:
        self.calls += 1
        await asyncio.sleep(0.02)
        return PHOTO_HEADER, [PHOTO_ROW]


async def test_photo_index_single_flight() -> None:
    fetcher = _CountingPhotoFetcher()
    svc = PhotoIndexService(fetcher, ttl_seconds=600)

    await asyncio.gather(*(svc.get() for _ in range(5)))

    assert fetcher.calls == 1


class _CountingLabelBackend:
    """GitLabClient 的 backend 為同步（跑在 to_thread）；用 time.sleep 撐開競爭窗口。"""

    def __init__(self) -> None:
        self.calls = 0

    def list_labels(self) -> list[str]:
        self.calls += 1
        time.sleep(0.05)
        return ["Team::開發組", "Team::行政組"]


async def test_label_index_single_flight() -> None:
    backend = _CountingLabelBackend()
    client = GitLabClient(backend, label_cache_ttl=600)  # type: ignore[arg-type]

    indexes = await asyncio.gather(*(client.get_label_index() for _ in range(5)))

    assert backend.calls == 1
    assert all(i is indexes[0] for i in indexes)


async def test_label_index_force_still_refetches() -> None:
    backend = _CountingLabelBackend()
    client = GitLabClient(backend, label_cache_ttl=600)  # type: ignore[arg-type]
    await client.get_label_index()
    await client.reload_labels()
    assert backend.calls == 2


# ------------------------------------------------------------------ #
# Drive：資料夾 single-flight ＋ 路徑併發解析
# ------------------------------------------------------------------ #
DRIVE_SCOPE = {"SITCON 2027": "s27"}
DRIVE_FOLDERS: dict[str, dict[str, Any]] = {
    "s27": {"id": "s27", "name": "SITCON 2027", "parents": []},
    "c1": {"id": "c1", "name": "合約", "parents": ["s27"]},
    "v1": {"id": "v1", "name": "場地", "parents": ["c1"]},
}
# 十個檔案共用同一條祖先鏈 v1 → c1 → s27
DRIVE_FILES = [
    {"id": f"f{i}", "name": f"合約{i}.pdf", "parents": ["v1"], "mimeType": "application/pdf", "webViewLink": f"u{i}"}
    for i in range(10)
]


class _SlowDriveGateway:
    def __init__(self) -> None:
        self.get_calls: list[str] = []

    async def search_files(self, query: str) -> list[dict[str, Any]]:
        return list(DRIVE_FILES)

    async def get_folder(self, folder_id: str) -> dict[str, Any] | None:
        self.get_calls.append(folder_id)
        await asyncio.sleep(0.02)  # 模擬一趟 API 往返
        return DRIVE_FOLDERS.get(folder_id)

    async def get_file(self, file_id: str) -> dict[str, Any] | None:  # pragma: no cover - 未用到
        return None

    async def fetch_text(self, file_id: str, export_mime: str | None) -> str:  # pragma: no cover
        return ""


async def test_drive_search_resolves_paths_concurrently_with_single_flight() -> None:
    gateway = _SlowDriveGateway()
    svc = DriveSearchService(gateway, DRIVE_SCOPE, ttl_seconds=600)

    started = time.monotonic()
    result = await svc.search(["合約"], limit=20)
    elapsed = time.monotonic() - started

    assert [f.path for f in result.files] == [f"SITCON 2027/合約/場地/合約{i}.pdf" for i in range(10)]
    # 十個檔案共用同一條鏈：single-flight 讓每個資料夾只查一次（v1、c1；s27 是範圍根，不用查）
    assert gateway.get_calls == ["v1", "c1"]
    # 併發解析 → 延遲是「最深的那條鏈」而不是 10 個檔案相加
    assert elapsed < 0.02 * 10


@pytest.mark.parametrize("scope_names", [None, ["SITCON 2027"]])
async def test_drive_search_order_is_stable(scope_names: list[str] | None) -> None:
    """改併發後結果順序仍與 Drive 回傳順序一致（分頁靠這個穩定）。"""
    svc = DriveSearchService(_SlowDriveGateway(), DRIVE_SCOPE, ttl_seconds=600)
    result = await svc.search(["合約"], scope_names=scope_names, limit=20)
    assert [f.file_id for f in result.files] == [f"f{i}" for i in range(10)]
