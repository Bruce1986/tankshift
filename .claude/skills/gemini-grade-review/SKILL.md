---
name: gemini-grade-review
description: >-
  Perform a Gemini-Code-Assist-grade DETAILED code review using Claude itself
  (plus subagents), calibrated to a curated, high-signal review rubric. Two
  modes: a one-shot review (report by default, can post inline to a PR), and a
  fix-loop that reviews → applies fixes → re-reviews until a clean pass ("I have
  no feedback to provide"). Calibrates its closing tone like a real reviewer:
  praises a near-clean change, stays terse on a clean one. Use when the user
  wants a thorough / detailed / "as careful as Gemini" review by Claude, or wants
  it to "review and fix to convergence" — phrasings like "詳細 review", "深度
  review", "Gemini 級審查", "幫我仔細審一下", "像 Gemini 那樣仔細 review",
  "審到沒問題為止", "review 到收斂", "thorough review", "fix to convergence".
  CLAUDE-NATIVE (no Gemini API key, no Gemini call). Do NOT use when the user
  wants Codex (use codex-review), wants to drive the EXTERNAL Gemini-bot loop on
  a pushed PR (use pr-review-loop), or just wants the quick built-in /code-review.
---

# Gemini-Grade Review (Claude-native)

## Contents
- [Modes](#modes)
- [Prerequisites](#prerequisites)
- [Step 1 — Resolve the target](#step-1--resolve-the-target)
- [A review pass (one round)](#a-review-pass-one-round)
  - [A — Prep (deterministic)](#a--prep-deterministic)
  - [B — Pick depth by size](#b--pick-depth-by-size)
  - [C — Recall (review broadly)](#c--recall-review-broadly)
  - [D — Precision (verify each finding)](#d--precision-verify-each-finding)
  - [E — Anchor](#e--anchor)
- [Step 2 — Deliver, or loop](#step-2--deliver-or-loop)
- [Step 3 — Closing tone (calibrated, §0.1)](#step-3--closing-tone-calibrated-01)
- [Guardrails](#guardrails)

Produce a review with the **thoroughness of a top-tier AI code reviewer**, but
performed by Claude (+subagents). The depth comes from four things working
together: a fixed multi-axis **rubric** (so coverage never drifts to style nits),
**severity calibration** (high/medium only), a **two-pass recall→precision** flow
(catch broadly, then verify each finding), and **deterministic grounding**
(registry checks + diff-line anchoring done in code, not guessed). It also
**iterates to convergence** when asked to fix, and **calibrates its closing tone**
the way an experienced reviewer does (praise when it's nearly clean; a plain
"no feedback" when it's clean — never padded nits).

## Modes

- **Review (default).** Run one **review pass** (below) and report. Does not change code.
- **Fix-loop.** When the user asks to fix / "修到沒問題" / "審到收斂" / `--fix`:
  repeatedly run a review pass, apply the confirmed fixes to the working tree,
  and re-review **until two consecutive passes find nothing** ("I have no feedback to
  provide."), or a safety stop trips. Edits the working tree; **commit/push only on explicit
  confirmation.** This is self-contained (Claude is both reviewer and fixer) — for
  driving the *external* gemini-code-assist bot on a pushed PR, use `pr-review-loop`.

**Division of labor.** Claude/subagents do the *reviewing reasoning*. The
mechanical, get-it-exactly-right parts are delegated to a deterministic helper so
they never hallucinate. The helper scripts are **bundled in this skill dir inside
the repo**, so the path resolves regardless of where the repo is cloned:

```
TOOLS="$(git rev-parse --show-toplevel)/.claude/skills/gemini-grade-review"
python3 "$TOOLS/review_tools.py" <prep|anchor|post> ...
```

`review_tools.py` (sharing `review_lib.py`, both in this skill dir) handles:
fetching + boundary-safe truncation + parsing the diff, **supply-chain registry
grounding** (PyPI/npm), **line anchoring** to commentable diff lines, and
**posting** the inline review.

## Prerequisites

- `gh` authenticated and `python3` available.
- `python3 "$TOOLS/review_tools.py" --help` works. If the script is missing, tell
  the user and stop — the deterministic grounding/anchoring/posting depend on it.
- **Windows / venv:** `python3` often doesn't exist — in bash/zsh (e.g. Git Bash), set
  `PY="$(command -v python3 || command -v python)"` once (`command -v` is POSIX-only;
  in native cmd/PowerShell use `python` directly) and substitute `$PY` for the literal
  `python3` in every command in this doc. Run the *project's* tests with the project's
  own interpreter (e.g. `.venv/Scripts/python.exe -m pytest`), not necessarily the one
  that runs `review_tools.py`.

## Step 1 — Resolve the target

- **`/gemini-grade-review` (no arg)** → the current branch vs its default branch.
  Find repo with `gh repo view --json nameWithOwner` and the base branch
  (`git symbolic-ref refs/remotes/origin/HEAD` or `git branch -r`). No PR → report
  only (cannot post inline without a PR).
- **`/gemini-grade-review <PR>`** → that GitHub PR number.
- **Fix-loop needs a writable checkout.** For the current branch you already have
  one. For a PR, `gh pr checkout <NUM>` first so fixes land on its branch.

---

## A review pass (one round)

Steps A–E produce one round's **`confirmed` findings**. The fix-loop repeats this;
review mode runs it once.

### A — Prep (deterministic)

```
GGR=$(mktemp -d)   # per-run temp dir — don't hardcode /tmp/... or parallel reviews collide
python3 "$TOOLS/review_tools.py" prep --repo <owner/repo> --pr <NUM> --diff-out "$GGR/diff"
# branch mode instead of --pr:
python3 "$TOOLS/review_tools.py" prep --base <BASE> --diff-out "$GGR/diff"
```

Prints `{truncated, file_count, commentable_total, files[], deps[]}` and writes the
(truncated-at-file-boundary) diff to `$GGR/diff`. The **`deps`** entries are
confirmed supply-chain findings — an added dependency / exact-pinned version that
does NOT exist on PyPI/npm. **Scope:** grounding only covers added deps in
`requirements*.txt` and `package.json` (exact `==`/`x.y.z` pins; non-registry specs
like `workspace:`/`file:`/git/url are skipped — including `npm:` aliases, a known
false-negative). `pyproject.toml`, `setup.py`, and
lockfiles are **not** checked — so a clean `deps:[]` does NOT mean "all dependency
changes verified". Treat `deps` entries as **already verified**; they skip step C/D
and always count as confirmed. If `truncated` is true, say so (later files weren't reviewed).

> **⚠️ `prep` is committed-only in _both_ modes** — `--base` runs `git diff <base>...HEAD`,
> and `--pr` fetches `gh pr diff <NUM>` (the last-**pushed** state on GitHub), so neither
> sees uncommitted working-tree edits. In the **fix-loop**, fixes are applied to the
> working tree and left **uncommitted** between rounds, so re-running `prep` on later rounds
> reviews *stale* (pre-fix) code — the loop can't converge (it keeps re-finding fixed issues,
> or falsely declares clean). **From round 2 on, build the diff yourself from the working
> tree** and point reviewers at it + the real on-disk files:
> `git diff "$(git merge-base <BASE> HEAD)" -- <changed paths…> > $GGR/diff` — a plain
> commit as the left side (no `..`/`...`) compares the **working tree** against it, so
> uncommitted fixes are included; pinning it to the **merge base** keeps commits that
> landed on `<BASE>` after branching from leaking in as PR changes. Do **not** use
> `<BASE>..HEAD` or `<BASE>...HEAD` (both committed-only — they'd miss uncommitted
> fixes), and don't diff against the `<BASE>` tip directly (if base advanced, its
> upstream changes pollute the review diff).
> If a fix **adds a new file** (e.g. a regression test), `git add -N <file>` (intent-to-add)
> it first — `git diff` omits untracked files even when their path is listed explicitly.
> Keep using `prep` for round 1 (clean tree). If you re-run it on later rounds just for the
> `deps` supply-chain grounding, give it a **separate output** (e.g. `--diff-out "$GGR/deps.diff"`)
> so it can't clobber the working-tree diff at `$GGR/diff` that the reviewers/anchor consume.
> (Note: Codex `exec review --base` *does* see the working tree, unlike `prep`.)

Also read the repo's **`.gemini/styleguide.md`** if present — fold its rules in.

### B — Pick depth by size

- **Small diff** (≈ ≤ 8 files AND ≤ 600 changed lines) → **one fresh subagent**:
  spawn a single Agent-tool subagent to review the whole diff (a clean context — not
  the orchestrator self-reviewing).
- **Large diff** → **fan-out**: spawn one fresh subagent per file (or small group),
  each reviewing only its slice. Never one-shot a huge diff.

> **Always review the actual code under review.** Source each reviewer's input from
> the diff `prep` wrote (`$GGR/diff`) or the real files on the branch — never an
> assumed-identical copy elsewhere on disk, which may have drifted. In the fix-loop
> (round ≥ 2) that diff must be the **working-tree** diff (see the ⚠️ in step A), and
> tell reviewers to read the **current on-disk files** — which already carry the fixes.

> **Subagent output contract & depth.** The **orchestrator owns the fan-out decision**
> (step B) — decide depth once, hand each subagent a *scoped slice*. A subagent may use its
> tools freely and isn't forbidden from spawning children, but a reviewer handed an
> **already-scoped** slice **shouldn't silently re-fan-out**: it repeats the cold-start
> reading, breaks the Sonnet/Opus cost cadence (you don't control a grandchild's model), and
> each extra async layer is where a parent can surface a "still-waiting…" status instead of
> the findings. **Non-negotiable:** tell every review subagent its FINAL message must be ONLY
> the findings JSON array (no prose, no status) and not to poll/wait. Want a leaf that *can't*
> delegate? Pick an agent type without the Agent tool. Each fix-loop round, also **carry
> forward the confirmed deliberate-design decisions** ("X is intentional — don't re-flag") so
> later rounds don't reopen settled points.

> **Independence beats blind spots.** The author/orchestrator should not be the sole
> reviewer. Do the reviewing with **fresh subagents** (clean context) even on a small
> diff — self-review is blind to the author's own assumptions.
>
> **Model cadence (tracer rounds).** Run the fix-loop in cycles of **3 Sonnet rounds then
> 1 Opus round**: review subagents on **Sonnet** (`model: sonnet`) for cheap, broad coverage
> on rounds 1–3, then on **Opus** (`model: opus`) for round 4 as a deeper "tracer" pass —
> repeating (Opus on rounds 4, 8, 12, 16, 20). The round cap (20) is a multiple of 4, so the
> **last round before pausing is always an Opus tracer**. (One-shot review mode runs on the
> session model.)
>
> **Codex as a second reviewer.** At the **start of a review, check `which codex` and ASK
> the user** whether to add it. If yes, run `codex exec review --base <BASE>` from the repo
> — `<BASE>` is the diff base (branch mode: the default branch; PR mode: the PR's base
> branch / merge-base after `gh pr checkout`). Fold its Blocking/Non-blocking findings into
> step D. **In the fix-loop run Codex on round 1 and again on the first clean pass** (a final
> independent cross-check before declaring convergence) — not every round (it's costly). If
> `codex` is absent, errors, or returns nothing parseable, proceed Claude-only and note it.

### C — Recall (review broadly)

Apply the rubric; flag anything genuinely suspicious (the next step filters). Each
finding = `{path, line, severity, issue, suggestion}` — `line` is the new-file line
number, `severity` ∈ {high, medium} only, `issue` is a full causal chain,
`suggestion` is a directly-applicable snippet (or "").

#### Rubric (high-signal checks)

排序大致依實測命中率，但**別把順序當成配額**——它隨 diff 的性質大幅擺動。（2026-07 語料實測：一份以 `.md` 為主的 diff，55% 的意見落在文件與一致性軸、資安 0 條；純程式 diff 則相反。**先看這個 diff 是什麼，再決定重心。**）

**文件／設計類 diff（`.md`、ADR、規格書）——別只審排版：**
- **方法論與領域事實**：提案的做法在該領域**是否真的成立**——量測會被什麼干擾（如戶外攝影機的自動曝光 AEC 會把畫面平均亮度拉回定值，Luma 無法粗篩晴/陰）、依賴的資料源**是否真的提供該欄位**（如 CWA 自動雨量站不提供雲量／日照輻射）、演算法在該資料維度下是否良定義（高維 embedding 的「中位數向量」、DBSCAN 的 `eps`）、有無循環定義（先剔除離群再算群心）。也要問**在地／實務約束**：這條規則落到真實世界會不會誤傷正當案例（如硬性要求 `archive_url`，會誤殺 MOPS、主計總處這類設防爬蟲但完全可信的權威來源）。
  > 這一軸是本 rubric 最容易被跳過、而漏掉的損失最大的一項——實測有意見是 Claude 與 Codex **十五輪都沒抓到**、只有帶入在地實務知識才看得見。
- **文件內部自我一致**：規範之間互不矛盾，**特別是與文件自己宣示的原則**（「X 必填」vs 同文件的「寧缺勿假」）；交叉引用的章節編號正確（§6.1 的內容其實在 §7.1）；ToC 連結文字與實際標題**逐字**相符（別漏括號補充）；符號定義跨段一致。
- **對外部系統行為的事實斷言要查證**：文件寫「`git worktree add --no-checkout` 建立時 index 是空的」——這是錯的（index 會預載 tree）。文件斷言工具行為時，**照抄等於背書**，要實際驗過再放行。
- **排版**：標題層級一致、渲染正確、長文件加目錄(ToC)。

**程式類 diff：**
- 一致性：與同檔/姊妹模組/專案自家 styleguide 既有寫法對齊（同樣的 `ensure_ascii` / 去重粒度 / 容錯鍵），別只在新增處特立獨行
- None/空值：取值後明確判斷 `is None`（如 `v = d.get("k")`，再判 `v is not None`）；慎用 `.get(k) or ""`，它會把合法 falsy 值 `0`/`False`/`[]` 也吃掉
- 例外處理：每個 I/O / 解析 / 外部呼叫點列出會拋的**具名例外**（JSONDecodeError / ValueError / FileNotFoundError…）並確認都攔了；禁裸 except
- 資源：檔案/連線/鎖用 `with`；邊界：空輸入、越界、off-by-one、除以零、本地路徑/子目錄等輸入變形
- 型別/結構防禦：外部 JSON/API 回傳先 `isinstance` 再用（dict vs list），**巢狀欄位逐層查**（releases/versions/items 也可能非預期型別）；公開函式對非預期輸入型別要安全淘汰、不崩
- **語意陷阱（語法看起來對、語意有坑）**：`getattr(o, "k", default)` 的 `default` 會**先被求值**（有副作用或昂貴就中招，不是 lazy）；JS `typeof NaN === "number"`、`typeof Infinity === "number"`（值域檢查要用 `Number.isFinite`）；`docs || []` 對**非陣列物件**不會退回空陣列（錯誤回應 `{detail:…}` 會原樣通過，隨後 `.filter` 爆 TypeError）；`.get(k) or ""`（同上）
- 重複邏輯 → 抽共用；硬編碼常數/magic number → 抽出或設定化
- 測試：新邏輯有對應單元測試（正常 + 邊界 + 例外路徑）；**測試非恆過**——斷言鎖得住行為（預期值不照抄實作輸出、沒把被測邏輯 mock 掉、非永真斷言）；新寫或大改（＝變更斷言或被測對象）的測試缺紅綠驗證紀錄（紅須為該測試自身的斷言失敗）或豁免理由（PR 說明；branch mode 無 PR 時查 WORKLOG.md／commit message）→ medium
- **外部工具設定檔**（`.coderabbit.yaml`、CI workflow、linter/formatter config）：鍵名拼法（snake_case vs camelCase）、**巢狀層級**、值的語意，都要對**官方 schema／文件**核對。錯放一層會**靜默失效**（設定看似生效實則吃預設值）；而 schema 若是 `additionalProperties:false`，拼錯一個鍵會讓**整份設定驗證失敗**。這類斷言必須查證，不可憑印象——見 Guardrails。
- 競態：共享狀態 / 計時器 / async 重入
- 時區：時間戳用帶 tzinfo 的 UTC，不用 naive `datetime.now()`
- 跨平台/locale：`subprocess` 帶 `encoding="utf-8"`+`errors="replace"`（CP950/Big5/二進位不崩）；輸出串流只在**入口**reconfigure（庫別動全域）；大參數走 stdin 避 ARG_MAX
- 資安：SQL 參數化、`subprocess` 不用 `shell=True`、無命令注入、不硬編碼密鑰

**Severity:** high = 崩潰 / 資安 / 資料遺失 / 破壞既有行為；medium = 健壯性 / 正確性邊界 / 效能 / 例外處理。**Do not emit low.** Don't pad.

**同一規則多處違例 → 一條 finding，不是 N 條。** 外部 bot 的典型弱點是把同一條 styleguide 規則的每個違例各開一條分散的 inline（實測同一規則連開 3-4 條）。改成：輸出**一條** finding，`issue` 說明規則、附上**全量違例清單**（`path:line` 列舉），`suggestion` 優先給**程式化重寫**而非逐處貼片——重建整份 ToC 一次消掉 20 條同類風險，勝過套 20 個 suggestion。

**套用任何 suggestion 前先界定適用邊界。** 規則很少全檔通用：LaTeX `$…$` 在 **code fence 內不會渲染**，所以 schema 區塊與 ASCII 流程圖裡的 `>=` 必須維持純 ASCII。bot 的 suggestion 不會幫你切這條線——`issue` 要寫明適用範圍與例外，否則修正本身就是新缺陷。

### D — Precision (verify each finding)

Don't ship the recall list raw. Verify with a **fresh verifier subagent** — a
*different* one than found the issues (worker ≠ verifier breaks correlated errors).
It returns each candidate with `keep: true|false`, dropping ones unsupported by the
diff, pure style nits, duplicates, or wrong; borderline-but-real re-graded to medium
(never low). Keep only `keep:true`. **If Codex was run, merge its findings here too**
(dedup by file + line + gist).

The `deps` findings from step A are already verified — don't re-judge them.

### E — Anchor

Merge `deps` + verified findings into one list = **`confirmed`**; anchor to real lines:

```
python3 "$TOOLS/review_tools.py" anchor --diff $GGR/diff --findings $GGR/findings.json
```

Returns `{anchored[], unanchored[]}` (anchored lines snapped when needed; unanchored
go in prose, not inline).

---

## Step 2 — Deliver, or loop

### Review mode → report (with the tone in Step 3)
Present a one-paragraph summary, the anchored findings grouped by file
(`path:line` · severity · issue · suggestion), any unanchored ones, and a
truncation note if any. **Posting is opt-in:** only on an explicit ask ("貼上去",
`--post`) AND a PR, after confirming, run
`review_tools.py post --repo <r> --pr <N> --diff $GGR/diff --findings $GGR/findings.json --summary-file $GGR/summary.txt`.

### Fix-loop mode → iterate to convergence
Track: `round`, the set of **open findings by key** (`(path, issue[:40])` — deliberately
**line-independent**, since each applied fix shifts downstream line numbers; this is NOT
`review_lib.dedup`'s key, which also includes `line`), `clean_streak`, and which keys
you've already attempted.

**Before round 1 — baseline the tests.** Run the project's test/validation command
once (`{{TEST_COMMAND}}` if the repo defines one, else `pytest` / `npm test` / etc.)
and record which tests fail *now*. If none is discoverable, note "no tests to validate
against" and proceed without the test gate (changes will be flagged unvalidated).

Each round:
1. Run a review pass (A–E) on the **current working tree** → `confirmed`.
2. If `confirmed` is empty, `clean_streak += 1`; **else apply the fixes one at a time**
   (use `suggestion`; else implement what `issue` describes), and reset `clean_streak = 0`.
   After **each** fix, re-run the tests and compare the **set of failing test ids** to the
   baseline: if any test **not in the baseline set** now fails (even if the total count is
   flat or lower), **revert that single fix** and keep the finding unresolved. Pre-existing
   (baseline) failures never count — don't revert a good fix because the suite was already
   red. A *newly added* test that the fix itself introduced and that fails is a **finding,
   not a regression** — don't revert for it. Re-run a single new failure once before
   reverting (flake guard); if a fix changes the test command/config, **re-baseline**. If
   no test command exists, apply the fix but mark the change **unvalidated**.
3. Update open-finding keys; a key you attempted that **survives unchanged**
   (same `(path, issue[:40])`) → mark *no-progress*, stop re-attempting it.
4. Go back to 1 — re-diff the **working tree**
   (`git diff "$(git merge-base <BASE> HEAD)" -- <changed paths…> > $GGR/diff`, **not**
   `prep` — both `--base` and `--pr` are committed-only; see the ⚠️ in step A) and re-review.

**Convergence:** declare done only after **two consecutive clean passes**
(`clean_streak ≥ 2`) — the reviewer is an LLM and noisy, so one clean pass isn't a
stable fixpoint. Then go to Step 3 and emit the clean closer with a note of what was fixed.

#### Safety stops (report what's open, never loop forever):
- **Round cap (default 20)**: on reaching it, pause, summarize progress + open findings,
  and **ask the user whether to continue** for another batch — never silently keep looping
  (it burns Claude quota). 20 is a multiple of 4 so the cap lands on an **Opus tracer** round
  (see model cadence); a self-contained loop usually converges in a few rounds, so 20 is a
  generous backstop.
- **Churn ceiling**: track the *attempted-but-still-open* key set (findings you tried to
  fix that remain open). If that set does **not strictly shrink for 2 consecutive rounds**
  — fixes keep trading one finding for another (oscillation) rather than reducing the
  backlog — stop and report. (Measure shrinkage on the attempted subset, not the whole
  `confirmed` set, which can legitimately grow as new issues surface.)
- **No-progress**: a finding whose key survives a fix attempt → human judgement; stop
  retrying it. (It stays in the attempted-but-still-open set on purpose — if no-progress
  findings are all that's left, that set stops shrinking and the churn ceiling cleanly ends the loop.)
- **Tests**: only baseline-*new* failures block a fix; if no tests exist, flag the run
  as **unvalidated** in the closing summary (don't claim green).
- **No outward actions mid-loop**: never commit/push between rounds. After
  convergence/stop, summarize and **offer** to commit/push — only on confirmation.

## Step 3 — Closing tone (calibrated, §0.1)

Match an experienced reviewer's register to how clean the result is:

- **Clean (zero findings / converged):** say it plainly —
  `I have no feedback to provide.` (optionally `…as the implementation is robust and well-tested.` / 「整體而言這是一次很棒的改進。」). In fix-loop, add a short note of what was fixed across the rounds.
- **Near-clean (a few findings, none `high`):** **lead with specific praise** of
  what's done well (e.g. "Overall, this is an excellent contribution." / 「整體而言這是一次很棒的改進。」), *then* the suggestions. (A `high` finding is never "near-clean" — use the next bucket.)
- **Real / blocking findings:** just the findings — no forced praise.

Never invent nits to look busy; high/medium only.

## Guardrails

- Calibrate severity: **high/medium only**, lean medium, don't pad. Calibrate tone (Step 3).
- Deterministic facts (does this package/version exist? which line is commentable?)
  come from `review_tools.py`, never from guessing.
- When a finding hinges on an **external system's behavior/syntax** (an API's query
  grammar, a service's error codes, **a config file's schema**, a CLI flag's actual
  effect), **verify empirically** — one minimal live call, or the official schema/doc —
  before accepting or fixing it; don't take the reviewer's (or Codex's, or Gemini's) word.
  Recorded cases where this saved a bad fix:
  - a Codex "invalid Overpass `out` clause" P1, disproved by a single live query;
  - **config schema is where bots confabulate most confidently.** On one PR, Gemini made
    two separate claims about `.coderabbit.yaml`, cited the schema for both, re-raised
    each across rounds — and **both were fabrications with the mechanism inverted**:
    (a) that `!`-prefixed globs in `path_filters` mean *include* (official `schema.v2.json`:
    "ignoring excluded ones (starting with '!')" — `!` **excludes**; applying the fix would
    have flipped the config into an include-only list and made CodeRabbit review *nothing
    but* the data files); (b) that `filePatterns` must become snake_case `file_patterns`
    (the schema defines only `enabled` and **`filePatterns`** — the "fix" would have
    introduced the unrecognized key it warned about). Both rejected by quoting the schema
    back. **Confidence, schema citations, and re-raising across rounds are not evidence** —
    open the actual schema, per key, every time.
  - 同理，文件裡對工具行為的**事實斷言**（"`--no-checkout` 時 index 是空的"）在照抄或引用前要實際驗過。
- Never post, commit, or push without explicit confirmation. Branch mode never posts.
- Reviewing is done by **fresh subagents** (Sonnet rounds 1–3, **Opus every 4th round** as a
  tracer; + Codex if the user opts in at the start), not the author.
- Fix-loop bounds itself: **20-round cap (then ask to continue; ends on an Opus tracer)** + churn-ceiling +
  no-progress stop + a **baselined** test gate (only new failures block a fix).
  Convergence needs **two consecutive clean passes**. For driving the external
  gemini-code-assist bot on a pushed PR instead, use `pr-review-loop`.
