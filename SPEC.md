# SPEC.md — 小石（SITCON 2027 工作人員 AI 助理 Telegram Bot）

> 版本：1.0（2026-07-23）
> 本文件描述「要做什麼」。實作方式見 `AGENTS.md`。
> 需求編號規則：`AUTH`（授權）、`TRIG`(觸發互動)、`GL`（GitLab）、`DR`（Drive）、`HM`（HackMD）、`RO`（名冊）、`LOG`（稽核）、`NFR`（非功能）、`EC`（邊界條件）。每條需求皆可寫成測試案例。

---

## 1. 專案背景與目的

SITCON 2027 籌備團隊以三個系統協作：GitLab（任務卡片）、Google 共用雲端硬碟（檔案）、HackMD（會議與文件）。工作人員多在 Telegram 群組溝通，切換系統操作卡片、找檔案、開會議記錄的摩擦成本高。

本專案建立一隻名為「小石」的 Telegram bot，讓被授權群組的工作人員以**自然語言（中英文混用）**完成：

1. GitLab 卡片的建立、編輯、留言、查詢（含自動判斷組別並指派組長）。
2. Google 共用雲端硬碟的檔案搜尋（bot 可讀檔案內容以判斷哪份才對，但回覆只給 metadata，絕不外流內容）。
3. HackMD 筆記的建立（含會議模板與自動歸檔）、搜尋、編輯。

**成功的定義**：SITCON 2027 全部功能需求（第 7～10 章）通過驗收測試；工作人員在群組內用一句話即可完成上述操作，錯誤時獲得明確可行動的錯誤訊息；名冊個資與 Drive 檔案內容零外洩。

---

## 2. 名詞定義

| 名詞 | 定義 |
|---|---|
| 小石 | 本 bot 的名稱與觸發詞。 |
| 超級管理員 | 唯一的 bot 擁有者，Telegram 數字 user ID 設定於環境變數。 |
| 授權群組 | 經超級管理員以 `/authorize` 授權的 Telegram 群組。 |
| 目標專案 | GitLab 專案 `sitcon-tw/2027`（gitlab.com）。本系統唯一操作的專案。 |
| 組別 | GitLab 上以 scoped label `Team::<組名>` 表示的 12 個組（場務、活動、總召、紀錄、編輯、行銷、行政、製播、議程、設計、財務、開發）。以 API 動態讀取為準，非硬編碼。 |
| 狀態 label | Scoped label `Status::Inbox / Waiting / Doing / Review / To Do`。卡片狀態**只**以 label 管理，bot 不執行 open/close。 |
| 籌會 label | 格式「`MMDD 第N籌`」或「`MMDD 站立會議`」的一般 label（例：`0913 一籌`、`0110 站立會議`），表示卡片要在哪場會議報告。 |
| 開著的卡 | GitLab state 為 `opened` **且**未帶 `Status::Review` 的卡。（依客戶定義「Status::Review 以外」，並排除已被籌會關閉者。） |
| 名冊 | Google Sheet（ID 設於環境變數）中指定分頁（英文欄名分頁，gid 設於環境變數）。 |
| 職掌文件 | `config/team_charter.md`，描述各組職責範圍，供組別判斷使用。由客戶另行產出，格式為自由 markdown、各組一節。 |
| 組長 | 名冊中 `position == "組長"` 且 `role == <該組>` 的成員。 |
| 總召 | 名冊中 `position == "總召"` 的成員（可能多位，全部視為總召）。 |
| service account 模式 | 三個外部服務均以單一憑證操作（GitLab PAT、Google service account、HackMD 個人 token），所有動作在外部系統上顯示為 bot 帳號。 |

---

## 3. 系統範圍總覽

- 平台：Telegram（僅群組，不支援私訊）。
- 整合：GitLab REST API、Google Drive API、Google Sheets API、HackMD API v1。三個功能域彼此獨立，無跨系統自動化流程。
- AI：單一 LLM（預設 Claude Sonnet 4.6，high thinking），provider／model／thinking 等級由 `.env` 設定，支援 Anthropic 格式與 OpenAI 相容格式（OpenAI、OpenRouter）。
- 部署：客戶 VPS，docker compose，長時運行。

---

## 4. 使用者角色（Actor）與使用情境（Use Case）

### 4.1 Actor

| Actor | 說明 | 權限 |
|---|---|---|
| 超級管理員 | 單一人，數字 Telegram user ID 寫於 `.env` | 全部功能＋管理指令（`/authorize`、`/revoke`、`/list_groups`、`/reload`） |
| 授權群組成員 | 授權群組內任何 Telegram 使用者 | 全部業務功能（GitLab／Drive／HackMD），無管理指令 |
| 未授權者 | 未授權群組的成員、私訊者 | 無任何功能；bot 保持沉默（例外見 AUTH-4） |

### 4.2 代表性 Use Case（非窮舉，作為驗收情境）

- UC-1 開卡自動分派：「小石 幫我開一張卡：官網首頁的倒數計時器壞了」→ bot 建卡、AI 判斷為開發組、上 `Team::開發組`＋`Status::Inbox`、assign 開發組組長、回覆卡號與連結。
- UC-2 指定細節開卡：「@bot 開一張卡給行政組，標題場地保證金匯款，due 8/15，assign 給 Yuan 跟 Leaf」→ 依指定內容建卡（不觸發自動判斷）。
- UC-3 編輯卡片：「小石 把 #42 的狀態改成 Doing，然後加上 0913 一籌」→ 更新 labels（scoped 互斥處理）。
- UC-4 留言：「小石 在 #42 留言：場地已確認，等發票」→ 新增 comment。
- UC-5 摘要討論：「小石 #42 底下討論到哪了」→ 讀取 comments 並摘要。
- UC-6 條件查詢：「小石 列出行政組還開著的卡」→ 依「開著」定義過濾並列出。
- UC-7 連結速查：「小石 給我倒數計時器那張卡的連結」→ 模糊比對，唯一命中回連結。
- UC-8 找檔案：「小石 幫我找去年的場地租借合約」→ 於 SITCON 2026／2027 範圍搜尋，回傳檔名＋路徑＋連結。
- UC-9 開會議記錄（大籌）:「小石 開一份 0913 一籌的會議記錄」→ 以大籌模板建立於「籌會文件」資料夾，上對應 tags，回連結。
- UC-10 開會議記錄（組會）：「小石 幫行政組開今天的會議記錄」→ 以組會模板建立於 `行政組/會議文件`，回連結。
- UC-11 找筆記：「小石 找上次討論贊助方案的那份文件」→ 兩階段搜尋，回標題＋連結。
- UC-12 編輯筆記：「小石 在 0913 一籌的記錄最後加上：散會時間 22:10」→ 定位筆記、修改內容、寫回。
- UC-13 群組授權：超管在新群組輸入 `/authorize` → 群組進入授權清單。

---

## 5. 觸發與互動模型

- **TRIG-1** bot 僅處理符合下列任一條件的群組訊息：(a) 訊息內 @mention bot username；(b) 訊息是對 bot 訊息的 reply；(c) 訊息文字（去除前導空白）以「小石」開頭；(d) 以 `/` 開頭的指令。其餘訊息一律當場丟棄，**不得**寫入任何儲存、不得送往 LLM。
- **TRIG-2** 為支援 TRIG-1(c)，Telegram bot 的 group privacy mode 必須關閉（部署前置作業）。bot 程式會收到授權群組所有訊息，過濾僅在本機記憶體進行。
- **TRIG-3** bot 對觸發訊息以繁體中文（zh-TW）回覆；使用者以英文觸發時以英文回覆。
- **TRIG-4** 對話脈絡：bot 對每個（群組, topic thread）保留最近的觸發互動（雙方訊息），供指代（「把它改成…」）使用；脈絡存活 30 分鐘或最近 10 輪，超過即失效。非觸發訊息不進入脈絡。（已回報並經客戶同意改為**純 reply-chain 模型**：一訊息一 session，脈絡只沿「回覆」傳遞——回覆 ask_user 問句→續答；**回覆小石的一般回覆→以該回合完整 transcript（含工具呼叫與結果）續接**（2026-08-11 修訂：先前只帶被回覆訊息文字，追問會失去上一輪查到的內容）；回覆他人訊息或 transcript 已失效（TTL 30 分鐘／重啟）→ 只帶被回覆訊息文字當脈絡。同一則回覆可被多人、多次各自接續。）
- **TRIG-5** 在 Telegram forum（topics）群組中，bot 於觸發訊息所在的 topic thread 內回覆。
- **TRIG-6** 破壞性以外的所有操作（本系統僅有 create／edit／comment）**不需**執行前確認，解析完成即執行，執行後回報結果摘要（動作、目標、連結、變更欄位）。
- **TRIG-7** 唯一允許的反問時機：指令歧義導致**無法執行**時（例：模糊比對命中多張卡、人名對到多人、會議類型無法判斷），bot 以單一問題列出候選讓使用者選擇，選擇後續接原指令執行。
- **TRIG-8** 超過 Telegram 單訊息長度上限的回覆須自動分段送出，不得截斷。
- **TRIG-9** bot 處理期間須送出 typing 狀態指示。
- **TRIG-10** bot 可對觸發訊息按 ❤ 愛心 reaction（react_heart 工具），何時按由 LLM 自行判斷（道謝、好消息、值得鼓勵等）；按了即取代完成時的 ✅（Telegram bot 一則訊息僅能掛一個 reaction）。（2026-08-06 追加）

---

## 6. 授權模型

- **AUTH-1** 超級管理員以 `.env` 中的數字 Telegram user ID 判定，全系統唯一。
- **AUTH-2** `/authorize`：僅超管、僅群組內可用；將該群組（chat_id）寫入授權清單並持久化。已授權群組重複執行回覆「已授權」。
- **AUTH-3** `/revoke`：僅超管、僅群組內可用；移除該群組授權。
- **AUTH-4** 未授權群組中：bot 對所有訊息保持沉默，唯一例外是超管輸入的 `/authorize`。
- **AUTH-5** 私訊：一律回覆固定訊息「小石僅在授權群組內提供服務」，不執行任何功能。
- **AUTH-6** `/list_groups`：僅超管可用，列出目前授權群組（名稱＋chat_id）。
- **AUTH-7** `/reload`：僅超管可用，清空並重載所有快取（名冊、label、HackMD 資料夾與筆記清單、Drive 資料夾樹、職掌文件）。
- **AUTH-8** `/help`：授權群組內任何人可用，列出功能簡介與範例句。
- **AUTH-9** 授權清單持久化於本機資料庫（volume），容器重啟不失效。
- **AUTH-11** `/notify_on`、`/notify_off`、`/notify_list`、`/notify_test`：僅超管、僅已授權群組內可用（里程碑預告設定，見 10.4）。
- **AUTH-10** 身分比對（超管判定、名冊對應）一律使用 Telegram 數字 user ID；username 僅作顯示與名冊輔助對照。

---

## 7. 功能需求 — GitLab

作用範圍限定 `sitcon-tw/2027` 單一專案。所有寫入動作完成後回覆卡片 IID 與完整 URL。

### 7.1 建立卡片

- **GL-1** 支援以自然語言建卡；標題必要（無法從語句萃取時反問），描述由 AI 將使用者敘述整理為簡潔 markdown。
- **GL-2** 使用者未指定組別時，AI 依（a）`Team::` label 名稱、（b）職掌文件內容，判斷任務所屬組別，加上對應 `Team::<組名>` label，並將該組**組長**（名冊查得）加入 assignees。
- **GL-3** 組別判斷無法得出唯一結論時，落到 fallback：加 `Team::總召組` label、assign 全部總召。
- **GL-4** 使用者明示 label 或 assignee 時，以使用者指定為準，不觸發 GL-2 自動判斷（部分指定時，未指定的部分仍自動補：例如只指定 assignee 未指定組別，組別仍自動判斷）。
- **GL-5** 新卡未指定狀態時預設加 `Status::Inbox`；使用者可口頭指定其他 `Status::` 值。
- **GL-6** 支援同時指定多位 assignee（GitLab 方案已確認支援多重指派）；建卡後以 API 回傳值核對實際套用名單，若有落差須在回覆中明列。
- **GL-7** 支援於建卡時指定 due date（含自然語言日期：「下週五」「8/15」，以 Asia/Taipei 解析）。
- **GL-8** 建卡描述末端附註來源列：`> _via 小石 · requested by @<telegram_username> (<telegram_user_id>)_`。
- **GL-9** 支援於建卡時指定籌會 label（含相對指涉：「下次籌會」→ 以現有籌會 label 的 MMDD 與今日日期解析出最近的未來場次；無法唯一解析時反問）。

### 7.2 label 規則

- **GL-10** 【硬性】**卡片操作**（建卡／編輯）只能使用專案**既有** label，不得隱式補建；送出前逐一比對白名單。
  【2026-08-02 追加需求修訂】label 本身的管理開放為獨立操作（見 7.2.1）；卡片流程仍不得因未知 label 而自動建立。
- **GL-11** label 白名單以 API 動態讀取（含分頁，全量），快取 TTL 10 分鐘，`/reload` 可強制刷新。
- **GL-12** 使用者提及的 label 經正規化（全半形、空白、大小寫）後仍無法精確對應時，回覆錯誤並列出至多 5 個名稱最接近的既有 label 供選擇，該次變更不執行。
- **GL-13** Scoped label 互斥由 bot 端保證：套用 `Status::X` 或 `Team::X` 前，先自最終 label 集合移除同 scope 其他值，再整組寫回。

### 7.2.1 label 管理（2026-08-02 追加需求）

- **GL-24** 支援 label 本身的新增／編輯（改名、換色、改描述）／刪除；每次異動後強制刷新白名單（不等 TTL）。
- **GL-25** 新增時正規化後同名視為已存在、拒絕重複建立；編輯／刪除目標經 GL-12 正規化解析，無對應時回近似候選。
- **GL-26** 刪除 label 為破壞性操作：僅在使用者明確指名該 label 時執行，回覆需提醒「已同時自所有卡片移除」。

### 7.3 編輯卡片

- **GL-14** 支援編輯：title、description、labels（增／減／換）、assignees（增／減／換，多位）、due date（設定／清除）。
- **GL-15** 目標卡片辨識依序支援：(a) `#<IID>` 或卡片 URL；(b) 標題模糊比對；(c) 對話脈絡指代。模糊比對命中多張時依 TRIG-7 反問（列出 IID＋標題，至多 5 筆）。
- **GL-16** 【硬性】bot 不得呼叫任何 state 變更（close／reopen）與刪除 API。
- **GL-17** 編輯完成回覆變更前後摘要（僅列出實際變動的欄位）。

### 7.4 留言

- **GL-18** 支援在指定卡片新增留言；留言末端附 GL-8 同款來源列。
- **GL-19** 支援讀取指定卡片的留言並依要求摘要或列出（過濾系統自動訊息，僅取人工留言）。

### 7.5 查詢

- **GL-20** 連結速查：以 IID 或標題模糊比對取得單一卡片時，回覆標題＋狀態 label＋assignees＋URL。
- **GL-21** 自然語言條件查詢，支援的過濾條件：組別（`Team::`）、狀態（`Status::`）、籌會 label、assignee、標題關鍵字、開著／全部。條件可組合。
- **GL-22** 「開著」採第 2 章定義（state opened 且無 `Status::Review`）。
- **GL-23** 查詢結果每筆呈現：`#IID 標題｜Status｜assignees｜URL`；預設至多 10 筆並註明總數，使用者可要求下一批。

---

## 8. 功能需求 — Google Drive（唯讀搜尋）

- **DR-1** 搜尋範圍限共用雲端硬碟（ID 設於 `.env`，現值 `0AIx9UW7aBiDgUk9PVA`）內名為「SITCON 2027」與「SITCON 2026」的兩個資料夾之全部子孫層。範圍資料夾名單設於 `.env`。（2026-08-11 修訂：現值已含 SITCON 2025，共三個年度資料夾；另**範圍內「指向資料夾的捷徑」其目標子樹視同範圍內**——實掃發現整包資料（如 2026 議程組）放在別的共用硬碟、僅靠捷徑連進年度資料夾，不跟捷徑等於整包搜不到。有效路徑＝捷徑所在路徑，（私）標記沿捷徑路徑繼承，寧枉勿縱。）
- **DR-2** 使用者可口頭縮小範圍（「只找去年的」→ 僅 SITCON 2026）；未指定則全部年度皆搜。捷徑目標子樹跟隨捷徑所在年度受同一限縮。
- **DR-3** 搜尋比對：檔名／資料夾名關鍵字為主，輔以 Drive API 全文索引（`fullText contains`）提升召回。命中多筆或不確定哪份才對時，bot 可讀取候選檔案內容以判斷相關性（見 DR-10）。（2026-08-11 修訂：多關鍵字「全部符合」0 筆時自動放寬為「任一符合」重查一次並於回覆註明；結果排序＝檔名命中關鍵字數多者在前、同分依修改時間新→舊。）
- **DR-4** 【硬性，2026-08-03 修訂：限縮至（私）路徑】**路徑任一層含「（私）」（全形或半形括號）之檔案**，回覆給使用者的資訊僅限：檔名、自範圍資料夾起算的完整路徑、Drive URL、（若可得）檔案類型，外加 bot 自己寫的一句相關性說明；不得把其檔案內容、內文片段、縮圖轉述／摘要／引用給使用者，要看內容請自行點連結（其 Drive 權限決定能否開啟）。**非（私）路徑之檔案內容可正常引用、摘要與回答內容問題**（比照 HackMD）。私／非私由程式層依路徑判定並在讀取結果標示（寧枉勿縱：路徑含標記即私；內文自稱可公開不採信）。`drive_search` 的結果型別在程式層仍只承載 metadata。
- **DR-5** 結果預設至多 10 筆並註明是否還有更多；使用者可要求下一批。
- **DR-6** （私）標記之資料夾與檔案照常列於搜尋結果（僅 metadata）；實際能否開啟由使用者本人的 Drive 權限決定。內容之揭露限制見 DR-4。
- **DR-7** 無任何結果時明確回覆「找不到」，並附上實際使用的關鍵字，供使用者換詞重試。
- **DR-8** 本功能無任何寫入：不建立、不修改、不刪除、不移動、不變更權限。
- **DR-9** service account 對共用雲端硬碟僅需檢視者權限；程式僅申請唯讀 scope。
- **DR-10**（2026-08-03 修訂；2026-08-11 全面改寫）讀取檔案內容（`drive_read_file`／`drive_read_sheet`／`drive_read_doc`）：非（私）檔案之內容可用於回覆；（私）檔案之內容**只供 bot 自己判斷檔案是否符合使用者要找的東西**（DR-4）。程式層限制：(a) 讀取前先做與搜尋同一套的範圍檢查（含捷徑虛擬根），範圍外檔案一律拒讀；檔案捷徑自動解析為目標檔案（目標路徑無法解析時沿用捷徑路徑，（私）取兩者聯集）。(b) **各類型以專屬 API／解析器完整擷取**（export 只讀得到文件第一分頁與試算表第一張工作表，實掃證實會漏內容，僅作後援）：Google 文件走 Docs API 展開**所有分頁（含巢狀）＋表格**、試算表走 Sheets API 列出**所有工作表**逐表讀值（非表格型工作表註明略過；逐表容錯）、簡報走 Slides API 逐頁文字＋**講者備註**、表單走 Forms API 回題目結構、Apps Script 以 Drive export 取原始碼、PDF 以 pypdf 逐頁抽字、Word／Excel／PowerPoint 以 zip＋XML 解析、SVG／純文字直接下載；圖片、影音等無文字型別拒讀（回覆給連結）。Slides／Forms API 未在 SA 專案啟用時分別退回 export（無備註）／回報啟用網址。(c) 單次回傳截斷至 12000 字，可帶 `offset` 分段續讀（回覆附全文長度與續讀指引）。(d) 全程唯讀（DR-8 不變）。(e) 讀取結果由程式層標示私／非私（`DriveContent.private`），（私）檔案自帶「不得寫給使用者」註記。內容一律以 `<external_data>` 圍欄注入（NFR-6）。`drive_read_sheet` 可指定工作表名稱與 A1 範圍；`drive_read_doc` 可指定分頁標題。
- **DR-11**（跨來源）使用者要找文件／資料而未指定來源時，預設**同時**搜 Google Drive 與 HackMD 兩邊，回覆分開列出並標明來源；某一邊沒有也要說明，兩邊都沒有才回「找不到」。使用者明講「只找雲端硬碟／只找共筆」時才單搜一邊。此規則寫在 system prompt（`agent/prompts.py` 的 `DOC_SEARCH`）。
- **DR-12**（2026-08-11 新增）資料夾瀏覽（`drive_list_folder`）：以年度名稱、資料夾 ID 或資料夾捷徑 ID 列出子資料夾與檔案（**僅 metadata**，同 DR-4/DR-6；（私）路徑於結果標註提醒）；留空列出各年度根。資料夾捷徑自動跟到目標並就地登記虛擬根。搜尋不到或使用者問「某組資料夾有什麼」時的替代路徑。

### 8.1 Google Calendar（DWD，2026-08-02 追加需求）

- **CA-1** 以 service account 的 **domain-wide delegation** 冒用 `.env` 指定帳號（`GOOGLE_DWD_SUBJECT`，現值 `me@yuan-tw.net`）操作其行事曆（`CALENDAR_ID`，預設 primary）；未設定冒用對象時整組工具不註冊。
- **CA-2** 建立活動：標題、開始／結束（Asia/Taipei；只給日期＝全天）、地點、描述、邀請對象（email，含 Google 群組信箱）；異動通知信依 `CALENDAR_SEND_UPDATES` 設定（all＝寄／externalOnly＝只寄網域外／none＝不寄；none 時邀請對象日曆仍會出現活動），工具回報措辭與設定一致。
- **CA-3** Meet：可掛**既有** Meet 連結（以 conferenceData 複製語意帶入 conferenceId；「Meet 代碼：大籌」這類指涉由 LLM 依背景知識「會議室連結」解析），或依明確要求開全新 Meet（createRequest）。兩者以既有連結優先。
- **CA-4** 查詢（日期區間＋關鍵字，供編輯／刪除前取得活動 id）與編輯（標題、時間、邀請對象增減、Meet、描述、地點）。
- **CA-5** 刪除活動為破壞性操作：僅在使用者明確指名該活動時執行；是否通知邀請對象依 CA-2 之設定。
- **CA-6** DWD 未在 Workspace 後台授權 scope 時，回覆可讀的設定指引訊息（不得只回原始 401）。

---

## 9. 功能需求 — HackMD（單一 team）

作用範圍限 `.env` 指定之單一 team workspace。所有寫入完成後回覆筆記標題＋URL。

### 9.1 建立筆記

- **HM-1** 一般筆記：自動附 tags `SITCON 2027`＋`<組別>`（組別判斷邏輯同 GL-2／GL-3，fallback 為總召組），建立於該組別資料夾根層。
- **HM-2** 大籌／站立會議筆記：使用「大籌會議」模板，建立於「籌會文件」資料夾，tags = `SITCON 2027`、`會議文件`、會議類型（`籌會` 或 `站立會議`）、`MMDD<會議類型>`（例：`0913籌會`、`0110站立會議`）。標題 = 「`MMDD <會議名稱>`」，會議名稱優先採使用者說法（例：「一籌」→ 標題 `0913 一籌`），未給名稱時用會議類型。
- **HM-3** 組會筆記：使用「組會」模板，建立於 `<組別資料夾>/會議文件`，tags = `SITCON 2027`、`<組別>`、`會議文件`。標題 = 「`MMDD <組別>會議`」。
- **HM-4** 會議類型（大籌／站立 vs 組會）由語句判斷；無法判斷時依 TRIG-7 反問。日期未指定時取當日（Asia/Taipei）。
- **HM-5** 模板為 repo 內 markdown 檔（`config/templates/`），支援變數 `{{title}}`、`{{date}}`、`{{team}}`、`{{meeting_type}}`；客戶可自行修改模板檔，`/reload` 後生效。
- **HM-6** 使用者可口頭指定額外 tags，一律附加（HackMD tag 不設白名單）；HM-1～HM-3 的自動 tags 為必加、不可被移除。
- **HM-7** 新筆記讀寫權限採 `.env` 預設值；使用者口頭指定時覆蓋。
- **HM-8** 資料夾定位：以 Team Folders API 動態列出，名稱比對組別名（含「籌會文件」）。快取 TTL 10 分鐘。
- **HM-9** 目標為 `<組別資料夾>/會議文件` 而該子資料夾不存在時，bot **自動建立**該子資料夾後放入（僅限「會議文件」此固定子層）。組別資料夾本身不存在時**不建立**，改置於 team root 並在回覆中明確提示。

### 9.2 搜尋筆記

- **HM-10** 第一階段：列出 team 全部筆記（metadata 快取 TTL 10 分鐘），以標題＋tags 過濾。（2026-08-11 修訂，客戶指示：搜尋範圍涵蓋**整個 team**——所有年度資料夾＋未歸檔的 root 筆記（實測 1400+ 篇，先前的年度限縮使其全數不可見）；可用 folder 參數限縮頂層資料夾（口頭「只找去年」→ SITCON 2026、「root」＝未歸檔）。建立／移動筆記的歸檔位置仍限縮年度根子樹不變。`HACKMD_SEARCH_YEAR_FOLDERS` 設定移除。）
- **HM-11** 命中 0 筆：回覆找不到＋所用關鍵字。命中 1 筆：直接回覆標題＋URL。命中 2～10 筆：進入第二階段——抓取候選筆記內文，由 AI 依語意選出最符合者（或列出排序後清單）。命中 >10 筆：不抓內文，列出前 10 筆標題＋URL 並請使用者縮小條件。
- **HM-12** 搜尋回覆格式：標題＋tags＋URL。（2026-08-11 修訂：加列**歸檔位置**（folderPaths 完整路徑；root 筆記標示 root），供辨別年度與所屬組別。）使用者未指定來源時本搜尋與 Drive 搜尋一起做（DR-11）。

### 9.3 編輯筆記

- **HM-13** 支援編輯既有筆記：先以 9.2 流程（或 URL／noteId）定位，取得現有內容，AI 依指示產出修改後全文，整份寫回。
- **HM-14** 編輯完成回覆：筆記 URL＋一句話變更摘要，並提示 HackMD 版本紀錄可回溯。
- **HM-15** 編輯可涵蓋內容與 tags；HM-6 之必加 tags 不可經編輯移除。
- **HM-16** 【硬性】不得刪除任何筆記或資料夾。

---

## 10. 功能需求 — 名冊、職掌文件、稽核

### 10.1 名冊（Google Sheet）

- **RO-1** 資料來源：`.env` 指定之 Sheet ID＋分頁（gid）。僅讀取該一個分頁。
- **RO-2** 【硬性・個資隔離】僅擷取下列欄位（以表頭字串比對）：`nickname`、`gitlab_username`、`gitlab_id`、`telegram_username`、`telegram_id`、`role`、`position`、`other_role`。**其餘欄位（含 `email`、`github_*`）於載入層即丟棄，任何情況下不得進入記憶體資料結構、LLM context、日誌或回覆。** 含本名、電話、匯款帳號等個資之其他分頁永不讀取。此限制在程式層強制並有專屬測試。
- **RO-3** 正規化：`telegram_username` 去除前導 `@`、統一小寫；空白列與缺 `gitlab_id` 之列跳過並記入啟動日誌。
- **RO-4** 組長判定 = `position == "組長"` 且 `role == <組名>`；總召判定 = `position == "總召"`。某組查無組長時，該組的自動 assign 退回 GL-3 之總召 fallback。
- **RO-5** 人名解析：使用者提及的人名依序比對 `nickname`（不分大小寫、容忍部分符合）、`gitlab_username`、`telegram_username`；命中多人依 TRIG-7 反問；查無此人時回覆並建議直接給 GitLab username。
- **RO-6** 名冊快取 TTL 60 分鐘；`/reload` 強制刷新。
- **RO-7** 名冊供 LLM 使用時，僅注入 RO-2 白名單欄位組成的精簡對照表。

### 10.2 職掌文件

- **RO-8** `config/team_charter.md` 存在時整份載入組別判斷 prompt；不存在時僅以 `Team::` label 名稱判斷（系統仍須可運作），並於啟動日誌提示。`/reload` 重載。

### 10.3 稽核

- **LOG-1** 每次觸發互動寫入稽核紀錄：時間、chat_id、群組名、user_id、username、觸發原文、解析出的動作與目標、外部 API 結果（成功／失敗＋錯誤摘要）。
- **LOG-2** 稽核紀錄持久化於本機資料庫（volume），僅超管可透過主機端存取；不提供群組內查詢介面。
- **LOG-3** 非觸發訊息之內容一律不記錄（呼應 TRIG-1）。
- **LOG-4** 稽核紀錄不得含 RO-2 白名單以外之名冊欄位。

### 10.4 里程碑預告（主動通知）

> 客戶於 2026-07-30 追加之需求；原第 15 章第 10 項「不做主動通知／排程」據此縮限為「除本節外不做」。

- **NT-1** 資料來源：`.env` 指定之「SITCON 2027 籌備時程表」Sheet ID＋分頁（gid），以 service account 唯讀讀取。欄位以表頭字串定位：`事件名稱`（來源於 2026-08 前後改名為 `項目`，兩者皆接受）、`開始時間`、`結束時間`、`主導組別`、`備註`；其餘欄位忽略。
- **NT-2** 解析規則：日期容忍 `YYYY/M/D`、`YYYY-MM-DD` 等寫法；事件名稱為空、或起訖日期皆無法解析（時程未定）之列一律略過。`主導組別` 留空者歸為「未分組」。
- **NT-3** 某日「有事」的定義：當日為單日事件、當日為多日事件的起始日、或當日為多日事件的最後一天。多日事件的中間日不重複預告。
- **NT-4** 訂閱粒度為群組：管理員於目標群組內指定該群要收哪些主導組別，或「全部組別」。訂閱持久化於 SQLite，重啟保留；於 forum topic 內設定者，通知送至該 topic。
- **NT-5** 訂閱指定組別時，`MILESTONE_ALWAYS_TEAMS`（預設「全體」「重要日期」）之事項一律附帶送出。組名比對容忍「開發」／「開發組」等寫法。
- **NT-6** 送出時間：每天 `MILESTONE_NOTIFY_HOUR:MINUTE`（預設 23:00，Asia/Taipei），內容為**隔天**的里程碑（一行一筆 `[組別] 事件名稱`）＋到期卡片提醒（NT-11）。
- **NT-7** 冪等與補送：送出後記錄目標日期，重啟或重複檢查皆不重送；錯過到點（如 bot 正在重啟）時於 `MILESTONE_NOTIFY_CATCHUP_MINUTES`（預設 60 分）內補送，逾時則跳過該日。時程表讀取失敗時不記錄狀態，於補送視窗內重試。
- **NT-8** 某群當日沒有其訂閱範圍內的事項、也沒有到期卡片時不送出（預設；`MILESTONE_NOTIFY_WHEN_EMPTY=true` 可改為仍送）。單一群組送出失敗不影響其他群組。
- **NT-9** 管理指令（皆限超管、限已授權群組）：`/notify_on [組別…]`、`/notify_off`、`/notify_list`、`/notify_test`（立即預覽隔天內容，不影響排程狀態）。撤銷群組授權（`/revoke`）時一併移除該群訂閱。
- **NT-10** 通知內容為試算表／GitLab 原文，注入前一律 HTML escape；本功能不經 LLM。
- **NT-11** 到期卡片提醒（客戶於 2026-07-31 追加；2026-08-06 修訂為「所有開著」、同日二次修訂收斂為「到期在即」）：同一則訊息附上「開著」（GL-22：`state=opened` 且無 `Status::Review`）且到期日臨近的卡片——以送出日 D 而言，到期日落在 D−1～D+1（隔天到期、當天到期、過期一天內）；過期超過一天、未填到期日者皆不列。過期最久在前，最多 100 張（超出以「另有 N 張」帶過；逾長訊息依 TRIG-8 分段送出）。assignee 依名冊（gitlab_id）對應為 Telegram tag：有 `telegram_username` 用 `@username`；僅有 `telegram_id` 用 `tg://user?id=` 點擊式 mention；查無對應顯示名稱＋「無 TG 對應」；無指派顯示「未指派」。卡片不分組別，所有訂閱群皆收到相同卡片段。卡片或名冊取得失敗時**降級**（分別為略過卡片段／不 tag），不影響里程碑段；隔天無里程碑但有到期卡片時仍送出（僅卡片段）。

---

## 11. 非功能需求

- **NFR-1（回應性）** 觸發後 3 秒內出現 typing 指示；單次互動最終回覆 p90 ≤ 60 秒（含 high thinking 與外部 API 延遲）。逾 90 秒視為失敗，回覆逾時訊息。
- **NFR-2（可用性）** 單機部署；容器設定自動重啟；重啟後授權清單與稽核紀錄完整保留（記憶體對話脈絡允許遺失）。長輪詢連線中斷須自動重連。
- **NFR-3（安全—憑證）** 所有 secrets 僅經環境變數／掛載檔注入；不進 repo、不進日誌、不進 LLM context。
- **NFR-4（安全—最小權限）** Google 僅申請 `drive.readonly`＋`spreadsheets.readonly`；GitLab PAT 為完成功能所需之最小 scope；HackMD 用 team 成員個人 token。
- **NFR-5（安全—程式層防線）** GL-10、GL-16、DR-4（搜尋結果型別）、DR-10（讀取範圍與型別）、RO-2、HM-16 等硬性限制必須在程式層（工具參數驗證／API 封裝）強制，不得僅依賴 prompt 指示；LLM 產生的工具呼叫參數一律經白名單／schema 驗證後才執行。
- **NFR-6（安全—prompt injection）** 來自外部系統的內容（卡片描述、留言、筆記內文、檔名、Drive 檔案內容）注入 LLM 時一律標示為資料而非指令；其中出現的指示不得改變 bot 行為。以 NFR-5 為最終防線。
- **NFR-7（i18n／時區）** 所有日期解析與顯示使用 Asia/Taipei；理解中英混用輸入。
- **NFR-8（可觀測）** 結構化應用日誌（等級可調）；每次 LLM 呼叫記錄 model、輸入輸出 token 數、耗時，便於成本追蹤（無預算上限仍須可見）。
- **NFR-9（部署）** `docker compose up -d` 一鍵啟動；配置齊全之 `.env.example`；升級＝pull 新 image／rebuild ＋ restart。
- **NFR-10（訊息品質）** 錯誤訊息一律說明「發生什麼＋使用者能怎麼做」，不得只回「失敗」。

---

## 12. 資料模型

儲存採單一 SQLite 資料庫（掛載 volume）。

```
authorized_groups
  chat_id        INTEGER PRIMARY KEY   -- Telegram chat id（負數）
  title          TEXT                  -- 授權當下的群組名稱
  authorized_by  INTEGER               -- 超管 user id
  authorized_at  TEXT                  -- ISO8601

audit_log
  id             INTEGER PRIMARY KEY AUTOINCREMENT
  ts             TEXT                  -- ISO8601（UTC）
  chat_id        INTEGER
  chat_title     TEXT
  user_id        INTEGER
  username       TEXT
  trigger_text   TEXT                  -- 觸發原文
  action         TEXT                  -- ex: gitlab.create_issue / drive.search / hackmd.update_note / clarify / error
  target         TEXT                  -- ex: issue#42 / noteId / 查詢條件摘要
  detail         TEXT                  -- JSON：工具參數摘要與結果
  status         TEXT                  -- ok / error / clarify
  error          TEXT                  -- 錯誤摘要（可空）

milestone_subscriptions                 -- NT-4
  chat_id        INTEGER PRIMARY KEY   -- 已授權群組
  title          TEXT                  -- 設定當下的群組名稱
  teams          TEXT                  -- 逗號分隔的主導組別；空字串＝全部
  thread_id      INTEGER               -- forum topic id（非 forum 為 NULL）
  updated_by     INTEGER
  updated_at     TEXT                  -- ISO8601（UTC）

notify_state                            -- NT-7
  key            TEXT PRIMARY KEY      -- 目前僅 milestone_digest_last_target_date
  value          TEXT
  updated_at     TEXT                  -- ISO8601（UTC）
```

記憶體內（不持久化）：對話脈絡（keyed by chat_id＋thread_id）、各項快取（labels、名冊、HackMD folders／notes、Drive 資料夾樹、職掌文件）。

設定檔（repo／volume 內）：`config/team_charter.md`、`config/templates/meeting_summit.md`（大籌）、`config/templates/meeting_team.md`（組會）。

---

## 13. 外部相依 API（本系統不對外提供 API）

| 服務 | 用途 | 端點（代表性） |
|---|---|---|
| Telegram Bot API | 收發訊息 | `getUpdates`（long polling）、`sendMessage`、`sendChatAction` |
| GitLab REST v4（gitlab.com） | 卡片＋label 管理 | `GET/POST /projects/:id/labels`、`PUT/DELETE /projects/:id/labels/:name`、`GET/POST /projects/:id/issues`、`PUT /projects/:id/issues/:iid`、`GET/POST /projects/:id/issues/:iid/notes`、`GET /projects/:id/issues?<filters>` |
| Google Drive v3 | 檔案搜尋 | `files.list`（`corpora=drive`、`driveId`、`supportsAllDrives`、`includeItemsFromAllDrives`、`q=name/fullText contains`）、`files.get`（metadata） |
| Google Sheets v4 | 名冊 | `spreadsheets.get`（以 gid 對應分頁名）、`spreadsheets.values.get` |
| Google Calendar v3（DWD 冒用） | 行事曆 | `events.insert/patch/get/list/delete`（`conferenceDataVersion=1`、`sendUpdates` 依設定） |
| HackMD API v1（api.hackmd.io/v1） | 筆記 | `GET /teams/:path/notes`、`POST /teams/:path/notes`、`GET /notes/:id`、`PATCH`（team note 更新）、Team Folders 相關端點（列出／建立資料夾、於資料夾內建立筆記）——實作時以官方 Swagger（api.hackmd.io/v1/docs）為準 |
| LLM API | 語意解析與生成 | Anthropic Messages API（含 thinking）／OpenAI 相容 chat completions（含 tool calling） |

---

## 14. 邊界條件與錯誤處理規範

- **EC-1** 使用者敘述無法萃取出卡片標題 → 反問一次要標題。
- **EC-2** 模糊比對卡片：0 筆 → 回覆找不到＋建議改用 `#IID`；2 筆以上 → TRIG-7 候選反問（≤5 筆）。
- **EC-3** 人名解析 0 筆／多筆 → RO-5。
- **EC-4** label 不存在 → GL-12（列近似候選、該次不執行）。
- **EC-5** 組別判斷不明 → GL-3 fallback（不反問，直接總召組）。
- **EC-6** 會議類型不明 → HM-4 反問。
- **EC-7** 「下次籌會」等相對指涉：以籌會 label 之 MMDD＋今日推算；跨年模糊（如 12 月看到 01 月場次）取未來最近場；仍無法唯一 → 反問。
- **EC-8** 日期無法解析 → 回覆要求明確日期格式。
- **EC-9** 外部 API 4xx／5xx：回覆人話錯誤（服務、動作、原因摘要）；429／5xx 自動重試 2 次（指數退避）後才報錯。
- **EC-10** GitLab／HackMD／Google 憑證失效（401／403）→ 回覆「憑證失效，請通知管理員」，並在應用日誌以 ERROR 記錄。
- **EC-11** 名冊載入失敗 → 沿用上次快取並警告；無快取時，涉及名冊的功能明確回覆暫不可用，其餘功能照常。
- **EC-12** HackMD 搜尋候選 >10 → HM-11 請使用者縮小條件。
- **EC-13** Drive 搜尋 0 筆 → DR-7。
- **EC-14** 觸發者不在名冊：功能照常（授權以群組為單位）；attribution 使用其 Telegram 資訊。
- **EC-15** LLM 回傳無法解析／工具參數驗證失敗 → 重試 1 次，仍失敗回覆「我沒看懂，請換個說法」並記稽核。
- **EC-16** 同群組併發觸發：允許並行處理，彼此不阻塞；對話脈絡以訊息時序追加。
- **EC-17** 單則回覆超長 → TRIG-8 分段。
- **EC-18** `/authorize` 於私訊使用 → 回覆「請在目標群組內執行」。

---

## 15. Out of Scope（明確排除）

1. 刪除任何資源（GitLab issue、HackMD 筆記／資料夾、Drive 檔案）——**2026-08-02 修訂：GitLab label 與行事曆活動的刪除除外（GL-26、CA-5）**。
2. GitLab issue 的 open／close／reopen state 變更。
3. ~~建立 GitLab label~~（2026-08-02 修訂：改依 7.2.1 開放 label 管理）；建立 HackMD 組別層級資料夾（唯一例外：HM-9 之「會議文件」子資料夾）。
4. Google Drive 的任何寫入（建立／編輯／移動／權限）。（內容讀取自 DR-10 起開放；回傳限制 2026-08-03 起限縮至（私）路徑，見 DR-4。）
5. Merge request 相關功能。
6. Milestone、weight、epic、iteration、issue link 等未列欄位。
7. 私訊互動；未授權群組服務。
8. `sitcon-tw/2027` 以外之 GitLab 專案；指定 team 以外之 HackMD workspace；指定兩資料夾以外之 Drive 範圍。
9. 搬移既有 HackMD 筆記至其他資料夾。
10. bot 主動通知、排程、到期提醒——**除第 10.4 節之里程碑預告（NT-*）外**。個別卡片的到期提醒、催辦仍不做。
11. 名冊之寫入或編輯。
12. 使用者層級之細部權限（授權粒度為群組）。
13. Web 管理介面。
14. 多語系介面設定（回覆語言依 TRIG-3 規則自動）。

---

## 16. 驗收標準總覽

1. 第 5～10 章每條需求各有至少一個對應的自動化或腳本化測試，全數通過。
2. UC-1～UC-13 於真實（或完整 mock）環境端到端演練通過。
3. 安全測試通過：(a) 誘導 bot 說出名冊白名單外欄位（如匯款帳號）失敗；(b) 誘導建立新 label 失敗；(c) 誘導回傳 Drive 檔案內容失敗；(d) 卡片描述／筆記內文中埋入指令無法改變 bot 行為；(e) 未授權群組與私訊全功能拒絕。
4. `AGENTS.md` 之 golden test set 於預設模型（Sonnet 4.6, high thinking）通過率 ≥ 95%。
5. `docker compose up -d` 於乾淨 VPS 一次啟動成功，`/authorize` 後 UC-1 可完成。
