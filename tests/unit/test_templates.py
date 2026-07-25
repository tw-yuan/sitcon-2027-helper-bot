"""T11：會議模板變數替換（HM-5）。"""

from __future__ import annotations

from sitcon_bot.domain.templates import TemplateStore


def test_render_substitutes_all_vars() -> None:
    store = TemplateStore({"summit": "# {{title}}\n日期 {{date}}\n類型 {{meeting_type}}\n組 {{team}}"})
    out = store.render("summit", title="0913 一籌", date="2027-09-13", meeting_type="籌會", team="")
    assert "0913 一籌" in out
    assert "2027-09-13" in out
    assert "籌會" in out
    assert "{{" not in out  # 變數全部替換


def test_render_unknown_kind_returns_empty() -> None:
    assert TemplateStore({}).render("nope") == ""
