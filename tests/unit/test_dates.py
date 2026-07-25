"""T11：日期輔助（MMDD 擷取、顯示日期）。"""

from __future__ import annotations

from sitcon_bot.domain.dates import display_date, to_mmdd


def test_to_mmdd_iso() -> None:
    assert to_mmdd("2027-09-13") == "0913"


def test_to_mmdd_short() -> None:
    assert to_mmdd("9-13") == "0913"
    assert to_mmdd("1/5") == "0105"


def test_to_mmdd_already_mmdd() -> None:
    assert to_mmdd("0913") == "0913"


def test_to_mmdd_none_is_today() -> None:
    out = to_mmdd(None)
    assert len(out) == 4 and out.isdigit()


def test_to_mmdd_unparseable_falls_back() -> None:
    out = to_mmdd("下週五")  # 無法解析 → 今日
    assert len(out) == 4 and out.isdigit()


def test_display_date_passthrough_and_default() -> None:
    assert display_date("2027-09-13") == "2027-09-13"
    d = display_date(None)
    assert len(d) == 10 and d[4] == "-"
