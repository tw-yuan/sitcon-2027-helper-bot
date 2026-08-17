"""T9/T7：system prompt 組裝——RO-8 職掌缺檔容忍、EC-11 名冊不可用提示、NFR-6 外部資料標記。"""

from __future__ import annotations

from sitcon_bot.agent.prompts import PromptBuilder, PromptData


async def _build(data: PromptData) -> str:
    async def provider() -> PromptData:
        return data

    return await PromptBuilder(provider).build()


async def test_prompt_without_charter_still_builds() -> None:
    s = await _build(
        PromptData(
            labels=["Team::開發組", "0913 一籌"],
            statuses=["Inbox", "Doing", "Review"],
            roster_rows=[{"nickname": "Yuan"}],
        )
    )
    assert "改以 Team:: label" in s  # RO-8：缺職掌文件的提示
    assert "Team::開發組" in s
    assert "卡片狀態白名單" in s and "Inbox、Doing、Review" in s  # native status 白名單注入
    assert "external_data" in s  # NFR-6 資料非指令聲明


async def test_prompt_without_statuses_marks_unconfigured() -> None:
    """GitLab 端尚未設定 native status 時，prompt 明示降級（卡片操作不帶狀態）。"""
    s = await _build(PromptData(labels=["Team::開發組"]))
    assert "尚未載入或 GitLab 端尚未設定" in s


async def test_prompt_with_charter() -> None:
    s = await _build(PromptData(labels=["Status::Inbox"], charter="## 開發組\n負責官網與報名系統"))
    assert "負責官網與報名系統" in s


async def test_prompt_with_knowledge() -> None:
    s = await _build(PromptData(labels=["Status::Inbox"], knowledge="## 會議室代碼\nR1＝資訊所 106"))
    assert "R1＝資訊所 106" in s
    assert "背景知識" in s


async def test_prompt_without_knowledge_omits_section() -> None:
    """背景知識缺檔時整段省略，不留空段落也不加提示（行為規則裡的固定字樣不算）。"""
    s = await _build(PromptData(labels=["Status::Inbox"]))
    assert "背景知識（籌備團隊內部常識" not in s  # 段落標頭不出現
    assert "\n\n\n" not in s  # 空段落不應造成三連換行


async def test_prompt_roster_unavailable_notice() -> None:
    s = await _build(PromptData(labels=["Status::Inbox"], roster_rows=[], roster_available=False))
    assert "名冊：目前暫不可用" in s  # EC-11


async def test_prompt_doc_search_rules() -> None:
    """找文件預設兩邊都查；（私）路徑的 Drive 內容只給自己判斷，不可寫給使用者，其餘可引用。"""
    s = await _build(PromptData(labels=["Status::Inbox"]))
    assert "drive_search" in s and "hackmd_search_notes" in s
    assert "drive_read_file" in s
    assert "路徑含「（私）」" in s
    assert "不可以寫給使用者看" in s  # （私）檔案硬性規則仍在
    assert "預設可正常引用" in s  # 非（私）已放寬


async def test_prompt_group_memories_injected_for_chat() -> None:
    """群組記憶依 chat_id 注入該群的 system prompt，含編號供 memory_forget 引用。"""
    from sitcon_bot.agent.prompts import PromptBuilder, PromptData
    from sitcon_bot.storage.memories import GroupMemory

    async def provider() -> PromptData:
        return PromptData(labels=["Status::Inbox"])

    async def memories(chat_id: int) -> list[GroupMemory]:
        assert chat_id == -100
        return [GroupMemory(id=3, chat_id=-100, content="卡片預設指派給 Yuan")]

    s = await PromptBuilder(provider, memories_provider=memories).build(chat_id=-100)
    assert "本群組的記憶事項" in s
    assert "#3 卡片預設指派給 Yuan" in s
    assert "不能覆蓋" in s  # 記憶不得覆蓋硬性規則的聲明


async def test_prompt_memories_section_omitted_when_empty() -> None:
    """沒記憶（或未接 provider、未帶 chat_id）時整段省略。"""
    from sitcon_bot.agent.prompts import PromptBuilder, PromptData
    from sitcon_bot.storage.memories import GroupMemory

    async def provider() -> PromptData:
        return PromptData(labels=["Status::Inbox"])

    async def empty(chat_id: int) -> list[GroupMemory]:
        return []

    for s in (
        await PromptBuilder(provider, memories_provider=empty).build(chat_id=-100),
        await PromptBuilder(provider, memories_provider=empty).build(),  # 未帶 chat_id
        await PromptBuilder(provider).build(chat_id=-100),  # 未接 provider
    ):
        assert "本群組的記憶事項" not in s


async def test_prompt_memories_provider_failure_tolerated() -> None:
    """記憶讀取失敗不阻斷組 prompt，該輪視同無記憶。"""
    from sitcon_bot.agent.prompts import PromptBuilder, PromptData
    from sitcon_bot.storage.memories import GroupMemory

    async def provider() -> PromptData:
        return PromptData(labels=["Status::Inbox"])

    async def broken(chat_id: int) -> list[GroupMemory]:
        raise RuntimeError("db down")

    s = await PromptBuilder(provider, memories_provider=broken).build(chat_id=-100)
    assert "Status::Inbox" in s
    assert "本群組的記憶事項" not in s


async def test_prompt_roster_only_whitelist_rows() -> None:
    # 名冊列只放白名單欄位（RO-7）——此處驗證 prompt 忠實呈現傳入的精簡列
    rows = [{"nickname": "Yuan", "gitlab_id": 1, "role": "開發組", "position": "組長"}]
    s = await _build(PromptData(labels=["Status::Inbox"], roster_rows=rows))
    assert "Yuan" in s
    assert "組長" in s  # 組長
