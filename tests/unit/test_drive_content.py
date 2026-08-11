"""各檔案類型完整擷取（DR-10 2026-08-11 修訂）：純渲染器＋DriveContentService 分流。

真實 API 的回傳形狀以 verify_apis 實測為準（Docs 分頁＋表格、Sheets 多工作表含非表格型、
Slides 備註、Forms 題型、Office zip 結構）；渲染器吃 dict／bytes，測試不打網路。
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from sitcon_bot.services.drive_client import (
    CONTENT_LIMIT,
    DriveFile,
    DriveReadError,
)
from sitcon_bot.services.drive_content import (
    ApiDisabledError,
    DriveContentService,
    a1_quote,
    extract_docx_text,
    extract_pptx_text,
    extract_svg_text,
    extract_xlsx_text,
    render_apps_script,
    render_document,
    render_form,
    render_presentation,
    render_spreadsheet,
)


# --------------------------------------------------------------------------- #
# Docs：多分頁（含巢狀）＋段落＋表格
# --------------------------------------------------------------------------- #
def _para(text: str, bullet: bool = False) -> dict[str, Any]:
    p: dict[str, Any] = {"elements": [{"textRun": {"content": text}}]}
    if bullet:
        p["bullet"] = {}
    return p


def _tab(title: str, contents: list[dict], children: list[dict] | None = None) -> dict[str, Any]:
    return {
        "tabProperties": {"title": title},
        "documentTab": {"body": {"content": contents}},
        "childTabs": children or [],
    }


DOC_MULTI_TAB = {
    "title": "企劃書",
    "tabs": [
        _tab("總覽", [{"paragraph": _para("今年主題是山線\n")}]),
        _tab(
            "預算",
            [
                {
                    "table": {
                        "tableRows": [
                            {"tableCells": [
                                {"content": [{"paragraph": _para("項目")}]},
                                {"content": [{"paragraph": _para("金額")}]},
                            ]},
                            {"tableCells": [
                                {"content": [{"paragraph": _para("場地")}]},
                                {"content": [{"paragraph": _para("120000")}]},
                            ]},
                        ]
                    }
                }
            ],
            children=[_tab("備註", [{"paragraph": _para("含清潔費\n")}])],
        ),
    ],
}


def test_render_document_walks_all_tabs_and_tables() -> None:
    text, kind = render_document(DOC_MULTI_TAB)
    assert "【分頁：總覽】" in text and "【分頁：預算】" in text
    assert "【分頁：備註】" in text  # 巢狀 childTabs 也要展開
    assert "山線" in text
    assert "｜項目｜金額｜" in text and "｜場地｜120000｜" in text  # 表格逐列
    assert "3 個分頁" in kind


def test_render_document_single_tab_no_header() -> None:
    doc = {"tabs": [_tab("Tab 1", [{"paragraph": _para("內文")}])]}
    text, _kind = render_document(doc)
    assert text == "內文"
    assert "分頁" not in text


def test_render_document_pick_tab() -> None:
    text, kind = render_document(DOC_MULTI_TAB, tab="預算")
    assert "場地" in text
    assert "山線" not in text
    assert "預算" in kind


def test_render_document_unknown_tab_lists_available() -> None:
    with pytest.raises(DriveReadError, match="總覽、預算、備註"):
        render_document(DOC_MULTI_TAB, tab="沒有這頁")


def test_render_document_legacy_body() -> None:
    doc = {"body": {"content": [{"paragraph": _para("舊格式內容")}]}}
    text, _ = render_document(doc)
    assert text == "舊格式內容"


def test_render_document_bullet_and_objects() -> None:
    doc = {
        "body": {
            "content": [
                {"paragraph": _para("第一點\n", bullet=True)},
                {"paragraph": {"elements": [{"inlineObjectElement": {}}]}},
            ]
        }
    }
    text, _ = render_document(doc)
    assert "• 第一點" in text
    assert "[圖片/物件]" in text


# --------------------------------------------------------------------------- #
# Sheets：全部工作表、非表格型註記、逐表容錯、單表模式
# --------------------------------------------------------------------------- #
SHEET_META = {
    "properties": {"title": "SITCON 2027 預算"},
    "sheets": [
        {"properties": {"title": "總表", "sheetType": "GRID"}},
        {"properties": {"title": "行政組(AD)", "sheetType": "GRID"}},
        {"properties": {"title": "時間軸", "sheetType": "OBJECT"}},
        {"properties": {"title": "草稿", "sheetType": "GRID", "hidden": True}},
    ],
}


def test_render_spreadsheet_lists_all_sheets_and_bodies() -> None:
    values = {
        "總表": [["項目", "金額"], ["場地", "120000"], ["", ""]],
        "行政組(AD)": [["保險", "30000"]],
    }
    text, kind = render_spreadsheet(SHEET_META, values)
    assert "共 4 張工作表" in text
    assert "總表、行政組(AD)、時間軸（非表格）、草稿（隱藏）" in text
    assert "【工作表：總表】" in text and "場地\t120000" in text
    assert "【工作表：行政組(AD)】" in text and "保險\t30000" in text
    assert "17" not in kind and "4 張工作表" in kind


def test_render_spreadsheet_tolerates_per_sheet_failure() -> None:
    values: dict[str, Any] = {"總表": [["ok"]], "行政組(AD)": RuntimeError("400 non-grid")}
    text, _ = render_spreadsheet(SHEET_META, values)
    assert "ok" in text
    assert "讀取失敗" in text  # 一張壞不拖垮整本


def test_render_spreadsheet_caps_rows_in_all_mode_with_hint() -> None:
    rows = [[f"r{i}", "x"] for i in range(200)]
    text, _ = render_spreadsheet(SHEET_META, {"總表": rows})
    assert "r59" in text and "r60" not in text  # SHEET_ROWS_CAP_ALL=60
    assert "另 140 列未顯示" in text
    assert 'worksheet="總表"' in text  # 提示可指定整張


def test_render_spreadsheet_single_mode_wider_cap() -> None:
    rows = [[f"r{i}"] for i in range(200)]
    text, _ = render_spreadsheet(SHEET_META, {"總表": rows}, single="總表")
    assert "r199" in text  # 單表模式放寬列數


def test_a1_quote_escapes() -> None:
    assert a1_quote("總表") == "'總表'"
    assert a1_quote("it's") == "'it''s'"


# --------------------------------------------------------------------------- #
# Slides／Forms／Apps Script
# --------------------------------------------------------------------------- #
def _shape(text: str) -> dict[str, Any]:
    return {"shape": {"text": {"textElements": [{"textRun": {"content": text}}]}}}


def test_render_presentation_pages_and_notes() -> None:
    pres = {
        "slides": [
            {
                "pageElements": [_shape("開場")],
                "slideProperties": {"notesPage": {"pageElements": [_shape("記得微笑")]}},
            },
            {"pageElements": []},
        ]
    }
    text, kind = render_presentation(pres)
    assert "【第 1 頁】" in text and "開場" in text
    assert "（講者備註）記得微笑" in text
    assert "【第 2 頁】" in text and "（無文字）" in text
    assert "2 頁" in kind


def test_render_form_questions() -> None:
    form = {
        "info": {"title": "工人登錄", "description": "請如實填寫"},
        "items": [
            {"title": "暱稱", "questionItem": {"question": {"required": True, "textQuestion": {}}}},
            {
                "title": "組別",
                "questionItem": {
                    "question": {
                        "choiceQuestion": {
                            "type": "RADIO",
                            "options": [{"value": "行政組"}, {"value": "議程組"}, {"isOther": True}],
                        }
                    }
                },
            },
            {"title": "第二部分", "pageBreakItem": {}},
        ],
    }
    text, kind = render_form(form)
    assert "工人登錄" in text and "請如實填寫" in text
    assert "1. 暱稱＊｜簡答文字" in text
    assert "單選：行政組、議程組、（其他）" in text
    assert "分頁：第二部分" in text
    assert "表單" in kind


def test_render_apps_script() -> None:
    raw = (
        b'{"files": [{"name": "main", "type": "SERVER_JS", "source": "function x() {}"},'
        b'{"name": "appsscript", "type": "JSON", "source": "{}"}]}'
    )
    text, kind = render_apps_script(raw)
    assert "【main.gs】" in text and "function x() {}" in text
    assert "2 個檔案" in kind


# --------------------------------------------------------------------------- #
# Office（stdlib zip 解析）與 SVG
# --------------------------------------------------------------------------- #
def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def test_extract_docx() -> None:
    data = _zip_bytes(
        {
            "word/document.xml": (
                "<w:document><w:body>"
                "<w:p><w:r><w:t>第一段</w:t></w:r><w:r><w:t xml:space=\"preserve\">，接著</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>第二段 &amp; 符號</w:t></w:r></w:p>"
                "</w:body></w:document>"
            )
        }
    )
    text, kind = extract_docx_text(data)
    assert "第一段，接著" in text
    assert "第二段 & 符號" in text
    assert kind.startswith("Word")


def test_extract_xlsx_shared_strings_and_sheet_names() -> None:
    data = _zip_bytes(
        {
            "xl/workbook.xml": (
                '<workbook><sheets>'
                '<sheet name="名單" sheetId="1" r:id="rId1"/>'
                '<sheet name="統計" sheetId="2" r:id="rId2"/>'
                "</sheets></workbook>"
            ),
            "xl/_rels/workbook.xml.rels": (
                '<Relationships>'
                '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" Target="worksheets/sheet2.xml"/>'
                "</Relationships>"
            ),
            "xl/sharedStrings.xml": "<sst><si><t>姓名</t></si><si><t>小石</t></si></sst>",
            "xl/worksheets/sheet1.xml": (
                '<worksheet><sheetData>'
                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1"><v>42</v></c></row>'
                '<row r="2"><c r="A2" t="s"><v>1</v></c></row>'
                "</sheetData></worksheet>"
            ),
            "xl/worksheets/sheet2.xml": (
                '<worksheet><sheetData>'
                '<row r="1"><c r="A1" t="inlineStr"><is><t>合計</t></is></c></row>'
                "</sheetData></worksheet>"
            ),
        }
    )
    text, kind = extract_xlsx_text(data)
    assert "【工作表：名單】" in text and "姓名\t42" in text and "小石" in text
    assert "【工作表：統計】" in text and "合計" in text
    assert "2 張工作表" in kind


def test_extract_pptx_slides_and_notes() -> None:
    data = _zip_bytes(
        {
            "ppt/slides/slide1.xml": "<p:sld><a:t>封面標題</a:t></p:sld>",
            "ppt/slides/slide2.xml": "<p:sld><a:t>第二頁</a:t></p:sld>",
            "ppt/slides/_rels/slide1.xml.rels": (
                '<Relationships><Relationship Target="../notesSlides/notesSlide1.xml"/></Relationships>'
            ),
            "ppt/notesSlides/notesSlide1.xml": "<p:notes><a:t>備註文字</a:t></p:notes>",
        }
    )
    text, kind = extract_pptx_text(data)
    assert "【第 1 頁】\n封面標題" in text
    assert "（講者備註）備註文字" in text
    assert "【第 2 頁】\n第二頁" in text
    assert "2 頁" in kind


def test_extract_svg_text_nodes() -> None:
    svg = '<svg><style>.a{}</style><text x="1">大廳</text><text><tspan>S 廳</tspan></text></svg>'
    assert extract_svg_text(svg) == "大廳\nS 廳"
    assert extract_svg_text("<svg><path d='M0 0'/></svg>") == ""


# --------------------------------------------------------------------------- #
# DriveContentService：分流、offset 分段、API 未啟用降級
# --------------------------------------------------------------------------- #
MIME_DOC = "application/vnd.google-apps.document"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"
MIME_SLIDES = "application/vnd.google-apps.presentation"
MIME_FORM = "application/vnd.google-apps.form"


class FakeSearch:
    """resolve_for_read／fetch_text 的假件（範圍與（私）判定已在 drive_client 測完）。"""

    def __init__(self, mime: str, private: bool = False) -> None:
        self.meta = DriveFile("f", "SITCON 2027/f", "url", mime, "fid")
        self.private = private
        self.fetch_calls: list[tuple[str, str | None]] = []

    async def resolve_for_read(self, file_id: str) -> tuple[DriveFile, bool]:
        return self.meta, self.private

    async def fetch_text(self, file_id: str, export_mime: str | None) -> str:
        self.fetch_calls.append((file_id, export_mime))
        return "export 後援內容"


class FakeContentGateway:
    def __init__(self) -> None:
        self.doc: dict[str, Any] | Exception = {"body": {"content": [{"paragraph": _para("字" * 30)}]}}
        self.sheet_meta: dict[str, Any] | Exception = SHEET_META
        self.values: dict[str, Any] = {"'總表'": [["a", "b"]], "'行政組(AD)'": [["c"]], "'草稿'": [["d"]]}
        self.pres: dict[str, Any] | Exception = {"slides": [{"pageElements": [_shape("hi")]}]}
        self.form: dict[str, Any] | Exception = {"info": {"title": "表單"}, "items": []}
        self.value_requests: list[str] = []

    async def get_document(self, file_id: str) -> dict[str, Any]:
        if isinstance(self.doc, Exception):
            raise self.doc
        return self.doc

    async def get_spreadsheet(self, file_id: str) -> dict[str, Any]:
        if isinstance(self.sheet_meta, Exception):
            raise self.sheet_meta
        return self.sheet_meta

    async def get_sheet_values(self, file_id: str, a1_range: str) -> list[list[Any]]:
        self.value_requests.append(a1_range)
        got = self.values.get(a1_range, [])
        if isinstance(got, Exception):
            raise got
        return got

    async def get_presentation(self, file_id: str) -> dict[str, Any]:
        if isinstance(self.pres, Exception):
            raise self.pres
        return self.pres

    async def get_form(self, file_id: str) -> dict[str, Any]:
        if isinstance(self.form, Exception):
            raise self.form
        return self.form

    async def export_bytes(self, file_id: str, mime: str) -> bytes:
        return b'{"files": []}'

    async def download_bytes(self, file_id: str) -> bytes:
        return b"plain"


def _svc(mime: str, private: bool = False) -> tuple[DriveContentService, FakeSearch, FakeContentGateway]:
    search = FakeSearch(mime, private)
    gw = FakeContentGateway()
    return DriveContentService(search, gw), search, gw  # type: ignore[arg-type]


async def test_service_reads_document() -> None:
    svc, _, _ = _svc(MIME_DOC)
    content = await svc.read("fid")
    assert content.text == "字" * 30
    assert content.kind == "Google 文件"
    assert content.total_len == 30


async def test_service_reads_all_grid_sheets_skips_non_grid() -> None:
    svc, _, gw = _svc(MIME_SHEET)
    content = await svc.read("fid")
    assert "【工作表：總表】" in content.text
    # 只對 GRID 工作表發值查詢（時間軸 OBJECT 不查）
    assert gw.value_requests == ["'總表'", "'行政組(AD)'", "'草稿'"]


async def test_service_single_worksheet_with_range() -> None:
    svc, _, gw = _svc(MIME_SHEET)
    await svc.read("fid", worksheet="總表", cell_range="A1:B2")
    assert gw.value_requests == ["'總表'!A1:B2"]


async def test_service_unknown_worksheet_lists_names() -> None:
    svc, _, _ = _svc(MIME_SHEET)
    with pytest.raises(DriveReadError, match="總表、行政組"):
        await svc.read("fid", worksheet="沒這張")


async def test_service_offset_paging() -> None:
    svc, _, gw = _svc(MIME_DOC)
    gw.doc = {"body": {"content": [{"paragraph": _para("字" * (CONTENT_LIMIT + 100))}]}}
    first = await svc.read("fid")
    assert first.truncated is True and len(first.text) == CONTENT_LIMIT
    second = await svc.read("fid", offset=CONTENT_LIMIT)
    assert second.offset == CONTENT_LIMIT
    assert len(second.text) == 100
    assert second.truncated is False


async def test_service_slides_fallback_when_api_disabled() -> None:
    svc, search, gw = _svc(MIME_SLIDES)
    gw.pres = ApiDisabledError("Google Slides API", "https://console/x")
    content = await svc.read("fid")
    assert content.text == "export 後援內容"
    assert "Slides API 未啟用" in content.kind
    assert search.fetch_calls == [("fid", "text/plain")]


async def test_service_form_disabled_surfaces_admin_message() -> None:
    svc, _, gw = _svc(MIME_FORM)
    gw.form = ApiDisabledError("Google Forms API", "https://console/forms")
    with pytest.raises(ApiDisabledError, match="Forms API"):
        await svc.read("fid")


async def test_service_plain_text_download() -> None:
    svc, search, _ = _svc("text/markdown")
    content = await svc.read("fid")
    assert content.text == "export 後援內容"
    assert search.fetch_calls == [("fid", None)]


async def test_service_rejects_binary() -> None:
    svc, _, _ = _svc("image/jpeg")
    with pytest.raises(DriveReadError, match="沒有文字內容"):
        await svc.read("fid")


async def test_service_keeps_private_flag() -> None:
    svc, _, _ = _svc(MIME_DOC, private=True)
    content = await svc.read("fid")
    assert content.private is True
