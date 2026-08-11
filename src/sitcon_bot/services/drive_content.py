"""各檔案類型的完整內容擷取（DR-10 2026-08-11 修訂）。

用 bot 的 SA 實掃三個年度資料夾（5000+ 檔）後確認 Drive export 的先天缺陷：
- Google 文件 export text/plain 只含第一個分頁（tab），其他分頁整個漏掉；
- Google 試算表 export text/csv 只含第一張工作表（例：2027 預算表有 17 張，漏 16 張）；
- 簡報 export 沒有講者備註；表單完全無法 export；PDF／Office 檔一律拒讀；
- 捷徑（shortcut，實掃 105 個）沒被處理，指向外部共用硬碟的整包資料（如 2026 議程組）讀不到。

因此各類型改走專屬 API／解析器，全程唯讀：
- 文件       → Docs API documents.get(includeTabsContent) 走訪所有分頁（含巢狀）＋段落＋表格
- 試算表     → Sheets API 列出全部工作表、逐表讀值（「時間軸」等非表格型工作表略過並註明；
               逐表容錯，一張壞不拖垮整本）
- 簡報       → Slides API 逐頁文字＋講者備註；API 未啟用時退回 export text/plain（無備註）
- 表單       → Forms API 題目結構；API 未啟用時明確回報啟用網址
- Apps Script → Drive export application/vnd.google-apps.script+json
- PDF        → pypdf 逐頁抽文字；Word／Excel／PowerPoint → 標準庫 zip＋XML 解析
- 繪圖       → export SVG 抽文字節點（多數繪圖文字已轉曲線，抽不到就請使用者開連結）
- 純文字／JSON／SVG 檔 → 直接下載
- 捷徑       → 由 DriveSearchService.resolve_for_read 解析目標後依目標類型讀取

Docs／Sheets／Slides／Forms API 都接受 drive.readonly scope（SA 不需新授權），但要在 SA
所屬 GCP 專案逐一啟用；未啟用時 Google 回 403 SERVICE_DISABLED，訊息附啟用連結，
本模組把它轉成可直接轉告管理員的中文說明。

渲染器全部是純函式（吃 API 回傳的 dict／bytes、吐字串），單元測試不需打網路。
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import threading
from html import unescape
from typing import Any, Protocol

from .drive_client import (
    CONTENT_LIMIT,
    DriveContent,
    DriveReadError,
    DriveSearchService,
    content_mode,
)
from .google_http import GOOGLE_NUM_RETRIES, build_google_service, request_http

log = logging.getLogger(__name__)

DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

MIME_DOC = "application/vnd.google-apps.document"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"
MIME_SLIDES = "application/vnd.google-apps.presentation"
MIME_FORM = "application/vnd.google-apps.form"
MIME_SCRIPT = "application/vnd.google-apps.script"
MIME_DRAWING = "application/vnd.google-apps.drawing"
MIME_PDF = "application/pdf"
MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
MIME_SVG = "image/svg+xml"

# 全部工作表模式下，單張工作表的顯示上限（避免一張巨表吃光整個回傳額度）
SHEET_ROWS_CAP_ALL = 60
# 指定單張工作表時的列數上限（超過另以 cell_range 縮小）
SHEET_ROWS_CAP_ONE = 2000
_CELL_CHAR_CAP = 300  # 單一儲存格顯示上限
_PDF_PAGES_CAP = 80
_DOWNLOAD_BYTES_CAP = 20 * 1024 * 1024  # 下載解析的檔案大小上限（PDF/Office）


class ApiDisabledError(Exception):
    """對應 Google 403 SERVICE_DISABLED：該 API 尚未在 SA 的 GCP 專案啟用。"""

    def __init__(self, api_label: str, enable_url: str | None) -> None:
        self.api_label = api_label
        self.enable_url = enable_url
        hint = f"啟用網址：{enable_url}" if enable_url else "請在 GCP 主控台啟用後再試。"
        super().__init__(f"{api_label} 尚未在 bot 的 Google Cloud 專案啟用，請管理員啟用（唯讀免改權限）。{hint}")


def _classify_http_error(exc: Exception, api_label: str) -> Exception:
    """把 googleapiclient HttpError 轉成可直接回給 LLM 的例外。"""
    text = str(exc)
    if "SERVICE_DISABLED" in text or "has not been used in project" in text:
        m = re.search(r"https://console\.developers\.google\.com/apis/api/[^\s\"'\\]+", text)
        return ApiDisabledError(api_label, m.group(0) if m else None)
    status = getattr(exc, "status_code", None)
    if status in (403, 404):
        return DriveReadError(f"{api_label} 讀不到這個檔案（沒有權限或不存在）。")
    return exc


# --------------------------------------------------------------------------- #
# Gateway（可注入假物件）
# --------------------------------------------------------------------------- #
class ContentGateway(Protocol):
    async def get_document(self, file_id: str) -> dict[str, Any]: ...
    async def get_spreadsheet(self, file_id: str) -> dict[str, Any]: ...
    async def get_sheet_values(self, file_id: str, a1_range: str) -> list[list[Any]]: ...
    async def get_presentation(self, file_id: str) -> dict[str, Any]: ...
    async def get_form(self, file_id: str) -> dict[str, Any]: ...
    async def export_bytes(self, file_id: str, mime: str) -> bytes: ...
    async def download_bytes(self, file_id: str) -> bytes: ...


class GoogleContentGateway:
    """Docs／Sheets／Slides／Forms／Drive 下載，全部共用同一份 SA 憑證與唯讀 scope。"""

    def __init__(self, sa_json_path: str) -> None:
        self._sa_json_path = sa_json_path
        self._services: dict[str, tuple[Any, Any]] = {}
        self._build_lock = threading.Lock()

    def _svc(self, api: str, version: str) -> tuple[Any, Any]:
        key = f"{api}/{version}"
        with self._build_lock:
            if key not in self._services:
                self._services[key] = build_google_service(api, version, self._sa_json_path, [DRIVE_SCOPE])
            return self._services[key]

    def _execute(self, request: Any, creds: Any, api_label: str) -> Any:
        try:
            return request.execute(http=request_http(creds), num_retries=GOOGLE_NUM_RETRIES)
        except Exception as exc:  # HttpError 轉語意化例外
            raise _classify_http_error(exc, api_label) from exc

    # --- sync 實作（asyncio.to_thread 呼叫） ---
    def _get_document_sync(self, file_id: str) -> dict[str, Any]:
        svc, creds = self._svc("docs", "v1")
        req = svc.documents().get(documentId=file_id, includeTabsContent=True)
        return self._execute(req, creds, "Google Docs API")

    def _get_spreadsheet_sync(self, file_id: str) -> dict[str, Any]:
        svc, creds = self._svc("sheets", "v4")
        req = svc.spreadsheets().get(
            spreadsheetId=file_id,
            fields="properties.title,sheets.properties(title,sheetType,hidden,gridProperties(rowCount,columnCount))",
        )
        return self._execute(req, creds, "Google Sheets API")

    def _get_sheet_values_sync(self, file_id: str, a1_range: str) -> list[list[Any]]:
        svc, creds = self._svc("sheets", "v4")
        req = svc.spreadsheets().values().get(spreadsheetId=file_id, range=a1_range, majorDimension="ROWS")
        resp = self._execute(req, creds, "Google Sheets API")
        return resp.get("values", [])

    def _get_presentation_sync(self, file_id: str) -> dict[str, Any]:
        svc, creds = self._svc("slides", "v1")
        req = svc.presentations().get(presentationId=file_id)
        return self._execute(req, creds, "Google Slides API")

    def _get_form_sync(self, file_id: str) -> dict[str, Any]:
        svc, creds = self._svc("forms", "v1")
        req = svc.forms().get(formId=file_id)
        return self._execute(req, creds, "Google Forms API")

    def _export_bytes_sync(self, file_id: str, mime: str) -> bytes:
        svc, creds = self._svc("drive", "v3")
        req = svc.files().export_media(fileId=file_id, mimeType=mime)
        data = self._execute(req, creds, "Google Drive API")
        return data if isinstance(data, bytes) else str(data).encode("utf-8")

    def _download_bytes_sync(self, file_id: str) -> bytes:
        svc, creds = self._svc("drive", "v3")
        req = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        data = self._execute(req, creds, "Google Drive API")
        return data if isinstance(data, bytes) else str(data).encode("utf-8")

    # --- async 介面 ---
    async def get_document(self, file_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_document_sync, file_id)

    async def get_spreadsheet(self, file_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_spreadsheet_sync, file_id)

    async def get_sheet_values(self, file_id: str, a1_range: str) -> list[list[Any]]:
        return await asyncio.to_thread(self._get_sheet_values_sync, file_id, a1_range)

    async def get_presentation(self, file_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_presentation_sync, file_id)

    async def get_form(self, file_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_form_sync, file_id)

    async def export_bytes(self, file_id: str, mime: str) -> bytes:
        return await asyncio.to_thread(self._export_bytes_sync, file_id, mime)

    async def download_bytes(self, file_id: str) -> bytes:
        return await asyncio.to_thread(self._download_bytes_sync, file_id)


# --------------------------------------------------------------------------- #
# 渲染器（純函式）—— Google 文件
# --------------------------------------------------------------------------- #
def _doc_paragraph_text(paragraph: dict[str, Any]) -> str:
    parts: list[str] = []
    for el in paragraph.get("elements", []):
        run = el.get("textRun")
        if run is not None:
            parts.append(run.get("content", ""))
            continue
        if "inlineObjectElement" in el:
            parts.append("[圖片/物件]")
        elif "richLink" in el:
            props = el.get("richLink", {}).get("richLinkProperties", {})
            parts.append(props.get("title") or props.get("uri") or "[連結]")
        elif "person" in el:
            parts.append(el.get("person", {}).get("personProperties", {}).get("name", "[人名]"))
        elif "footnoteReference" in el:
            parts.append(f"[註{el['footnoteReference'].get('footnoteNumber', '')}]")
    text = "".join(parts)
    if paragraph.get("bullet") is not None:
        text = "• " + text.lstrip()
    return text


def _doc_structural_text(content: list[dict[str, Any]]) -> str:
    """走訪 Docs structuralElements：段落、表格（每列以「｜」相接）、目錄。"""
    out: list[str] = []
    for element in content:
        if "paragraph" in element:
            out.append(_doc_paragraph_text(element["paragraph"]))
        elif "table" in element:
            for row in element["table"].get("tableRows", []):
                cells = [
                    _doc_structural_text(cell.get("content", [])).replace("\n", " ").strip()
                    for cell in row.get("tableCells", [])
                ]
                out.append("｜" + "｜".join(cells) + "｜\n")
        elif "tableOfContents" in element:
            out.append(_doc_structural_text(element["tableOfContents"].get("content", [])))
    return "".join(out)


def _walk_tabs(tabs: list[dict[str, Any]], depth: int = 0) -> list[tuple[str, int, str]]:
    """攤平（含巢狀 childTabs）為 [(標題, 深度, 內文)]。"""
    flat: list[tuple[str, int, str]] = []
    for tab in tabs:
        title = tab.get("tabProperties", {}).get("title", "")
        body = tab.get("documentTab", {}).get("body", {}).get("content", [])
        flat.append((title, depth, _doc_structural_text(body)))
        flat.extend(_walk_tabs(tab.get("childTabs", []), depth + 1))
    return flat


def render_document(doc: dict[str, Any], tab: str | None = None) -> tuple[str, str]:
    """回傳 (內文, 型別說明)。多分頁全部展開；tab 指定時只取標題吻合的分頁。"""
    tabs = doc.get("tabs") or []
    if not tabs:  # 舊格式（無分頁概念）
        return _doc_structural_text(doc.get("body", {}).get("content", [])), "Google 文件"

    flat = _walk_tabs(tabs)
    if tab is not None and tab.strip():
        want = tab.strip().lower()
        picked = [t for t in flat if want in t[0].lower()]
        if not picked:
            names = "、".join(t[0] for t in flat)
            raise DriveReadError(f"這份文件沒有叫「{tab}」的分頁。既有分頁：{names}")
        flat = picked

    if len(flat) == 1 and tab is None:
        return flat[0][2], "Google 文件"
    parts = [f"{'#' * (d + 1)}【分頁：{title or '(未命名)'}】\n{text}" for title, d, text in flat]
    if tab is None:
        label = f"Google 文件（{len(flat)} 個分頁）"
    else:
        label = f"Google 文件（分頁：{'、'.join(t[0] for t in flat)}）"
    return "\n\n".join(parts), label


# --------------------------------------------------------------------------- #
# 渲染器 —— 試算表
# --------------------------------------------------------------------------- #
def _sheet_row_text(row: list[Any]) -> str:
    cells = [str(c) for c in row]
    while cells and not cells[-1].strip():  # 去尾端空欄
        cells.pop()
    cells = [c if len(c) <= _CELL_CHAR_CAP else c[:_CELL_CHAR_CAP] + "…" for c in cells]
    return "\t".join(cells)


def a1_quote(title: str) -> str:
    """工作表名轉 A1 引號形式（內部單引號成對跳脫）。"""
    return "'" + title.replace("'", "''") + "'"


def render_spreadsheet(
    meta: dict[str, Any],
    values_by_title: dict[str, list[list[Any]] | Exception],
    *,
    single: str | None = None,
) -> tuple[str, str]:
    """回傳 (內文, 型別說明)。meta 為 spreadsheets.get 的回傳；values 逐表容錯。

    single 給定時代表使用者指定只看那張工作表（列數上限較寬）。
    """
    doc_title = meta.get("properties", {}).get("title", "")
    props = [s.get("properties", {}) for s in meta.get("sheets", [])]
    labels: list[str] = []
    for p in props:
        tag = p.get("title", "")
        if p.get("sheetType") and p["sheetType"] != "GRID":
            tag += "（非表格）"
        if p.get("hidden"):
            tag += "（隱藏）"
        labels.append(tag)
    header = f"試算表「{doc_title}」共 {len(props)} 張工作表：{'、'.join(labels)}"

    rows_cap = SHEET_ROWS_CAP_ONE if single else SHEET_ROWS_CAP_ALL
    blocks: list[str] = []
    for p in props:
        title = p.get("title", "")
        if title not in values_by_title:
            continue
        got = values_by_title[title]
        if isinstance(got, ApiDisabledError):
            raise got
        if isinstance(got, Exception):
            blocks.append(f"【工作表：{title}】\n（讀取失敗：{got}）")
            continue
        rows = [r for r in got if any(str(c).strip() for c in r)]
        shown = rows[:rows_cap]
        body = "\n".join(_sheet_row_text(r) for r in shown) or "（空）"
        note = ""
        if len(rows) > len(shown):
            note = (
                f"\n…（另 {len(rows) - len(shown)} 列未顯示；"
                f"要看整張請用 drive_read_sheet 指定 worksheet=\"{title}\"，或加 cell_range 縮小範圍）"
            )
        blocks.append(f"【工作表：{title}】（{len(rows)} 列有資料）\n{body}{note}")

    kind = f"Google 試算表（{len(props)} 張工作表）"
    return header + "\n\n" + "\n\n".join(blocks), kind


# --------------------------------------------------------------------------- #
# 渲染器 —— 簡報
# --------------------------------------------------------------------------- #
def _slides_elements_text(elements: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for el in elements:
        shape = el.get("shape")
        if shape is not None:
            runs = [
                te.get("textRun", {}).get("content", "")
                for te in shape.get("text", {}).get("textElements", [])
            ]
            text = "".join(runs).strip()
            if text:
                out.append(text)
        table = el.get("table")
        if table is not None:
            for row in table.get("tableRows", []):
                cells: list[str] = []
                for cell in row.get("tableCells", []):
                    runs = [
                        te.get("textRun", {}).get("content", "")
                        for te in cell.get("text", {}).get("textElements", [])
                    ]
                    cells.append("".join(runs).replace("\n", " ").strip())
                if any(cells):
                    out.append("｜" + "｜".join(cells) + "｜")
        group = el.get("elementGroup")
        if group is not None:
            out.extend(_slides_elements_text(group.get("children", [])))
    return out


def render_presentation(pres: dict[str, Any]) -> tuple[str, str]:
    slides = pres.get("slides", [])
    blocks: list[str] = []
    for i, slide in enumerate(slides, 1):
        lines = _slides_elements_text(slide.get("pageElements", []))
        notes_page = slide.get("slideProperties", {}).get("notesPage", {})
        notes = _slides_elements_text(notes_page.get("pageElements", []))
        block = f"【第 {i} 頁】\n" + ("\n".join(lines) if lines else "（無文字）")
        if notes:
            block += "\n（講者備註）" + " / ".join(notes)
        blocks.append(block)
    return "\n\n".join(blocks), f"Google 簡報（{len(slides)} 頁，含講者備註）"


# --------------------------------------------------------------------------- #
# 渲染器 —— 表單
# --------------------------------------------------------------------------- #
def _form_question_desc(question: dict[str, Any]) -> str:
    if "choiceQuestion" in question:
        cq = question["choiceQuestion"]
        kind = {"RADIO": "單選", "CHECKBOX": "多選", "DROP_DOWN": "下拉"}.get(cq.get("type", ""), "選擇")
        opts = "、".join(o.get("value", "") or ("（其他）" if o.get("isOther") else "") for o in cq.get("options", []))
        return f"{kind}：{opts}"
    if "textQuestion" in question:
        return "長文字" if question["textQuestion"].get("paragraph") else "簡答文字"
    if "scaleQuestion" in question:
        sq = question["scaleQuestion"]
        return f"量表 {sq.get('low')}–{sq.get('high')}（{sq.get('lowLabel', '')}→{sq.get('highLabel', '')}）"
    if "dateQuestion" in question:
        return "日期"
    if "timeQuestion" in question:
        return "時間"
    if "fileUploadQuestion" in question:
        return "檔案上傳"
    if "ratingQuestion" in question:
        return "評分"
    return "（未知題型）"


def render_form(form: dict[str, Any]) -> tuple[str, str]:
    info = form.get("info", {})
    lines = [f"表單標題：{info.get('title', '')}"]
    if info.get("description"):
        lines.append(f"說明：{info['description']}")
    n_q = 0
    for item in form.get("items", []):
        title = item.get("title", "")
        if "questionItem" in item:
            n_q += 1
            q = item["questionItem"].get("question", {})
            req = "＊" if q.get("required") else ""
            lines.append(f"{n_q}. {title}{req}｜{_form_question_desc(q)}")
        elif "questionGroupItem" in item:
            rows = item["questionGroupItem"].get("questions", [])
            n_q += 1
            row_titles = "、".join(r.get("rowQuestion", {}).get("title", "") for r in rows)
            lines.append(f"{n_q}. {title}（題組：{row_titles}）")
        elif "pageBreakItem" in item:
            lines.append(f"—— 分頁：{title} ——")
        elif "textItem" in item:
            lines.append(f"（說明文字）{title}")
        elif "imageItem" in item or "videoItem" in item:
            lines.append(f"（媒體）{title}")
    lines.append(f"（共 {n_q} 題；此為題目結構，回覆內容存在連結的回應試算表）")
    return "\n".join(lines), "Google 表單（題目結構）"


# --------------------------------------------------------------------------- #
# 渲染器 —— Apps Script／PDF／Office／SVG
# --------------------------------------------------------------------------- #
def render_apps_script(raw: bytes) -> tuple[str, str]:
    proj = json.loads(raw.decode("utf-8", errors="replace"))
    files = proj.get("files", [])
    blocks = [
        f"【{f.get('name', '')}.{'gs' if f.get('type') == 'SERVER_JS' else str(f.get('type', '')).lower()}】\n"
        + (f.get("source", "") or "（空）")
        for f in files
    ]
    return "\n\n".join(blocks), f"Apps Script（{len(files)} 個檔案）"


def extract_pdf_text(data: bytes) -> tuple[str, str]:
    from pypdf import PdfReader  # 延遲載入：僅在讀 PDF 時需要

    reader = PdfReader(io.BytesIO(data))
    pages = reader.pages[:_PDF_PAGES_CAP]
    blocks: list[str] = []
    for i, page in enumerate(pages, 1):
        try:
            text = (page.extract_text() or "").strip()
        except Exception:  # 個別頁面解析失敗不拖垮整份
            text = "（此頁解析失敗）"
        blocks.append(f"【第 {i} 頁】\n{text or '（此頁無可擷取文字）'}")
    total = len(reader.pages)
    if total > len(pages):
        blocks.append(f"…（僅解析前 {len(pages)} 頁，共 {total} 頁）")
    body = "\n\n".join(blocks)
    if not any(p.extract_text() for p in pages[:5] if p):
        pass  # 掃描檔判斷交由上面每頁訊息呈現
    return body, f"PDF（{total} 頁）"


_XML_TAG = re.compile(r"<[^>]+>")


def _xml_texts(xml: str, tag: str) -> list[str]:
    """抽 <tag ...>text</tag> 的內文（含實體解碼）。"""
    return [unescape(m) for m in re.findall(rf"<{tag}[^>]*>([^<]*)</{tag}>", xml)]


def extract_docx_text(data: bytes) -> tuple[str, str]:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        xml = zf.read("word/document.xml").decode("utf-8", errors="replace")
    paragraphs = re.split(r"</w:p>", xml)
    lines = ["".join(_xml_texts(p, "w:t")) for p in paragraphs]
    return "\n".join(line for line in lines if line.strip()), "Word 文件（docx）"


def _col_index(ref: str) -> int:
    """A1 欄字母 → 0-based index（'B2' → 1）。"""
    n = 0
    for ch in ref:
        if ch.isalpha():
            n = n * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            break
    return max(n - 1, 0)


def extract_xlsx_text(data: bytes) -> tuple[str, str]:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        shared: list[str] = []
        if "xl/sharedStrings.xml" in names:
            sxml = zf.read("xl/sharedStrings.xml").decode("utf-8", errors="replace")
            shared = ["".join(_xml_texts(si, "t")) for si in re.findall(r"<si>(.*?)</si>", sxml, re.S)]

        # 工作表名稱與檔案對應（workbook.xml + rels）
        wb = zf.read("xl/workbook.xml").decode("utf-8", errors="replace")
        rels = zf.read("xl/_rels/workbook.xml.rels").decode("utf-8", errors="replace")
        rel_map = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
        sheets = re.findall(r'<sheet[^>]*name="([^"]*)"[^>]*r:id="([^"]*)"', wb)

        blocks: list[str] = []
        for title, rid in sheets:
            target = rel_map.get(rid, "")
            path = "xl/" + target.lstrip("/") if not target.startswith("xl/") else target
            if path not in names:
                continue
            xml = zf.read(path).decode("utf-8", errors="replace")
            rows_out: list[str] = []
            for row_xml in re.findall(r"<row[^>]*>(.*?)</row>", xml, re.S):
                cells: dict[int, str] = {}
                for c_attrs, c_body in re.findall(r"<c([^>]*)>(.*?)</c>", row_xml, re.S):
                    ref = re.search(r'r="([A-Z]+)\d+"', c_attrs)
                    idx = _col_index(ref.group(1)) if ref else len(cells)
                    t_attr = re.search(r't="([^"]+)"', c_attrs)
                    v = re.search(r"<v>([^<]*)</v>", c_body)
                    if t_attr and t_attr.group(1) == "s" and v:
                        val = shared[int(v.group(1))] if int(v.group(1)) < len(shared) else ""
                    elif t_attr and t_attr.group(1) == "inlineStr":
                        val = "".join(_xml_texts(c_body, "t"))
                    else:
                        val = unescape(v.group(1)) if v else ""
                    cells[idx] = val
                if any(v.strip() for v in cells.values()):
                    width = max(cells) + 1
                    rows_out.append(_sheet_row_text([cells.get(i, "") for i in range(width)]))
            shown = rows_out[:SHEET_ROWS_CAP_ONE]
            more = f"\n…（另 {len(rows_out) - len(shown)} 列未顯示）" if len(rows_out) > len(shown) else ""
            blocks.append(f"【工作表：{unescape(title)}】（{len(rows_out)} 列有資料）\n" + "\n".join(shown) + more)
    return "\n\n".join(blocks), f"Excel（{len(blocks)} 張工作表）"


def extract_pptx_text(data: bytes) -> tuple[str, str]:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        names = set(zf.namelist())
        slide_paths = sorted(
            (n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
            key=lambda n: int(re.search(r"(\d+)", n).group(1)),  # type: ignore[union-attr]
        )
        blocks: list[str] = []
        for i, path in enumerate(slide_paths, 1):
            xml = zf.read(path).decode("utf-8", errors="replace")
            texts = [t for t in _xml_texts(xml, "a:t")]
            block = f"【第 {i} 頁】\n" + ("".join(texts) or "（無文字）")
            # 講者備註：slideN.xml.rels → notesSlideM.xml
            rels_path = f"ppt/slides/_rels/slide{re.search(r'(\d+)', path).group(1)}.xml.rels"  # type: ignore[union-attr]
            if rels_path in names:
                rels = zf.read(rels_path).decode("utf-8", errors="replace")
                m = re.search(r'Target="\.\./(notesSlides/notesSlide\d+\.xml)"', rels)
                if m and f"ppt/{m.group(1)}" in names:
                    nxml = zf.read(f"ppt/{m.group(1)}").decode("utf-8", errors="replace")
                    notes = "".join(_xml_texts(nxml, "a:t")).strip()
                    if notes:
                        block += f"\n（講者備註）{notes}"
            blocks.append(block)
    return "\n\n".join(blocks), f"PowerPoint（{len(slide_paths)} 頁）"


def extract_svg_text(svg: str) -> str:
    body = re.sub(r"<(style|script)[^>]*>.*?</\1>", "", svg, flags=re.S)
    texts = [unescape(t).strip() for t in re.findall(r"<(?:text|tspan)[^>]*>([^<]+)<", body)]
    return "\n".join(t for t in texts if t)


# --------------------------------------------------------------------------- #
# 讀取服務：解析範圍/捷徑 → 依類型分流 → offset 分段
# --------------------------------------------------------------------------- #
class DriveContentService:
    """drive_read_file／drive_read_sheet／drive_read_doc 背後的完整讀取器。

    範圍檢查、（私）標記與捷徑解析委由 DriveSearchService.resolve_for_read（DR-1／DR-4），
    本類別只負責「拿到內容」；所有路徑一律唯讀（DR-8）。
    """

    def __init__(self, search: DriveSearchService, gateway: ContentGateway) -> None:
        self._search = search
        self._gateway = gateway

    async def read(
        self,
        file_id: str,
        *,
        worksheet: str | None = None,
        cell_range: str | None = None,
        tab: str | None = None,
        offset: int = 0,
    ) -> DriveContent:
        meta, private = await self._search.resolve_for_read(file_id)
        text, kind = await self._extract(meta.file_id, meta.mime, worksheet=worksheet, cell_range=cell_range, tab=tab)
        text = text.strip("\n")
        total = len(text)
        offset = max(0, min(offset, total))
        page = text[offset : offset + CONTENT_LIMIT]
        return DriveContent(
            file=meta,
            text=page,
            truncated=(offset + CONTENT_LIMIT) < total,
            private=private,
            kind=kind,
            offset=offset,
            total_len=total,
        )

    async def _extract(
        self,
        file_id: str,
        mime: str | None,
        *,
        worksheet: str | None,
        cell_range: str | None,
        tab: str | None,
    ) -> tuple[str, str]:
        if mime == MIME_DOC:
            return await self._extract_document(file_id, tab)
        if mime == MIME_SHEET:
            return await self._extract_spreadsheet(file_id, worksheet, cell_range)
        if mime == MIME_SLIDES:
            return await self._extract_presentation(file_id)
        if mime == MIME_FORM:
            return render_form(await self._gateway.get_form(file_id))
        if mime == MIME_SCRIPT:
            return render_apps_script(
                await self._gateway.export_bytes(file_id, "application/vnd.google-apps.script+json")
            )
        if mime == MIME_DRAWING:
            svg = (await self._gateway.export_bytes(file_id, MIME_SVG)).decode("utf-8", errors="replace")
            text = extract_svg_text(svg)
            return (
                text or "（這張繪圖的文字已轉成向量圖形，抽不出文字；要看內容請開啟連結。）",
                "Google 繪圖",
            )
        if mime == MIME_PDF:
            return extract_pdf_text(await self._download(file_id))
        if mime == MIME_DOCX:
            return extract_docx_text(await self._download(file_id))
        if mime == MIME_XLSX:
            return extract_xlsx_text(await self._download(file_id))
        if mime == MIME_PPTX:
            return extract_pptx_text(await self._download(file_id))
        if mime == MIME_SVG:
            raw = (await self._download(file_id)).decode("utf-8", errors="replace")
            return (extract_svg_text(raw) or raw[:CONTENT_LIMIT]), "SVG"
        mode = content_mode(mime)
        if mode is not None:  # 純文字類（text/*、json、yaml、xml）與 Google 文件類 export 後援
            text = await self._search.fetch_text(file_id, mode[1])
            return text, "純文字"
        raise DriveReadError(
            f"這個檔案類型（{mime or '未知'}）沒有文字內容可讀（圖片、影音等請直接開連結檢視）。"
        )

    async def _download(self, file_id: str) -> bytes:
        data = await self._gateway.download_bytes(file_id)
        if len(data) > _DOWNLOAD_BYTES_CAP:
            raise DriveReadError("檔案太大（超過 20MB），不解析內容；請直接開連結。")
        return data

    async def _extract_presentation(self, file_id: str) -> tuple[str, str]:
        try:
            return render_presentation(await self._gateway.get_presentation(file_id))
        except ApiDisabledError as exc:
            # Slides API 沒開時退回 export（有全部頁面文字、無講者備註）
            log.warning("Slides API 未啟用，退回 export：%s", exc)
            text = await self._search.fetch_text(file_id, "text/plain")
            return text, "Google 簡報（Slides API 未啟用：純文字、無講者備註）"

    async def _extract_document(self, file_id: str, tab: str | None) -> tuple[str, str]:
        try:
            return render_document(await self._gateway.get_document(file_id), tab)
        except ApiDisabledError as exc:
            # Docs API 沒開時退回 export（只含第一分頁）——仍可用，但註明缺陷
            log.warning("Docs API 未啟用，退回 export：%s", exc)
            text = await self._search.fetch_text(file_id, "text/plain")
            return text, "Google 文件（Docs API 未啟用：僅含第一個分頁，表格會失去欄位結構）"

    async def _extract_spreadsheet(
        self, file_id: str, worksheet: str | None, cell_range: str | None
    ) -> tuple[str, str]:
        try:
            meta = await self._gateway.get_spreadsheet(file_id)
        except ApiDisabledError as exc:
            log.warning("Sheets API 未啟用，退回 export：%s", exc)
            text = await self._search.fetch_text(file_id, "text/csv")
            return text, "Google 試算表（Sheets API 未啟用：僅含第一張工作表）"

        props = [s.get("properties", {}) for s in meta.get("sheets", [])]
        grid_titles = [p.get("title", "") for p in props if p.get("sheetType", "GRID") == "GRID"]
        if worksheet is not None and worksheet.strip():
            want = worksheet.strip()
            picked = [t for t in grid_titles if t == want] or [
                t for t in grid_titles if want.lower() in t.lower()
            ]
            if not picked:
                raise DriveReadError(f"沒有叫「{want}」的工作表。既有工作表：{'、'.join(grid_titles)}")
            targets = picked[:1]
        else:
            targets = grid_titles

        async def fetch(title: str) -> list[list[Any]] | Exception:
            a1 = a1_quote(title) + (f"!{cell_range.strip()}" if cell_range and cell_range.strip() else "")
            try:
                return await self._gateway.get_sheet_values(file_id, a1)
            except Exception as exc:  # 逐表容錯：一張讀不到不拖垮整本
                return exc

        results = await asyncio.gather(*(fetch(t) for t in targets))
        values = dict(zip(targets, results, strict=True))
        single = targets[0] if (worksheet is not None and worksheet.strip()) else None
        return render_spreadsheet(meta, values, single=single)


def build_drive_content_service(sa_json_path: str, search: DriveSearchService) -> DriveContentService:
    return DriveContentService(search, GoogleContentGateway(sa_json_path))
