# Gemini 執行指南

@AGENTS.md

> 本檔只保留 **Gemini 專屬差異**。通用協作規範（驗證指令、PR Review 流程、
> 程式碼審查原則、思考與判斷原則、撰寫程式碼的原則、commit 慣例、git push 安全守則）已透過
> 上方 `@AGENTS.md` import 併入；若你的執行環境不支援 `@` import 語法，
> 請先完整閱讀 [`AGENTS.md`](AGENTS.md) 再繼續。

---

## Review 指令與帳號

本檔專屬 Gemini，刻意寫死、不用 `AGENTS.md` 的 `{{REVIEW_COMMAND}}` / `{{BOT_USER_LOGIN}}` 占位符：

- 觸發 review：`/gemini review`
- Bot 帳號：`gemini-code-assist[bot]`

> ℹ️ **版本註記（2026-08-01 更新）：** 曾預告免費版 `/gemini review` 於 2026-07-17 終止服務，
> **但該關閉並未實際生效**——實測 2026-07-28 收到的是 daily quota 警告（服務活著、只是額度
> 用完），2026-07-31 再貼則在 60 秒內回了完整 review。所以線上 Gemini 目前有三種可能狀態：
> 免費額度仍可用、付費／Google Cloud 版、或自建 review bot。
>
> **本檔是範本，不能替你的 repo 斷言是哪一種**：付費／GCP 版是帳號／專案層級的訂閱，
> 不隨範本檔案複製；免費額度則隨時可能限流。`/gemini review` 是否真的會有回應，
> **一律以實測為準**——在 PR 留言後確認 bot 真的產出審查內容（配額警告、rate limit、
> 只回摘要都**不算**）。不要試圖從回覆速度或內容回推是哪一版在服務。
>
> 線上這道是**第二關**：必須先跑本機 `gemini-grade-review`（Claude-native 或 Codex-native）
> 審到收斂，再推 PR 跑線上，兩道都收斂才可合併
> （見 [`AGENTS.md`「PR Review 流程」](AGENTS.md#pr-review-流程)）。
>
> 若實測**完全沒有任何回應**（等滿約 10 分鐘、PR 上沒有 bot 留言；⚠️ **判定「零回應」之前，
> 必須先依 [`.claude/skills/pr-review-loop/SKILL.md`](.claude/skills/pr-review-loop/SKILL.md)
> 的「Post the trigger」與 6b 回讀確認觸發留言真的貼出去、且內容未被 Windows 的 Git-Bash
> 改寫成 `C:/Program Files/Git/...`——沒貼成功所產生的觀察與「真的沒有 bot」完全相同，
> 而本註記路徑的結局是合併，所以這道前置在這裡比在下方更不能省**），**且本 repo 從未
> 收到過任何 review bot 留言**（含配額警告，須掃全 repo 歷史而非只看本 PR；
> `github-actions[bot]`／`dependabot[bot]` 等 CI bot **不算**，掃描指令見 `AGENTS.md`。
> ⚠️ **掃描本身失敗、或無法涵蓋全部 PR 時，空輸出不算「從未收到過」**——掃描壞掉的
> 輸出與乾淨的結果一模一樣，此時不得套用本註記路徑，須把已查到的範圍與卡住的原因寫進
> PR 交由使用者裁決，未取得明示裁決前不得以「無線上 gate」名義合併。
> ⚠️ **另外還要跑 `AGENTS.md` 那支腳本一併輸出的 census，並把它列出的每個 bot 帳號
> 與每個 check/status 名稱逐一分類完**——白名單的零命中只證明「不是那幾個 bot」，
> 不證明「沒有 review bot」；只發 status 而不留言的 reviewer 只會出現在 check/status
> 那一欄，漏看它就是本註記路徑最容易踩到的假豁免，
> 沒分類完就不算「從未收到過」）：本機那道收斂後、
> 在 PR 明確註記「無線上 gate」即可合入（不會因此永久卡住），但應儘早補上線上 gate。
>
> ⚠️ 以下**兩種**都**不適用**上述註記路徑：
> - **配額警告／rate limit**：bot 回得出額度不足，就代表它已安裝且處理了這則留言，屬
>   「這次沒審成」，等視窗重置後重跑。**但視窗不保證會重置**：若重跑已跨過至少兩個
>   配額視窗（約 48 小時）仍每次都拿不到真正的審查內容，就把重跑次數、時間點與每次
>   收到的回覆寫進 PR，**交由使用者裁決**（同下方兩支的處置）——不得讓它無限期卡住。
> - **本 repo 曾經有過 bot 回應、這次卻靜默**：屬「沒送到／沒審成」。先回讀確認觸發留言
>   真的貼對——Windows 的 Git-Bash 會把 `/gemini review` 改寫成
>   `C:/Program Files/Git/...` 路徑，Gemini 根本收不到，看起來就跟「沒有 bot」一模一樣；
>   成因、正確貼法與回讀驗證指令見
>   [`.claude/skills/pr-review-loop/SKILL.md`](.claude/skills/pr-review-loop/SKILL.md)
>   的「Post the trigger」與 6b。確認無誤後再重貼／等待後重跑。
>
> 第二種的**出口**（這是可以走註記路徑的情形，不在上述兩種之列）：查出**積極證據**
> 顯示 bot 已下線（App 被移除／seat 被收回），即**適用同一套合併條件**（本機收斂後於 PR
> 註記並附上該證據）。注意這裡不能寫成「比照『從未接過』」——「從未接過」的前提是
> 全歷史掃描三個留言端點都空**且 census 的帳號與 check/status 名稱都已分類完**，
> 而本情形依定義曾收到過 bot 留言、
> 掃描必然非空，永遠滿足不了；
> 詳見 [`AGENTS.md`「尚未接線上 bot 的專案怎麼辦」](AGENTS.md#尚未接線上-bot-的專案怎麼辦)
> 一節的「確認為第一列或第三列之後」。
> 查安裝狀態只能看網頁 `https://github.com/<owner>/<repo>/settings/installations`
> （需 repo admin）。**別用 `gh api repos/.../installation`**——那是 GitHub App 的
> JWT 專用端點，使用者 token 一律回 401（實測），有沒有 admin 都一樣，也永遠不會回
> 404 或 `app_slug`。三種結果各有出口：
>
> - **頁面查得到、App 已不在** → 積極證據成立，適用上述合併條件。
> - **頁面查得到、App 仍在**，而重貼／等待跨過約 48 小時仍全程零回應：確認 installation
>   的 repository access 是否涵蓋本 repo、該 repo 有無 seat；仍查不出原因就把紀錄寫進 PR
>   **交由使用者裁決**——這一支既拿不到「已移除」的積極證據、也不算權限不足，
>   沒有這條會永遠卡住。
> - **沒有 repo admin、設定頁打不開**（CLI 沒有替代路徑，見上）：把重貼次數、等待時間與
>   查核受阻的原因寫進 PR，請使用者裁決；使用者確認 bot 已不在服務後同樣適用上述合併條件
>   （不是「比照從未接過」，理由同上）——**不得**因為拿不到證據就讓合併無限期卡住。
>
> 完整判準表見 [`AGENTS.md`「尚未接線上 bot 的專案怎麼辦」](AGENTS.md#尚未接線上-bot-的專案怎麼辦)。

---

## ⚠️ Gemini 讀不到 inline reply

Gemini Code Assist 讀不到 review comment 的 inline reply 留言串。直接回覆在
review comment 上的內容，下一輪審查不會被讀到，Gemini 會再次提出同一條建議。
若要反駁某條建議（事實有誤、或先前已評估後不採納），**請把理由直接寫成該行
程式碼的註解**——Gemini 會重讀 diff，寫在程式碼裡的說明才真正傳達得到。建議
用可辨識的標記，例如 `// Gemini-review: intentional — <為什麼這段是對的>`。
（給人類 reviewer 看的 inline reply 仍可保留，但別指望它傳到 Gemini。）

---

## 執行環境注意事項

預設執行環境為 **macOS**。執行前先確認當前系統，避免使用錯誤的指令。

### 指令相容性

| 需求 | 建議用法 |
|------|----------|
| 刪除檔案 | `rm <file>` |
| 刪除目錄 | `rm -rf <dir>` |
| 跨平台檔案操作 | 用腳本處理（例如 `temp_ops.py`） |

### Git Commit Message

若訊息含特殊字元，改用檔案方式提交：

```bash
git commit -F COMMIT_EDITMSG
```

commit message 格式見 [`AGENTS.md`「Commit 慣例」](AGENTS.md#commit-慣例)。

---

## 專案特定注意事項

- 對外文件（README、`docs/`、程式碼與註解）用英文；`README.zh-TW.md`、`WORKLOG.md`、`project-handbook.md` 用正體中文。
- 本專案**不讀、不複製** AGPL 授權的 EcosizerEngine 原始碼；儲熱桶模型只依 EnergyPlus 公開工程手冊實作。review 時若看到疑似從 AGPL 專案搬來的程式碼，請標出。
- 單位依 DESIGN §2.3：°C、K、L、W 或 kW、kWh，並寫在欄位名稱的字尾；功率欄位要標明是熱輸出還是電輸入。預設數值（電價、碳排係數、用水曲線）都要在資料檔裡附來源與年份。

---

## PR Review 自動化循環

唯一權威版本在 [`.claude/skills/pr-review-loop/SKILL.md`](.claude/skills/pr-review-loop/SKILL.md)；
本檔不另抄流程（避免漂移）。對 Gemini 的差異只有一處：觸發指令與 bot 帳號用本檔
上方寫死的值（`/gemini review`、`gemini-code-assist[bot]`），不用占位符。
