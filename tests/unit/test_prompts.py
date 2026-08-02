"""T9/T7：system prompt 組裝——RO-8 職掌缺檔容忍、EC-11 名冊不可用提示、NFR-6 外部資料標記。"""

from __future__ import annotations

from sitcon_bot.agent.prompts import PromptBuilder, PromptData


async def _build(data: PromptData) -> str:
    async def provider() -> PromptData:
        return data

    return await PromptBuilder(provider).build()


async def test_prompt_without_charter_still_builds() -> None:
    s = await _build(PromptData(labels=["Status::Inbox", "Team::開發組"], roster_rows=[{"nickname": "Yuan"}]))
    assert "改以 Team:: label" in s  # RO-8：缺職掌文件的提示
    assert "Status::Inbox" in s
    assert "external_data" in s  # NFR-6 資料非指令聲明


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


async def test_prompt_roster_only_whitelist_rows() -> None:
    # 名冊列只放白名單欄位（RO-7）——此處驗證 prompt 忠實呈現傳入的精簡列
    rows = [{"nickname": "Yuan", "gitlab_id": 1, "role": "開發組", "position": "組長"}]
    s = await _build(PromptData(labels=["Status::Inbox"], roster_rows=rows))
    assert "Yuan" in s
    assert "組長" in s  # 組長
