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

### Misc

- [SITCON 社群指南](https://sitcon.org/community-guide/)
- [各式文件存放處說明](https://hackmd.io/@SITCON/doc)
- [GitLab 基本教學](https://hackmd.io/@SITCON/GitLab)
- [共筆、會議紀錄](https://hackmd.io/team/SITCON)
- [雲端硬碟](https://drive.google.com/drive/folders/1pigQFmO-v5xWjvhTWJrXQWcWGqTfPX_F?usp=sharing)
- [SITCON 日曆](https://calendar.google.com/calendar/embed?src=ull8p8ceof4sdba5na86s016p8%40group.calendar.google.com&ctz=Asia/Taipei)