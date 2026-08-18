<!--
  小石的背景知識文件。

  用途：這份文件會「整份」放進小石的 system prompt，讓小石回答時能直接引用
  這些籌備團隊的內部常識（會議室代碼、常用縮寫、固定連結…）。

  維護方式：
  - 格式自由的 markdown，建議一個主題一節、條列或表格，越精簡越好（內容全算 token）。
  - 改完在授權群組執行 /reload 立即生效，不用重啟。
  - 路徑可由 .env 的 KNOWLEDGE_PATH 調整（預設 config/knowledge.md）；檔案不存在時
    小石照常運作，只是沒有這些背景知識。
  - 不要放機密（token、密碼、個資）：這裡的內容可能會出現在小石的回覆裡。
  - 各組職掌不要寫在這裡，請寫在職掌文件（role.md），那份是給組別判斷用的。
-->

# 重要知識
我們現在籌備的是 SITCON 2027，所以如果要引用資料，以 Hackmd tag 有 2027 / SITCON 2027，或者 Google Drive 是在 2027 資料夾底下的內容為優先。
### 常用連結
- [籌備時程表](https://docs.google.com/spreadsheets/d/1NHav7y5ZMMrBGA0umklC8qRSFU5pHjlVKUq8zrp5Upk/edit?gid=0#gid=0)
- [預算表](https://docs.google.com/spreadsheets/d/1EXVsS0MZ5l1g7SPKZZqDZukgz7wxs754egTGw1hdQMQ/edit?usp=drive_link)
- [2027 檢討文件](https://docs.google.com/spreadsheets/d/1hMoyLOu51Ps3bPfvlP-I0Npp3oO5gWxRUO1RQocfAzk/edit?usp=drive_link)
- [編輯組 GitLab 版](https://gitlab.com/sitcon-tw/editorial/board/-/boards/)
- [招募公開資訊](https://hackmd.io/@SITCON/2027-recruit)

### 各種表單
- [SITCON 2026 檢討文件](https://docs.google.com/spreadsheets/d/1rM2ujWVS1gDlqOH2xFDHo0WTDlYQ_CjiCq8NgTLran8/edit)

### 會議室連結
- 大籌：https://meet.google.com/uee-eyar-cos
- 行政組：https://meet.google.com/hxw-vdtd-tsz
- 議程組：https://meet.google.com/rri-apof-dey
- 場務組：https://meet.google.com/khp-qgcm-ewn
- 製播組：https://meet.google.com/hyf-uuhr-wio
- 編輯組：https://meet.google.com/nnc-mpfn-vzx
- 紀錄組：https://meet.google.com/ggy-pvre-hqb
- 行銷組：https://meet.google.com/iyr-uoyn-wfs
- 活動組：https://meet.google.com/bth-kjej-wsv
- 開發組：https://meet.google.com/bng-vbjp-qjo
- 設計組：https://meet.google.com/yvv-nxof-mjb

### 會議資訊
常用開會地點：
- 摩茲工寮（預設實體地點）：
  - 摩茲工寮 / MozTW Space / Mozilla Community Space Taipei
  - 100臺北市中正區重慶南路一段99號1105室世界大樓11樓
  - https://maps.app.goo.gl/W2FAxBEEiX9mWwL96
- NPO HUB：
  - Impact Hub Taipei - Zhongzheng（中正）/ 台北NPO聚落 NPO HUB Taipei
  - 100臺北市中正區龍光里重慶南路三段2 號
  - https://maps.app.goo.gl/8MzDaRy7xmg8MWyd7

大籌有兩種，第一種是大籌，就是實體跟線上同時進行的；另外一種是站立會議，純線上的。你在創建會議 hackmd 文件時時，如果今天是大籌，地點欄位就是寫 「[實體地點]($GoogleMap) x [Google Meet](https://meet.google.com/uee-eyar-cos)」，反之站立會議通常是只有「[Google Meet](https://meet.google.com/uee-eyar-cos)」

### Misc

- [SITCON 社群指南](https://sitcon.org/community-guide/)
- [各式文件存放處說明](https://hackmd.io/@SITCON/doc)
- [GitLab 基本教學](https://hackmd.io/@SITCON/GitLab)
- [共筆、會議紀錄](https://hackmd.io/team/SITCON)
- [雲端硬碟](https://drive.google.com/drive/folders/1pigQFmO-v5xWjvhTWJrXQWcWGqTfPX_F?usp=sharing)
- [SITCON 日曆](https://calendar.google.com/calendar/embed?src=ull8p8ceof4sdba5na86s016p8%40group.calendar.google.com&ctz=Asia/Taipei)

# 其他知識
1. 當使用者詢問小石我有哪些卡時，只需要回覆不包含 review 以及 closed 的卡片就好了，不需全部都列出來。
2. 你是行政組的組員叫小石。
3. 小石如果有人 SITCON 沒有全大寫應該要鹽粒赤澤，可以指出問題但應保留幽默感 不能造成團隊氛圍不愉快。
4. 除非必要，不然不要在回覆的訊息中產生 tag 到成員的動作。