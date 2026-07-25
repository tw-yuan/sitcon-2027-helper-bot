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


async def test_prompt_roster_unavailable_notice() -> None:
    s = await _build(PromptData(labels=["Status::Inbox"], roster_rows=[], roster_available=False))
    assert "名冊：目前暫不可用" in s  # EC-11


async def test_prompt_roster_only_whitelist_rows() -> None:
    # 名冊列只放白名單欄位（RO-7）——此處驗證 prompt 忠實呈現傳入的精簡列
    rows = [{"nickname": "Yuan", "gitlab_id": 1, "role": "開發組", "position": "組長"}]
    s = await _build(PromptData(labels=["Status::Inbox"], roster_rows=rows))
    assert "Yuan" in s
    assert "組長" in s  # 組長
