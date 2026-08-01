"""T11：HackMD client（respx HTTP mock）——folders、subfolder 自動建、建/讀/改筆記、重試、憑證、無刪除。"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from sitcon_bot.services.hackmd_client import (
    BASE_URL,
    HackMDAPIError,
    HackMDClient,
    HackMDCredentialError,
)

TEAM = "sitcon"


async def _noop(_: float) -> None:
    return None


def _client() -> HackMDClient:
    return HackMDClient(token="t", team_path=TEAM, sleep=_noop)


async def test_list_and_find_folder() -> None:
    with respx.mock:
        respx.get(f"{BASE_URL}/teams/{TEAM}/folders").mock(
            return_value=httpx.Response(
                200,
                json=[
                    {"id": "f1", "name": "開發組", "parentFolderId": None},
                    {"id": "f2", "name": "會議文件", "parentFolderId": "f1"},
                ],
            )
        )
        c = _client()
        assert (await c.find_folder("開發組")).id == "f1"
        assert (await c.find_folder("會議文件", parent_id="f1")).id == "f2"
        assert await c.find_folder("不存在") is None
        await c.aclose()


async def test_ensure_subfolder_creates_when_missing() -> None:
    with respx.mock:
        respx.get(f"{BASE_URL}/teams/{TEAM}/folders").mock(
            return_value=httpx.Response(200, json=[{"id": "f1", "name": "行政組", "parentFolderId": None}])
        )
        create = respx.post(f"{BASE_URL}/teams/{TEAM}/folders").mock(
            return_value=httpx.Response(201, json={"id": "sub1", "name": "會議文件", "parentFolderId": "f1"})
        )
        c = _client()
        sub = await c.ensure_meeting_subfolder("f1", "會議文件")
        assert sub.id == "sub1"
        assert create.called
        body = json.loads(create.calls.last.request.content)
        assert body == {"name": "會議文件", "parentFolderId": "f1"}
        await c.aclose()


async def test_create_note_payload() -> None:
    with respx.mock:
        route = respx.post(f"{BASE_URL}/teams/{TEAM}/notes").mock(
            return_value=httpx.Response(
                201, json={"id": "n1", "title": "0913 一籌", "tags": ["SITCON 2027"], "publishLink": "https://hackmd.io/n1"}
            )
        )
        c = _client()
        note = await c.create_note(
            title="0913 一籌", content="body", tags=["SITCON 2027", "會議文件"], parent_folder_id="f1"
        )
        assert note.id == "n1"
        assert note.url == "https://hackmd.io/n1"
        body = json.loads(route.calls.last.request.content)
        assert body["tags"] == ["SITCON 2027", "會議文件"]
        assert body["readPermission"] == "signed_in"
        assert body["writePermission"] == "signed_in"
        assert body["parentFolderId"] == "f1"
        await c.aclose()


async def test_get_and_update_note() -> None:
    with respx.mock:
        respx.get(f"{BASE_URL}/teams/{TEAM}/notes/n1").mock(
            return_value=httpx.Response(
                200, json={"id": "n1", "title": "t", "content": "內文", "tags": ["SITCON 2027"], "shortId": "abc"}
            )
        )
        patch = respx.patch(f"{BASE_URL}/teams/{TEAM}/notes/n1").mock(return_value=httpx.Response(202))
        c = _client()
        note = await c.get_note("n1")
        assert note.content == "內文"
        assert note.url == "https://hackmd.io/abc"  # shortId fallback
        await c.update_note("n1", content="新內文", tags=["SITCON 2027"])
        assert patch.called
        body = json.loads(patch.calls.last.request.content)
        assert body["content"] == "新內文"
        await c.aclose()


async def test_move_note_patches_parent_and_invalidates_cache() -> None:
    with respx.mock:
        notes = respx.get(f"{BASE_URL}/teams/{TEAM}/notes").mock(
            return_value=httpx.Response(200, json=[{"id": "n1", "title": "t", "tags": []}])
        )
        patch = respx.patch(f"{BASE_URL}/teams/{TEAM}/notes/n1").mock(return_value=httpx.Response(202))
        c = _client()
        await c.list_notes()
        await c.move_note("n1", "f9")
        assert json.loads(patch.calls.last.request.content) == {"parentFolderId": "f9"}
        await c.list_notes()
        assert notes.call_count == 2  # 移動後 metadata 快取失效（folderPaths 已變）
        await c.aclose()


async def test_notes_cache_hits_once() -> None:
    with respx.mock:
        route = respx.get(f"{BASE_URL}/teams/{TEAM}/notes").mock(
            return_value=httpx.Response(200, json=[{"id": "n1", "title": "t", "tags": []}])
        )
        c = _client()
        await c.list_notes()
        await c.list_notes()
        assert route.call_count == 1  # TTL 內只打一次
        await c.aclose()


async def test_retry_on_5xx_then_success() -> None:
    with respx.mock:
        route = respx.get(f"{BASE_URL}/teams/{TEAM}/notes").mock(
            side_effect=[httpx.Response(503), httpx.Response(503), httpx.Response(200, json=[])]
        )
        c = _client()
        await c.list_notes()
        assert route.call_count == 3
        await c.aclose()


async def test_401_raises_credential_error() -> None:
    with respx.mock:
        respx.get(f"{BASE_URL}/teams/{TEAM}/notes").mock(return_value=httpx.Response(401))
        c = _client()
        with pytest.raises(HackMDCredentialError):
            await c.list_notes()
        await c.aclose()


async def test_4xx_raises_api_error() -> None:
    with respx.mock:
        respx.post(f"{BASE_URL}/teams/{TEAM}/notes").mock(return_value=httpx.Response(400, text="bad"))
        c = _client()
        with pytest.raises(HackMDAPIError):
            await c.create_note(title="t", content="c", tags=[])
        await c.aclose()


def test_no_delete_method_hm16() -> None:
    # HM-16：client 層不存在刪除方法
    assert not hasattr(HackMDClient, "delete_note")
    assert not hasattr(HackMDClient, "delete_folder")
