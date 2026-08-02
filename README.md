# 小石（SITCON 2027 工作人員 AI 助理 Telegram Bot）

讓 SITCON 2027 籌備團隊在 Telegram 群組內，用一句自然語言（中英混用）完成
GitLab 卡片、Google 共用雲端硬碟檔案搜尋、HackMD 筆記三大類操作。

- **需求規格**：[SPEC.md](SPEC.md)（唯一權威）
- **實作指引**：[AGENTS.md](AGENTS.md)

> 小石只在**被授權的群組**內服務，以自然語言操作三個系統；破壞性以外的操作直接執行、
> 執行後回報結果，錯誤時給明確可行動的訊息；名冊個資與 Drive 檔案內容零外洩。

---

## 目錄

- [能做什麼](#能做什麼)
- [前置作業](#前置作業必讀)
- [設定 `.env`](#設定-env)
- [部署（docker compose）](#部署docker-compose)
- [授權與使用](#授權與使用)
- [備份與升級](#備份與升級)
- [本機開發與測試](#本機開發與測試)
- [架構總覽](#架構總覽)
- [安全防線](#安全防線)
- [疑難排解](#疑難排解)

---

## 能做什麼

在授權群組裡 **@提及小石**、**reply 小石的訊息**、或用「**小石 …**」開頭：

| 類別 | 範例 |
|---|---|
| GitLab 開卡（自動判斷組別、指派組長） | 「小石 幫我開一張卡：官網倒數計時器壞了」 |
| GitLab 指定細節開卡 | 「小石 開卡給行政組，標題場地保證金匯款，due 8/15」 |
| GitLab 編輯/留言/查詢 | 「小石 把 #42 改成 Doing，加 0913 一籌」「小石 列出行政組還開著的卡」 |
| 找文件（Drive＋HackMD 一起找，回覆只給檔名與連結） | 「小石 幫我找去年的場地租借合約」 |
| HackMD 開/找/改筆記 | 「小石 開一份 0913 一籌的會議記錄」「小石 找上次討論贊助方案的文件」 |
| 里程碑預告（主動通知） | 每天 23:00 自動預告隔天的籌備時程事項＋過期 GitLab 卡片提醒（tag assignee），可依群組設定要收哪些組別 |

管理指令（管理員）：`/authorize`、`/revoke`、`/list_groups`、`/reload`、
`/notify_on`、`/notify_off`、`/notify_list`、`/notify_test`；任何人：`/help`。

---

## 前置作業（必讀）

部署前需準備四組憑證，並完成對應的權限授予。

### 1. Telegram

1. 對 [@BotFather](https://t.me/BotFather) 執行 `/newbot` 建立 bot，取得 **bot token**。
2. **關閉 group privacy**（否則收不到「小石」前綴的一般訊息 — TRIG-2）：
   BotFather → `/mybots` → 選 bot → *Bot Settings* → *Group Privacy* → **Turn off**。
3. 把 bot 加入目標群組。
4. 取得**管理員的數字 user ID**（非 username）：對 [@userinfobot](https://t.me/userinfobot) 傳訊即可看到。

### 2. GitLab（service account）

1. 以將作為 bot 身分的 GitLab 帳號建立 **Personal Access Token**，scope 勾 `api`。
2. 把該帳號加入專案 `sitcon-tw/2027`，角色至少 **Reporter**（建卡/留言需 Reporter 以上；本系統不改 issue 開關狀態）。

### 3. Google（service account，唯讀）

1. 在 Google Cloud Console 建立 **service account**，下載 JSON 金鑰。
2. 啟用 **Google Drive API** 與 **Google Sheets API**。
3. 把 service account 的 email **以檢視者（Viewer）共用**給：
   - 共用雲端硬碟（含「SITCON 2027」「SITCON 2026」兩資料夾）；
   - 名冊 Google Sheet；
   - 籌備時程表 Google Sheet（里程碑預告來源，`MILESTONE_SHEET_ID`）。
4. 本系統只申請 `drive.readonly` + `spreadsheets.readonly`（最小權限，NFR-4）。

> 名冊只讀取 `.env` 指定的**單一分頁**，且只擷取白名單欄位（`nickname`、`gitlab_username`、
> `gitlab_id`、`telegram_username`、`telegram_id`、`role`、`position`、`other_role`）。
> 含本名、電話、匯款帳號等個資的其他分頁**永不讀取**（RO-2）。

### 4. HackMD

1. 以 team 成員身分建立 **個人 API token**（HackMD → 設定 → API）。
2. 記下 team 的 **path**（`hackmd.io/@team/...` 的 team path）。

---

## 設定 `.env`

複製範本並填入：

```bash
cp .env.example .env
```

必填變數（缺任一啟動即失敗並指出變數名）：

| 變數 | 說明 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather 的 bot token |
| `TELEGRAM_ADMIN_ID` | 管理員數字 user ID |
| `LLM_API_KEY` | LLM API 金鑰 |
| `GITLAB_TOKEN` | GitLab PAT（scope=api） |
| `HACKMD_TOKEN` | HackMD 個人 token |
| `HACKMD_TEAM_PATH` | HackMD team path |

其餘變數（LLM provider/model、GitLab/Google/HackMD 端點、快取 TTL 等）皆有預設值，
完整清單與註解見 [.env.example](.env.example)。

- **LLM**：`LLM_PROVIDER=anthropic`（原生格式）或 `openai_compat`（OpenAI／OpenRouter，設 `LLM_BASE_URL`）。
  `LLM_MODEL` 依 provider 的模型字串（OpenRouter 例：`anthropic/claude-sonnet-4.6`）。
  `LLM_THINKING` = `off|low|medium|high`，統一對映到 adaptive thinking 的 effort（見下方[備註](#關於-thinking-設定)）。
  `LLM_SERVICE_TIER`（僅 openai_compat，留空＝不帶）：Codex 的 fast mode 即 `fast`，官方 API 另有 `priority`／`flex`；
  帳號或 gateway 不支援時 provider 會回 400，改回留空即可。
- **併發**：訊息之間並行處理，`MAX_CONCURRENT_UPDATES`（預設 32）為同時處理的 update 上限、
  `MAX_CONCURRENT_AGENT_TURNS`（預設 8）為同時進行的 agent 回合上限。
  `SERIALIZE_PER_CHAT`（預設 `true`）讓同一對話（群＋forum topic）的 agent 回合依序跑，回覆順序不亂；
  設 `false` 則同群也完全並行（SPEC EC-16）。不同群／不同 topic 一律並行。
- Google service account 金鑰以檔案掛載（`GOOGLE_SA_JSON_PATH`，compose 預設 `/run/secrets/google-sa.json`）。

---

## 部署（docker compose）

```bash
# 1) 準備 .env（見上）
cp .env.example .env && $EDITOR .env

# 2) 放入 Google service account 金鑰（容器以非 root 讀取，需可讀）
mkdir -p secrets
cp <你的金鑰>.json secrets/google-sa.json
chmod o+r secrets/google-sa.json

# 3) 讓資料目錄可被容器使用者（uid 10001）寫入（SQLite 存這裡）
mkdir -p data && sudo chown -R 10001:10001 data

# 4) 啟動（長輪詢，不需對外開 port／網域／TLS）
docker compose up -d --build     # 首次或程式更新後都加 --build
docker compose logs -f           # 觀察啟動摘要（secret 已遮蔽）

# 停止 / 重啟
docker compose down
docker compose restart
```

> 容器以非 root（uid 10001）執行以符合最小權限；因此 bind 掛載的 `./data` 需屬 uid 10001（上面步驟 3），
> `secrets/google-sa.json` 需 others 可讀。程式更新後務必 `docker compose up -d --build` 重新 build，否則跑到舊 image。

容器設定 `restart: unless-stopped`，斷線自動重連；授權清單與稽核紀錄持久化於 `./data`（volume），
重啟不失效（NFR-2）。

---

## 授權與使用

1. 管理員在**目標群組**內輸入 `/authorize` → 該群組進入授權清單。
2. 群組成員即可用 @提及／reply／「小石 …」開頭觸發；輸入 `/help` 看範例。
3. 管理：`/list_groups`（列出授權群組）、`/revoke`（撤銷本群組）、`/reload`（重載名冊/label/HackMD/Drive/時程表/職掌文件快取）。

未授權群組與私訊一律不提供功能（私訊回固定說明；未授權群組沉默，唯一例外是管理員的 `/authorize`）。

### 里程碑預告（NT-*）

每天 **23:00（Asia/Taipei）** 自動把**隔天**的籌備時程里程碑（一行一筆 `[組別] 事件名稱`）推到有訂閱的群組，
並附上**仍未關閉且已到期**（含當天到期）的 GitLab 卡片提醒，tag 到 assignee。
里程碑資料來源是 [SITCON 2027 籌備時程表](https://docs.google.com/spreadsheets/d/1esryzLBpnE51NIUU4-prwG0RrnG3W4eYQJ6FZSigFPQ/edit)
的「工作表1」（`MILESTONE_SHEET_ID` / `MILESTONE_SHEET_GID`），**須把該試算表分享給 service account（檢視者即可）**。

管理員在**目標群組**（forum 群組請在要收通知的 topic 裡）執行：

| 指令 | 說明 |
|---|---|
| `/notify_on` | 本群收**全部組別**的里程碑 |
| `/notify_on 開發組, 行政組` | 只收這幾組（分隔用逗號／頓號／空白皆可，「開發」＝「開發組」） |
| `/notify_off` | 本群取消訂閱 |
| `/notify_list` | 列出所有訂閱群組與其組別 |
| `/notify_test` | 立刻預覽本群明天會收到的內容（不影響排程） |

行為細節：

- 指定組別時，**全體**與**重要日期**的事項一律附帶送出（`MILESTONE_ALWAYS_TEAMS` 可調）。
- 「隔天有事」＝當天是單日事件、多日事件的**起始日**或**最後一天**；中間日不重複打擾。
- 卡片提醒收錄 `state=opened` 且 `due date ≤ 當天` 的卡，過期最久在前、最多 20 張；assignee 依名冊對應
  Telegram 帳號來 tag（查無對應則顯示名稱＋「無 TG 對應」）。卡片**不分組別**，所有訂閱群都收到。
- 該群隔天沒有相關事項、也沒有過期卡片就不送（`MILESTONE_NOTIFY_WHEN_EMPTY=true` 可改）。
- 已送出的日期記在 DB，重啟不會重送；若 23:00 剛好在重啟中，1 小時內啟動會補送（`MILESTONE_NOTIFY_CATCHUP_MINUTES`）。
- GitLab 暫時取不到卡片時該日只送里程碑段；名冊取不到時卡片照送、只是 tag 退化為顯示名稱。
- 時程表改了不必重啟，快取 1 小時（`CACHE_TTL_MILESTONES`），要立即生效就 `/reload`。
- 整條路徑不經 LLM，內容照表原文送出（HTML escape）。
- `/revoke` 撤銷授權時會一併移除該群訂閱。

---

## 備份與升級

- **備份**：定期備份 `./data`（含 `sitcon_bot.sqlite3`：授權清單與稽核紀錄）。
- **升級**：

  ```bash
  git pull
  docker compose build
  docker compose up -d
  ```

- **設定檔熱更新**：`role.md`（職掌文件，路徑見 `TEAM_CHARTER_PATH`）、`config/knowledge.md`
  （背景知識：會議室代碼等內部常識，路徑見 `KNOWLEDGE_PATH`）與 `config/templates/*.md`
  （會議模板）改完後，在授權群組執行 `/reload` 即生效，不需重啟。

---

## 本機開發與測試

需求：Python 3.12+、[uv](https://docs.astral.sh/uv/)。

```bash
uv sync                         # 安裝依賴（含 dev）
uv run python -m sitcon_bot --check   # 驗證設定並印摘要（不連線）
uv run ruff check .             # lint
uv run pytest                   # 單元＋整合＋安全測試（golden 需憑證，預設略過）
```

### Golden test set（實打 LLM）

```bash
# 需在環境變數提供 LLM_API_KEY（與 LLM_PROVIDER/LLM_MODEL）
uv run pytest tests/golden -m golden --run-golden

# Sonnet vs Haiku 對照
uv run pytest tests/golden --run-golden --golden-model claude-haiku-4-5
```

逐條比對 LLM 的「第一個工具呼叫」名稱與關鍵參數；通過率門檻預設 95%（`GOLDEN_THRESHOLD` 可調）。

### 安全測試（SPEC 16.3）

```bash
uv run pytest -m security       # 五項硬性防線（程式層，不需 LLM）
```

---

## 架構總覽

```
Telegram ──長輪詢──▶ gateway（TRIG-1 觸發過濾／授權路由）
                        │  觸發訊息
                        ▼
                     Agent loop（tool-calling）
                        │  system prompt = 人設＋規則＋文件搜尋規則＋今日＋label 白名單＋名冊(白名單欄位)＋職掌
                        ├─▶ LLM adapter（Anthropic / OpenAI 相容；adaptive thinking）
                        └─▶ tools（pydantic 驗證 → 執行）
                              ├ gitlab_*  → GitLabClient（label 白名單／scoped 互斥／無 state 變更；
                              │              含 label 管理 create/update/delete，異動即刷新白名單）
                              ├ resolve_person → 名冊（RO-2 白名單）
                              ├ drive_search/_read_file → DriveClient（搜尋只回 metadata；
                              │                            內容僅供判斷相關性，不外流）
                              ├ hackmd_*  → HackMDClient（notes／team folders／模板）
                              └ calendar_* → CalendarService（DWD 冒用 GOOGLE_DWD_SUBJECT；
                                             邀請對象／既有 Meet 連結／新 Meet）
   SQLite（授權清單、稽核）持久化於 volume
```

- 觸發過濾、授權判定、label 白名單、scoped 互斥、名冊欄位白名單、Drive 內容不外流、
  HackMD 無刪除等**硬性限制皆在程式層強制**，不依賴 prompt（NFR-5）。
- 對話脈絡以（群組, topic）保留最近互動供指代，30 分鐘或 10 輪失效（TRIG-4）。

模組對照見 [AGENTS.md](AGENTS.md) 第 2 章。

## 安全防線

| 硬性條款 | 強制位置 |
|---|---|
| GL-10 卡片操作只用既有 label | `services/gitlab_client.py`：寫入前逐一比對白名單，未知 label 拒絕、不隱式補建（label 管理為 2026-08-02 起的獨立明確操作，異動後強制刷新白名單） |
| GL-16 不變更 issue 開關/刪除 | `gitlab_client.py`：不實作 state/delete，payload 永不含 `state_event` |
| DR-4 只回 metadata | `services/drive_client.py`：回傳型別只有 `{name,path,url,mime}` |
| RO-2 名冊欄位白名單 | `services/sheets_roster.py`：僅白名單欄位落地，`Member` 結構不承載其他欄位 |
| HM-16 不刪除筆記/資料夾 | `services/hackmd_client.py`：無 delete 方法 |
| NFR-6 外部內容為資料非指令 | 工具結果以 `<external_data>` 標記注入 |

對應的自動化測試在 `tests/security/`（SPEC 16.3 五項）。

### 關於 thinking 設定

`.env` 的 `LLM_THINKING`（off/low/medium/high）對映到 **adaptive thinking + `output_config.effort`**，
而非固定的 `budget_tokens`。原因：`budget_tokens` 已於較新的模型（Opus 4.7+/Sonnet 5 等）移除並會回 400；
adaptive + effort 對所有現行模型（含預設 `claude-sonnet-4-6`）皆可用。`off` 對映到 thinking disabled。

---

## 疑難排解

| 症狀 | 可能原因 / 解法 |
|---|---|
| 啟動即失敗、指出某變數 | `.env` 缺該必填變數；對照 `.env.example` 補齊 |
| 「小石 …」開頭沒反應 | BotFather group privacy 未關（TRIG-2）；或群組未 `/authorize` |
| 涉及人名/組長指派回「名冊暫不可用」 | service account 未共用名冊 Sheet，或 gid 錯；查啟動日誌 |
| 回「憑證失效，請通知管理員」 | 對應服務（GitLab/HackMD/Google）token 失效或權限不足 |
| label 找不到 | 只能用專案既有 label；小石會列出最接近的候選供改用 |

日誌等級以 `LOG_LEVEL` 調整；每次 LLM 呼叫會記錄 model／token／耗時，便於成本追蹤（NFR-8）。

## 授權條款

MIT
