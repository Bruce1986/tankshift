# 工作日誌（WORKLOG）

> **這是專案的工作記錄：做過什麼、為什麼這樣做、接下來要做什麼。**
> 每次收工或 commit 前更新一次；記錄設計決策比記錄「做了什麼」更重要。
>
> **未來工作的單一真相在 [`project-handbook.md`](project-handbook.md#任務清單) 的「任務清單」**——
> 本檔不重複維護任務清單；每篇日誌以「下一步」收尾、指向任務編號即可。

## 當前狀態

| 項目 | 內容 |
|---|---|
| **進行中** | T-01（PR #1 審核中；合併後改 ✅、這格改「無」） |
| **下一步** | T-02 TypeScript 工具鏈＋CI（新增相依套件前先問 owner）；T-10 待 owner 確認 Gemini App 的 repo 存取設定後重貼 `/gemini review`（目前 ⛔） |
| **卡住／待拍板** | PR #1 的 `/gemini review` 沒有回應，疑似 Gemini App 沒裝在本 repo（見 2026-11-21 日誌），要 owner 確認 App 的 repo 存取設定 |

---

## 日誌（新的在上）

### 2026-11-21

#### 完成
- 從 `Bruce1986/repo-template` 建立 public repo。
- T-01：README（英文）、`README.zh-TW.md`、`docs/DESIGN.md`（英文）、WORKLOG、手冊的專案身份與任務清單；授權年份與著作權人；刪除範本自帶的 `template-selftest.yml`；移除多專案生態系與多 remote 的條件段落。

#### 問題 / 決策
- 決策 D1～D9 與其他裁決都記在 DESIGN §4，這裡不重抄。由來：
  - 2026-11-20：方向討論後，owner 拍板 D1～D4；同日外包前案調查。
  - 2026-11-21：owner 依建議拍板 D5～D9，以及前案報告列的六項待決。
- 前案調查報告是外部單位做的，報告本身沒有存進 repo。
  - 授權：OCHRE、HPWHsim、OpenDHW 是 2026-11-20 用 `gh api` 查 GitHub 的授權偵測結果；EcosizerEngine 的偵測結果是空的，同日用 `gh api …/contents` 列出根目錄確認沒有 LICENSE 檔，AGPL 是依它 README 的宣告。
  - 專利：描述引自調查報告與搜尋摘要，原文沒有重讀；法律狀態沒有評估，DESIGN §5 只註明「尚未評估」，不做有效或失效的斷言。
  - 報告說「HPWHsim 只能用 MSVC 建置」，但 2026-11-20 讀它 `.github/workflows/build-and-test.yml` 的 matrix，ubuntu、windows、macos 都有列（只看設定，沒查執行結果），所以這句沒有寫進文件。
  - 報告漏列 RWTH-EBC/OpenDHW（MIT），已補進 DESIGN §5。
- 本機 review（gemini-grade-review fix-loop）期間，DESIGN §2.4 與 §3 補了幾項設計。這些都是**提案，不是 owner 裁決**，`T_tap` 與容許差要到 T-05、T-07 才定：
  - 混水閥與「洗到冷水」的定義；
  - 循環管路改用熱損係數；
  - 桶邊、循環管路周邊、熱源側空氣、自來水四種隨季節變化的溫度輸入；
  - 電價日曆；
  - 退伍軍人菌時數在 v1 只是下限值；
  - §2.4 失敗規則：輸入驗證、節省必須同等服務才算（送出熱量變少、冷水澡或退伍軍人菌時數變多就標「不可比較」）、兩個情境用同一組用水事件並註明亂數種子（場所、地點日曆或年份不同就拒絕比較）、需求曲線分平假日與月份（用場所的地點日曆，跟電價自己的日分類分開）、沒有日期的典型年資料依月日對齊（閏年 2 月 29 日重複 28 日）、熱泵熱輸出隨室外溫度變化、日光節約切換日：有日期的輸入（團體事件）落在不存在或重複的時刻算輸入錯誤，每天重複的邊界（加熱時段、需求曲線）照固定規則處理並在報告註明、模擬年用當地曆年、資料涵蓋不足就停、UTC 分鐘格線與電價時鐘基準、加熱時段嚴格執行、COP 截斷比例要回報；另補「熱泵夜間偏樂觀」的已知偏差。「加熱時段外要不要允許安全破例」留到 T-03 問 owner。每項提案由哪個任務確認，見 DESIGN §2.4 的對照表。
- AGENTS.md、GEMINI.md 的修改：刪掉多專案生態系與多 remote 的條件段落；GEMINI.md 補上本專案注意事項。
- README 暫時不放聯絡信箱：`hotwater@brucejhang.com` 要等 owner 在 iCloud 開好再加，免得寄信的人收到退信。
- 這支 PR 除了 Markdown 還動到 `LICENSE` 和刪除一個 workflow，所以 `ci.yml` 會跑；但它目前只做 checkout、不驗證任何東西。真正的檢查在 T-02 補上。

- T-10 實測（PR #1）：2026-11-21 00:18:12Z 留言 `/gemini review`，到 00:39Z 為止 Gemini 沒有任何留言或 review，也沒有開 PR 時常見的自動摘要。同一時段 Gemini 在其他 repo 仍有回應（最晚一筆是 brucejhang_website#380 的 00:08:04Z、BrainSimulation#9 的 00:10:16Z），所以不是全面停擺。推測是 Gemini App 用「選定的 repo」方式安裝，新 repo 不在名單內；`gh` 的 token 沒有權限列出 App 安裝，無法確認，屬於推測。CodeRabbit 有留言，但說明本 repo 星數不到 10，不會自動 review。

#### 下一步
- owner 確認 Gemini App 的 repo 存取設定後，重貼 `/gemini review`（T-10）
- T-02
