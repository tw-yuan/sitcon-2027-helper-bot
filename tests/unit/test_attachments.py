"""使用者附件：擷取、暫存（跨群隔離／TTL）、視覺區塊、文字讀取、GitLab 附檔、adapter 轉換、相簿。"""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace
from typing import Any

import pytest
from test_draft_streaming import _FakeAudit, _settings
from test_gitlab_tools import FakeBackend, FakeRosterService, _client, _roster

from sitcon_bot.agent.core import VISION_DROPPED_NOTE, Agent, AgentRequest
from sitcon_bot.agent.prompts import PromptBuilder, PromptData
from sitcon_bot.agent.tools.attachment_tools import AttachmentReadTool, ReadAttachmentArgs
from sitcon_bot.agent.tools.base import ToolContext, ToolRegistry
from sitcon_bot.agent.tools.gitlab_tools import (
    CommentIssueArgs,
    CreateIssueArgs,
    GitlabCommentIssueTool,
    GitlabCreateIssueTool,
    GitlabUpdateIssueTool,
    UpdateIssueArgs,
)
from sitcon_bot.services.attachments import (
    Attachment,
    AttachmentError,
    AttachmentStore,
    extract_text,
    is_text_readable,
    sniff_image_mime,
)
from sitcon_bot.services.llm.anthropic_adapter import AnthropicAdapter
from sitcon_bot.services.llm.base import (
    ImageBlock,
    LLMClient,
    LLMResponse,
    Message,
    TextBlock,
    ToolResultBlock,
    Usage,
)
from sitcon_bot.services.llm.openai_compat_adapter import OpenAICompatAdapter
from sitcon_bot.telegram.attachments import extract_attachments
from sitcon_bot.telegram.gateway import Gateway

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"0" * 16
CTX = ToolContext(chat_id=-100, thread_id=None, user_id=42, username="yuan", text="x")


def _store(files: dict[str, bytes] | None = None, now: list[float] | None = None) -> AttachmentStore:
    clock = now if now is not None else [0.0]
    store = AttachmentStore(ttl_seconds=60, clock=lambda: clock[0])
    files = files or {}

    async def dl(file_id: str) -> bytes:
        if file_id not in files:
            raise RuntimeError("file not found")
        return files[file_id]

    store.bind_downloader(dl)
    return store


def _photo(file_id: str = "P", size: int = 100) -> Attachment:
    return Attachment(kind="photo", file_id=file_id, file_name=f"{file_id}.jpg", mime="image/jpeg", size=size)


# ------------------------------------------------------------------ #
# 暫存
# ------------------------------------------------------------------ #
async def test_store_scopes_by_chat_and_expires() -> None:
    now = [0.0]
    store = _store({"P": JPEG}, now)
    att = store.add(-100, _photo())
    assert len(att.id) == 6
    assert store.get(-100, att.id).file_id == "P"
    assert store.get(-100, f"[{att.id.upper()}]").file_id == "P"  # LLM 帶方括號／大寫也認
    with pytest.raises(AttachmentError, match="不屬於這個對話"):
        store.get(-999, att.id)
    now[0] = 61
    with pytest.raises(AttachmentError, match="過期"):
        store.get(-100, att.id)


async def test_store_registers_copies() -> None:
    store = _store()
    src = _photo()
    a = store.add(-1, src)
    b = store.add(-2, src)  # 同一份參照（相簿暫存）被兩次觸發登記
    assert a.id != b.id and src.id == ""
    assert store.get(-1, a.id).chat_id == -1 and store.get(-2, b.id).chat_id == -2


async def test_store_rejects_oversize_download() -> None:
    store = _store({"P": JPEG})
    att = store.add(-1, _photo(size=30 * 1024 * 1024))
    with pytest.raises(AttachmentError, match="20MB"):
        await store.load(att)


async def test_load_vision_sniffs_real_mime() -> None:
    # 貼圖縮圖宣稱 jpeg、實際是 webp → 以檔頭為準
    store = _store({"S": b"tgs", "T": WEBP, "D": b"%PDF-1.4"})
    sticker = store.add(
        -1,
        Attachment(
            kind="sticker",
            file_id="S",
            file_name="s.tgs",
            mime="application/x-tgsticker",
            preview_file_id="T",
            preview_mime="image/jpeg",
        ),
    )
    assert await store.load_vision(sticker) == (WEBP, "image/webp")
    pdf = store.add(-1, Attachment(kind="document", file_id="D", file_name="a.pdf", mime="application/pdf"))
    assert await store.load_vision(pdf) is None


def test_sniff_image_mime() -> None:
    assert sniff_image_mime(JPEG) == "image/jpeg"
    assert sniff_image_mime(PNG) == "image/png"
    assert sniff_image_mime(b"GIF89a....") == "image/gif"
    assert sniff_image_mime(WEBP) == "image/webp"
    assert sniff_image_mime(b"<svg") is None


# ------------------------------------------------------------------ #
# Telegram 訊息擷取
# ------------------------------------------------------------------ #
def _msg(**kw: Any) -> SimpleNamespace:
    base: dict[str, Any] = dict(
        message_id=9,
        photo=(),
        document=None,
        sticker=None,
        video=None,
        animation=None,
        video_note=None,
        audio=None,
        voice=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_extract_photo_takes_largest() -> None:
    sizes = (SimpleNamespace(file_id="small", file_size=10), SimpleNamespace(file_id="big", file_size=900))
    [att] = extract_attachments(_msg(photo=sizes))
    assert (att.kind, att.file_id, att.mime, att.size) == ("photo", "big", "image/jpeg", 900)
    assert att.vision_source() == ("big", "image/jpeg")


def test_extract_document_and_animated_sticker() -> None:
    doc = SimpleNamespace(
        file_id="D", file_name="報價單.pdf", mime_type="application/pdf", file_size=2048, thumbnail=None
    )
    sticker = SimpleNamespace(
        file_id="S",
        is_animated=True,
        is_video=False,
        emoji="😂",
        set_name="funny",
        file_size=5,
        thumbnail=SimpleNamespace(file_id="ST"),
    )
    [d] = extract_attachments(_msg(document=doc), "被回覆的訊息")
    assert (d.kind, d.file_name, d.source) == ("document", "報價單.pdf", "被回覆的訊息")
    assert d.vision_source() is None
    [s] = extract_attachments(_msg(sticker=sticker))
    assert s.preview_file_id == "ST" and "😂" in s.note and "funny" in s.note


def test_extract_animation_not_duplicated_as_document() -> None:
    anim = SimpleNamespace(file_id="A", file_name="x.mp4", mime_type="video/mp4", file_size=1, thumbnail=None)
    atts = extract_attachments(_msg(animation=anim, document=anim))
    assert [a.kind for a in atts] == ["animation"]


# ------------------------------------------------------------------ #
# 文字擷取
# ------------------------------------------------------------------ #
def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", f"<w:document><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:document>")
    return buf.getvalue()


def test_extract_text_types() -> None:
    assert extract_text("text/plain", "a.txt", "你好".encode()) == ("你好", "純文字")
    # Telegram 常把 Office 檔標成 octet-stream → 以副檔名補判
    assert is_text_readable("application/octet-stream", "議程.docx")
    assert extract_text("application/octet-stream", "議程.docx", _docx("開幕"))[0] == "開幕"
    assert not is_text_readable("image/jpeg", "a.jpg")
    with pytest.raises(AttachmentError):
        extract_text("image/jpeg", "a.jpg", JPEG)


async def test_attachment_read_tool_paginates_and_wraps() -> None:
    store = _store({"F": ("字" * 13000).encode()})
    att = store.add(-100, Attachment(kind="document", file_id="F", file_name="a.txt", mime="text/plain"))
    tool = AttachmentReadTool(store)
    out = await tool.run(ReadAttachmentArgs(attachment_id=att.id), CTX)
    assert "<external_data>" in out and "offset=12000" in out
    out2 = await tool.run(ReadAttachmentArgs(attachment_id=att.id, offset=12000), CTX)
    assert "續讀" not in out2
    other_chat = ToolContext(chat_id=-5, thread_id=None, user_id=1, username=None, text="")
    assert "不屬於" in await tool.run(ReadAttachmentArgs(attachment_id=att.id), other_chat)


# ------------------------------------------------------------------ #
# Agent：視覺區塊與不支援時的退回
# ------------------------------------------------------------------ #
class _VisionLLM(LLMClient):
    def __init__(self, reject_images: bool = False) -> None:
        self.reject = reject_images
        self.calls: list[list[Message]] = []

    async def chat(self, *, system: str, messages: list[Message], tools: Any, thinking: Any, **_: Any) -> LLMResponse:
        self.calls.append([Message(m.role, list(m.content), m.raw) for m in messages])
        has_img = any(isinstance(b, ImageBlock) for m in messages for b in m.content)
        if self.reject and has_img:
            raise type("BadRequest", (Exception,), {"status_code": 400})("image input not supported")
        return LLMResponse(
            text="看到了", tool_calls=[], usage=Usage(0, 0), stop_reason="end_turn", model="m", raw_assistant=[]
        )


async def _provider() -> PromptData:
    return PromptData()


async def _no_sleep(_: float) -> None:
    return None


def _agent(llm: LLMClient, store: AttachmentStore, vision: bool = True) -> Agent:
    return Agent(
        llm=llm,
        tools=ToolRegistry([]),
        prompt_builder=PromptBuilder(_provider),
        sleep=_no_sleep,
        attachments=store,
        vision=vision,
    )


def _areq(atts: list[Attachment], **kw: Any) -> AgentRequest:
    return AgentRequest(
        chat_id=-100, thread_id=None, user_id=42, username="yuan", text="這是什麼", attachments=atts, **kw
    )


async def test_agent_sends_image_blocks_and_lists_attachments() -> None:
    store = _store({"P": JPEG, "D": b"%PDF"})
    photo = store.add(-100, _photo())
    doc = store.add(-100, Attachment(kind="document", file_id="D", file_name="x.pdf", mime="application/pdf"))
    llm = _VisionLLM()
    res = await _agent(llm, store).handle(_areq([photo, doc]))
    assert res.reply == "看到了"
    blocks = llm.calls[0][0].content
    images = [b for b in blocks if isinstance(b, ImageBlock)]
    assert len(images) == 1 and images[0].media_type == "image/jpeg" and images[0].data == JPEG
    note = next(b.text for b in blocks if isinstance(b, TextBlock) and "附件" in b.text)
    assert f"[{photo.id}]" in note and f"[{doc.id}]" in note and "attachment_read" in note
    assert "<external_data>" in note  # 檔名等使用者可控內容包圍欄


async def test_agent_vision_off_sends_no_images() -> None:
    store = _store({"P": JPEG})
    llm = _VisionLLM()
    await _agent(llm, store, vision=False).handle(_areq([store.add(-100, _photo())]))
    blocks = llm.calls[0][0].content
    assert not any(isinstance(b, ImageBlock) for b in blocks)
    assert any("看不到畫面" in b.text for b in blocks if isinstance(b, TextBlock))


async def test_agent_strips_images_when_model_rejects() -> None:
    store = _store({"P": JPEG})
    llm = _VisionLLM(reject_images=True)
    res = await _agent(llm, store).handle(_areq([store.add(-100, _photo())]))
    assert res.status == "ok" and res.reply == "看到了"
    assert len(llm.calls) == 2  # 帶圖被拒 → 立即拿掉圖重送，不走退避重試
    retry_blocks = llm.calls[1][0].content
    assert not any(isinstance(b, ImageBlock) for b in retry_blocks)
    assert any(isinstance(b, TextBlock) and b.text == VISION_DROPPED_NOTE for b in retry_blocks)
    # 存回的 transcript 也不再帶圖（續接時不會又被拒）
    assert res.history is not None
    assert not any(isinstance(b, ImageBlock) for m in res.history for b in m.content)


# ------------------------------------------------------------------ #
# LLM adapter 轉換
# ------------------------------------------------------------------ #
def test_anthropic_image_block() -> None:
    msg = AnthropicAdapter._to_message(Message("user", [TextBlock("看"), ImageBlock("image/png", PNG)]))
    img = msg["content"][1]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
    assert img["source"]["type"] == "base64" and isinstance(img["source"]["data"], str)


def test_openai_image_parts_after_tool_results() -> None:
    m = Message("user", [ToolResultBlock("call_1", "答案"), TextBlock("附件："), ImageBlock("image/jpeg", JPEG)])
    out = OpenAICompatAdapter._to_messages(m)
    assert out[0] == {"role": "tool", "tool_call_id": "call_1", "content": "答案"}
    assert out[1]["role"] == "user"
    parts = out[1]["content"]
    assert parts[0] == {"type": "text", "text": "附件："}
    assert parts[1]["type"] == "image_url" and parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_openai_text_only_user_stays_string() -> None:
    assert OpenAICompatAdapter._to_messages(Message("user", [TextBlock("a"), TextBlock("b")])) == [
        {"role": "user", "content": "ab"}
    ]


# ------------------------------------------------------------------ #
# GitLab 附檔
# ------------------------------------------------------------------ #
class _UploadBackend(FakeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.uploads: list[tuple[str, bytes]] = []

    def upload_file(self, filename: str, data: bytes) -> dict[str, Any]:
        self.uploads.append((filename, data))
        n = len(self.uploads)
        return {"markdown": f"![{filename}](/uploads/h{n}/{filename})", "url": f"/uploads/h{n}/{filename}"}

    def update_issue(self, iid: int, payload: dict[str, Any]) -> dict[str, Any]:
        if "description" in payload:
            self.issues[iid]["description"] = payload["description"]
        return super().update_issue(iid, payload)


async def test_create_issue_uploads_and_embeds_attachments() -> None:
    backend = _UploadBackend()
    store = _store({"P": JPEG})
    att = store.add(-100, _photo())
    tool = GitlabCreateIssueTool(_client(backend), FakeRosterService(_roster()), attachments=store)
    reply = await tool.run(CreateIssueArgs(title="海報錯字", description="見附圖", attachment_ids=[att.id]), CTX)
    assert "已附 1 個附件" in reply
    assert backend.uploads == [("P.jpg", JPEG)]
    desc = backend.last_create_payload["description"]
    # 順序：描述 → 附件 → 來源標註
    assert desc.index("見附圖") < desc.index("![P.jpg](/uploads/h1/P.jpg)") < desc.index("via 小石")


async def test_create_issue_aborts_when_attachment_invalid() -> None:
    backend = _UploadBackend()
    tool = GitlabCreateIssueTool(_client(backend), FakeRosterService(_roster()), attachments=_store())
    reply = await tool.run(CreateIssueArgs(title="x", attachment_ids=["zzzzzz"]), CTX)
    assert "未建卡" in reply and backend.last_create_payload is None


async def test_comment_with_only_attachment() -> None:
    backend = _UploadBackend()
    backend.issues[7] = {"iid": 7, "title": "t", "labels": [], "assignees": [], "state": "opened", "web_url": "u"}
    store = _store({"D": b"data"})
    att = store.add(-100, Attachment(kind="document", file_id="D", file_name="清單.xlsx", mime=None))
    tool = GitlabCommentIssueTool(_client(backend), FakeRosterService(_roster()), attachments=store)
    reply = await tool.run(CommentIssueArgs(iid=7, attachment_ids=[att.id]), CTX)
    assert "含 1 個附件" in reply
    assert "清單.xlsx" in backend.notes[7][0]["body"]
    assert "空的" in await tool.run(CommentIssueArgs(iid=7), CTX)


async def test_update_appends_attachment_to_existing_description() -> None:
    backend = _UploadBackend()
    backend.issues[7] = {
        "iid": 7,
        "title": "t",
        "description": "原本描述\n\n> _via 小石 · requested by x_",
        "labels": [],
        "assignees": [],
        "state": "opened",
        "web_url": "u",
    }
    store = _store({"P": JPEG})
    att = store.add(-100, _photo())
    tool = GitlabUpdateIssueTool(_client(backend), FakeRosterService(_roster()), attachments=store)
    reply = await tool.run(UpdateIssueArgs(iid=7, attachment_ids=[att.id]), CTX)
    desc = backend.issues[7]["description"]
    assert desc.startswith("原本描述") and desc.endswith("![P.jpg](/uploads/h1/P.jpg)")
    assert desc.count("via 小石") == 1  # 只附檔不重複加來源標註
    assert "描述加上 1 個附件" in reply


# ------------------------------------------------------------------ #
# Gateway：本則＋被回覆訊息＋相簿
# ------------------------------------------------------------------ #
def _tg_msg(mid: int, file_id: str, group: str | None = None, reply_to: Any = None) -> SimpleNamespace:
    m = _msg(
        message_id=mid,
        photo=(SimpleNamespace(file_id=file_id, file_size=10),),
        media_group_id=group,
        reply_to_message=reply_to,
        chat=SimpleNamespace(id=-100),
    )
    return m


async def test_gateway_collects_album_and_reply_attachments() -> None:
    store = _store()
    gw = Gateway(_settings(), groups=None, audit=_FakeAudit(), commands=None, attachments=store, album_settle=0)  # type: ignore[arg-type]
    for mid, fid in ((10, "A1"), (11, "A2"), (12, "A3")):
        gw._albums.put(-100, "G", mid, extract_attachments(_tg_msg(mid, fid, "G"), "同一則相簿"))
    trigger = _tg_msg(11, "A2", "G", reply_to=_tg_msg(5, "OLD"))
    atts = await gw._collect_attachments(trigger)  # type: ignore[arg-type]
    assert [a.file_id for a in atts] == ["A2", "A1", "A3", "OLD"]
    assert atts[0].source == "本則訊息" and atts[1].source == "同一則相簿" and atts[3].source == "被回覆的訊息"
    assert all(store.get(-100, a.id) for a in atts)
