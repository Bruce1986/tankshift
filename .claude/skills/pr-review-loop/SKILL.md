---
name: pr-review-loop
description: Run an automated review-iteration loop on a GitHub pull request — fetch review comments (including from the Gemini Code Assist bot), sync the branch, fix the code, run tests, commit + push, post `/gemini review`, wait, and repeat until Gemini has no further feedback. Trigger this skill whenever the user says things like "跑 PR review 循環", "開始 REVIEW 循環", "處理 PR 的留言", "把 PR 的 comment 跑完", "run the PR review loop", "iterate on PR feedback", "keep fixing until Gemini is happy", or any similar phrasing that implies a repeating fix→push→re-review cycle on a pull request. Use this even for short phrasings like "handle the PR comments" or "loop on the PR" — they mean this workflow. Do NOT trigger for one-off requests like "fix this one comment" or "look at the PR".
---

# PR Review Loop

Iterate on a GitHub PR: fetch comments → sync → fix → test → push → request Gemini review → wait → repeat, until the Gemini Code Assist bot (or equivalent reviewer) has no more feedback or a blocker forces a pause.

## Why this exists

Gemini Code Assist on GitHub gives iterative review feedback, and the natural workflow is a loop: address comments, push, ask Gemini to re-review, wait, repeat. Doing this by hand is mechanical and error-prone (wrong PR number, forgotten `git pull`, pushing before tests pass). This skill encodes the loop so it runs consistently without losing track of state across iterations.

## Preconditions — confirm every time

Even if you think you know the PR, **always confirm** before starting the loop. Wrong PR numbers and branch mismatches are the most common cause of wasted iterations.

1. **Identify the PR.**
   - If the user provided a PR URL or number, echo it back: `要跑的是 PR #123 (<url>)，對嗎？`
   - Otherwise auto-detect from the current branch:
     ```bash
     gh pr view --json number,title,url,headRefName,baseRefName,state
     ```
     Then confirm: `目前分支對應 PR #123「<title>」，就是它嗎？`
   - Only proceed after explicit confirmation.

2. **Confirm the test command.** Auto-detect (see *Detecting the test command* below) and confirm: `這輪測試會跑 <cmd>，OK 嗎？`

3. **Confirm the wait interval.** Default is 150 s for the first check after posting `/gemini review` — optimistic, because when GitHub/Gemini isn't busy the response often lands that fast, and the retry step below catches the slower cases. Comfortably under the ~5 min prompt-cache TTL so it stays cheap. Let the user override: `第一次等 150 秒檢查 Gemini 回覆，要調嗎？`

4. **Confirm the review bot is live — by testing, not by assuming.** A free-tier `/gemini review` shutdown was announced for **2026-07-17**, but empirically it did **not** take effect: on 2026-07-28 the bot replied with a *daily quota* warning (service alive, just rate-limited), and on 2026-07-31 it posted a full review within 60 seconds. So a repo may be served by free quota, paid/GCP Gemini Code Assist, or a self-hosted bot — **never infer from the sunset date that the bot is gone**, and don't try to infer which tier is serving you from response speed or content. If the repo genuinely has no live bot the loop waits forever, so confirm before starting: `這個 repo 接的 review bot 還在服務嗎（實測留言確認，不要用日期推定）？`

5. **Offer a local `gemini-grade-review` pass first.** Each external round costs minutes of wall time and Gemini quota; the local skill is same-session and catches most of what the bot would raise. Ask once:
   `要先跑一輪本機 gemini-grade-review 再進循環嗎？（能省下 1-2 輪外部往返）`
   Skip the offer if the branch already has a GGR run this session, or the user declines.

   > **Why this is here.** Corpus audit (2026-07-31): of 41 PRs that received Gemini bot comments in one week, **only 3 had any local GGR run**. The gap between the two reviewers is mostly *"we never ran it"*, not *"the rubric missed it"* — so the cheapest quality win is running the local pass at all.

## The loop

Repeat until a termination condition fires.

### 1. Fetch PR state and comments

Run these in parallel — they're independent (`$PR` is the PR number, e.g. `PR=123`; using a shell var avoids the unquoted-`<N>` redirection trap):


```bash
gh pr view $PR --json number,headRefName,state,mergeable,reviewDecision,url
gh api "repos/{owner}/{repo}/pulls/$PR/comments" --paginate   # line comments on the diff
gh api "repos/{owner}/{repo}/issues/$PR/comments" --paginate  # general PR comments (where bots often post)
gh api "repos/{owner}/{repo}/pulls/$PR/reviews"  --paginate   # review verdicts
```



Build a list of **actionable items**: authored by a reviewer (human or bot), not already resolved/outdated, and not already handled in a previous iteration of this loop.

Track handled comment IDs in memory across iterations so you don't re-do them. No need to persist — the loop is one continuous session.

### 2. Sync with remote

Between iterations, a human reviewer, a bot, or the user may have pushed. Always resync:


```bash
git fetch origin
git status                          # working tree clean?
git rev-parse --abbrev-ref HEAD     # still on the PR branch?
git pull --ff-only origin "<branch>"
```



If `git pull` hits a conflict, the working tree is dirty, or you're on the wrong branch — **stop and ask the user**. Do not force-resolve or switch branches silently.

### 3. Address the comments

For each actionable comment, decide which of three paths it falls into:

**(a) Agree → make the edit.**

- Read carefully, note file + line for line-level comments.
- **Keep tests honest.** If the comment changes the function's contract, update the corresponding tests to match. Don't `.skip` or delete tests to make CI green — that's hiding the problem.

**(b) Disagree (factually wrong, or already considered and rejected) → push back IN THE CODE, do NOT pause.**

The default for plain disagreements is to push back rather than stop. The reviewer (Gemini included) sometimes misreads types, miscounts edges, or repeats a suggestion already rejected in a prior round. When that happens:

> ⚠️ **Gemini does NOT read inline reply threads.** A reply posted on the review comment (`.../comments/<id>/replies`) will not be seen on Gemini's next pass — it will just re-flag the same suggestion. To make the rationale reach Gemini, **put it in the code itself**: Gemini re-reads the diff, so a comment on the relevant line is what actually lands.

- Look for prior context: scan recent commit messages on the branch (`git log` since base) for a `Rejected:` / `Hold:` block that already covers this suggestion. If found, cite that rationale.
- **Write the rationale as a code comment** on the exact line/block Gemini flagged, in the file's own comment style. Keep it tight — one or two lines — and make it explain *why* the current code is correct so the comment earns its place even after the review is over. Prefer a recognizable marker so the final sweep can find it, e.g.:
  ```ts
  // Gemini-review: intentional — <short reason this is correct>. See <file:line> / commit <sha>.
  ```
  This both pushes back at Gemini (it sees the comment in the re-read diff and can drop the suggestion) and leaves a durable in-code record for future readers.
- Optionally also post a short top-level PR comment or inline reply for the *human* reviewers' benefit — but never rely on it to reach Gemini; the code comment is the load-bearing one.
- Commit and push the code comment like any other change (it goes through the normal step 5 commit), then continue the loop. Re-running `/gemini review` after pushing gives Gemini a chance to drop the comment once it sees the rationale in the diff.

**(c) Design decision → pause and ask the user.**

If the comment touches architecture, data model, security tradeoffs, scope changes, public API shape, or anything where the user's judgment matters more than yours — stop and check in. Show the comment and the options. Don't guess on things the user would want to weigh in on. Genuine ambiguity (you can't tell what the comment is asking for) also belongs here.

The line between (b) and (c): if you can disprove the comment by pointing at a specific file:line or prior commit, it's (b). If reasonable engineers could disagree, it's (c).

### 4. Run tests

Use the auto-detected command. If tests fail:

- Fix iteratively. Understand the failure, fix the root cause, re-run.
- If the same failure recurs after a couple of attempts and suggests the original comment's fix is wrong, stop and ask the user.
- Never skip failing tests or bypass hooks (`--no-verify`) to push green.

### 5. Commit and push

Follow the project's commit convention — if its `CLAUDE.md` / `AGENTS.md` defines one, that wins. Otherwise default to Conventional Commits. Generally: one commit per logical change; don't bundle unrelated fixes.


```bash
git add "<file1>" "<file2>"         # one quoted path PER FILE; avoid `git add -A`
git commit -m "<type>(<scope>): <description>"
git push
```



Never `git push --force` on a PR branch without explicit user approval — other reviewers may be looking at specific commit SHAs.

### 6. Request Gemini review

**Capture the baseline comment id FIRST (6b below), then post the trigger, then re-read and compare — all before any waiting.** The id capture is part of posting, not a poll-time afterthought. Posting before you have the baseline makes the check un-runnable: `PREV` would then equal `NEW`, so `[ "$NEW" -gt "$PREV" ]` reports "the post silently failed" on a round where it actually succeeded — and the prescribed remedy (repost) would burn a round and mask whether the first post worked.

**Post the trigger — platform matters:**

- macOS / Linux:
  ```bash
  gh pr comment "<N>" --repo "<owner>/<repo>" --body "/gemini review"
  ```
- **Windows — do NOT post `--body "/gemini review"` from the Bash tool.** Git-Bash/MSYS rewrites any argument *value* that starts with `/` into a Windows path, so `/gemini review` silently becomes `C:/Program Files/Git/gemini review` and Gemini never receives the command. Use one of:
  - the **PowerShell tool** (no MSYS, simplest): `gh pr comment "<N>" --repo "<owner>/<repo>" --body "/gemini review"`, or
  - a **body file** (tool-agnostic; also the fix for non-ASCII bodies): write `/gemini review` into a UTF-8 file **with no trailing newline** — `printf '%s' '/gemini review' > trigger.txt`, *not* `echo`, which appends an LF that GitHub keeps in the body — then `gh pr comment "<N>" --repo "<owner>/<repo>" --body-file C:/Users/.../trigger.txt` (the arg now starts with `C:`, so MSYS can't mangle it), or
  - if you must use Bash `--body`, prefix `MSYS_NO_PATHCONV=1`.
  - This applies to **any** leading-slash `gh` value (`/review`, `/codex`, `gh api` paths starting with `/`), on any subcommand. `@coderabbitai review` is *not* affected (`@` isn't a path).

**6b. Verify the posted body (mandatory, before any waiting):** assert the comment you just posted has body exactly the trigger you meant to send, and does **not** contain `Program Files/Git`.

> **The trigger is `{{REVIEW_COMMAND}}`, not necessarily the literal `/gemini review`.** This
> section writes `/gemini review` throughout because it is the default, but `AGENTS.md`
> 「尚未接線上 bot 的專案怎麼辦」explicitly sanctions a self-hosted bot's own command, and the
> 48h-clock block below already parameterizes it (`TRIGGER='{{REVIEW_COMMAND}}'` plus an
> unreplaced-placeholder guard). Do the same in the two verification blocks below: on such a
> repo, asserting the literal `/gemini review` fails on a *correctly posted* trigger and sends
> you into a pointless delete-and-repost — the exact wasted round this check exists to prevent.
>
> **Compare case-insensitively.** The trigger itself is not case-sensitive: measured on
> `Bruce1986/repo-template` PR #28, the comment `/Gemini review` (capital G, id 5218030656,
> 2026-08-07T14:02:47Z) drew a full review from `gemini-code-assist[bot]` four minutes later
> (review 4883543449, 14:06:58Z). A case-exact assertion would have condemned that working
> trigger as "mangled" and deleted it. Fold case on both sides — but fold **only** case:
> everything else stays exact, because the failure this check exists to catch (MSYS rewriting
> the argument into `C:/Program Files/Git/gemini review`) must still be rejected.

> ⚠️ **A body-only assertion is not enough — it cannot tell "posted correctly this round" from "posted correctly LAST round, silently skipped this round".** If the post silently no-ops (e.g. unquoted `<N>`/`<owner>/<repo>` eats the argv — measured: it creates a file named `--repo` and posts nothing), your own newest comment is still the previous round's `/gemini review`, whose body is perfect, so 6b PASSES on a round where nothing was posted. The loop then waits, sees no bot reply, and converts a posting failure into "bot out of service" → the merge exemption.
>
> So **capture the id before posting and require a strictly newer one after**.
>
> ⚠️ **This is three separate shell invocations, not one script.** The post happens between
> the two halves, and on Windows step 6 tells you to post from the **PowerShell tool** — a
> different process. Shell state (variables, functions) does **not** survive between tool
> calls, so the baseline must be persisted to a **file**, not a variable. Writing it as one
> block with a `# …post here…` comment in the middle is what makes readers re-run the whole
> thing afterwards, which recaptures `PREV` from the *post-post* state ⇒ `PREV == NEW` ⇒
> "the post silently failed" reported on a round that actually succeeded, and the prescribed
> remedy (repost) reproduces it forever.
>
> **Run half 1 exactly once per post, always BEFORE it, never after.** A baseline is only
> meaningful for the one post it precedes, so half 1 mints a **fresh, uniquely-named** file
> each time and prints its path; half 2 must be given *that* path.
>
> **When a repost needs a new half 1, and when it must NOT.** The rule is whether the
> baseline was consumed, not whether you are reposting:
> - **After half 2 PASSED** (it `rm -f`'d the baseline) — e.g. the step 7 repost of the next
>   round: run a **new half 1** before posting again.
> - **After half 2 FAILED with "the post silently failed"**: nothing was posted, so the
>   existing `$BASE` is still the correct baseline. **Do not re-run half 1** — its `rm -f`
>   would delete the very file you still need, and half 2 would then report "baseline file
>   absent or superseded", a diagnosis that is simply false. Fix the invocation, repost,
>   and re-run **half 2 only**, with the same path.
>
> **Preamble — run at the start of BOTH halves:**
> ```bash
> PR="<PR number>"                # MUST be set in every invocation — see the warning above
> REPO="<owner>/<repo>"           # same: nothing survives from an earlier tool call
> case "$PR$REPO" in *'<'*|*'>'*)  # else gh just 404s and the error blames gh, not you
>   echo 'PR/REPO not substituted' >&2; exit 1;; esac
> SLUG=$(printf '%s' "$REPO" | tr / _)
> set -o pipefail                 # belt-and-braces; the check below does NOT depend on it
> ME=$(gh api user --jq .login) || exit 1
> newest_own_id() {               # YOUR OWN comments only — see note below
>   gh api "repos/$REPO/issues/$PR/comments?per_page=100" --paginate \
>     --jq "[.[] | select(((.user // {}).login // \"\")==\"$ME\")] | .[] | .id"
> }
> require_id() {                  # $1 = label; echoes a validated numeric id, else returns 1
>   # `tail` stays OUTSIDE the substitution so $? is gh's own status — that keeps
>   # "gh failed" distinguishable from "gh succeeded, you just have no comments yet".
>   out=$(newest_own_id) || { echo "$1 readback FAILED (gh error) — do NOT treat as posted" >&2; return 1; }
>   id=$(printf '%s' "$out" | tail -n 1)
>   case "$id" in
>     '')       id=0;;            # OBSERVED empty list — the normal state of round 1
>     *[!0-9]*) echo "$1 non-numeric id — readback broken" >&2; return 1;;
>   esac
>   printf '%s\n' "$id"
> }
> ```
>
> **Half 1 — BEFORE posting** (0 is a legitimate baseline: no own comments yet):
> ```bash
> # leftovers from aborted rounds are NEVER reusable. `find -delete`, not `rm -f …*`:
> # the glob has no match in the normal case (round 1, or after half 2 removed the file),
> # and zsh — the default shell on macOS, which GEMINI.md declares the default execution
> # environment — has NOMATCH on, so the line aborts with "no matches found" and status 1
> # (verified: bash exits 0 silently, zsh does not). That renders a working step as a
> # broken one, and under `set -e` it kills half 1 before BASE is ever minted.
> find "${TMPDIR:-/tmp}" -maxdepth 1 -name "prev_id.$SLUG.$PR.*" -delete 2>/dev/null
> BASE=$(mktemp "${TMPDIR:-/tmp}/prev_id.$SLUG.$PR.XXXXXX") || exit 1
> require_id PREV > "$BASE" || { rm -f "$BASE"; exit 1; }
> printf 'baseline=%s  BASE=%s\n' "$(cat "$BASE")" "$BASE"
> ```
>
> …now post the trigger (separate tool call — PowerShell on Windows)…
>
> **Half 2 — AFTER posting**, with the exact `BASE` half 1 just printed:
> ```bash
> BASE="<the BASE path printed by half 1 of THIS round>"
> case "$BASE" in ''|*XXXXXX|*'<'*)
>   echo 'BASE not substituted — run half 1 BEFORE the post and paste its path here' >&2
>   exit 1;; esac
> case "$BASE" in */prev_id."$SLUG"."$PR".*) ;;   # must belong to THIS repo+PR
>   *) echo "BASE belongs to a different repo/PR — run half 1 for THIS PR before the post" >&2
>      exit 1;; esac
> PREV=$(cat "$BASE") || { echo 'baseline file absent or superseded — do NOT repost blind' >&2; exit 1; }
> case "$PREV" in ''|*[!0-9]*)
>   echo 'baseline corrupt — recapture BEFORE reposting, do NOT repost blind' >&2
>   exit 1;; esac
> NEW=$(require_id NEW) || exit 1
> [ "$NEW" -gt "$PREV" ] || {
>   echo "NEW($NEW) not > PREV($PREV). EITHER the post silently failed, OR half 1 was run" >&2
>   echo "AFTER the post. These are indistinguishable by id alone — establish which before" >&2
>   echo "acting; reposting on the second reading loops forever." >&2
>   exit 1; }
> rm -f "$BASE"
> ```
>
> **Why a fresh unique file rather than one fixed name.** A fixed `prev_comment_id.$PR`
> could be (a) left behind by a round that aborted between the post and half 2 — half 2 then
> reads a *previous* round's baseline, and since the id it certifies against is older, a round
> on which **nothing was posted** passes as posted; or (b) silently overwritten by re-running
> half 1 after the post, which recreates the `PREV == NEW` false failure this whole
> restructure exists to remove. `mktemp` plus the explicit hand-off makes the *common*
> mis-sequencing hard, but it does **not** by itself make a wrong baseline impossible —
> half 1's `rm -f` only clears leftovers under the SAME `$SLUG.$PR` key, so a file abandoned
> by an aborted round on a **different PR or repo** survives, and if that path gets pasted its
> id is merely older, so `NEW > PREV` is true and an unposted trigger is certified as posted
> (measured). That is why half 2 also asserts the path carries this repo's and this PR's key.
> Keying on `$SLUG` as well as `$PR` matters because this file is copied into every generated
> repo, where PR numbers restart at 1 — and on Windows Git-Bash `TMPDIR` is often unset,
> so everything lands in one shared `/tmp`.
>
> **A failed readback must exit 1, never satisfy the check by being empty.** The naive
> `[ "$NEW" != "$PREV" ]` passes when `NEW` is the empty string (measured: PREV=999, NEW='' ⇒ true),
> so a second `gh api` that errors — rate limit, transient 5xx, expired token, wrong `$PR` —
> would report "posted successfully" on a round where nothing was posted.
>
> **Why `tail` is outside the substitution.** A command substitution containing a pipeline
> reports the LAST element's status, and `tail -n 1` exits 0 on empty input — so
> `PREV=$(newest_own_id | tail -n 1) || exit 1` never fires on a `gh` failure (measured).
> Same trap the scan script documents as "`echo $?` after `… | sort -u` is useless".
> `set -o pipefail` fixes that form, and is kept here for the rest of the block, but the
> pipeline is split out anyway so the check does not silently depend on one shell option.
>
> **"gh failed" and "no comments yet" are different observations — do not collapse them.**
> On round 1 of a freshly opened PR you have posted nothing, so a perfectly healthy
> `gh` returns an empty list. Treating that as a broken readback aborts step 6 *before the
> trigger is ever posted*, and the stated remedy ("fix the invocation and repost") cannot
> help — no invocation conjures a pre-existing comment, so the loop could never start
> (measured: with `newest_own_id` stubbed to succeed with empty output, the old form exited
> 1 with "readback broken"). That also bricks the empirical bot test that
> `AGENTS.md`「尚未接線上 bot 的專案怎麼辦」routes every new repo through — which is exactly
> the guard-whose-exit-is-unreachable shape this block exists to prevent. Hence `id=0` on an
> observed empty list, and `return 1` only on a real `gh` error.
>
> **But never write `${PREV:-0}`.** That defaults an **unobserved** baseline — the readback
> errored and you substituted a guess — so any real comment id beats it and a failed pre-post
> readback plus a silent no-op still passes. The `id=0` above is the opposite case: `gh`
> exited 0 and the list was genuinely empty. Observed zero is a baseline; unobserved zero is
> a hole. (`NEW` gets the same treatment safely: if the post no-ops, `NEW` is still 0 and
> `[ 0 -gt 0 ]` fails.)
>
> **If the check fires, nothing was posted** — *provided the baseline came from half 1*.
> The argv was eaten (unquoted `<N>` / `<owner>/<repo>`) or MSYS rewrote the body. Fix the
> invocation per the platform rules above, repost, and re-run **half 2 only, with the same
> `$BASE`** (see "when a repost needs a new half 1" above — this is the case where it must
> NOT be re-minted). Do **not** start the wait, and do **not** treat the ensuing silence as
> evidence about the bot.
>
> ⚠️ **One exception, and it is the reason halves 1 and 2 are separate:** if `PREV` was
> (re)captured *after* the post, then `PREV == NEW` is a measurement artefact, not a failed
> post — reposting on that basis loops forever. That is why half 2 reads the baseline from
> `$BASE` and refuses to run when the file is absent, instead of silently recomputing it.

```bash
# Bash — assert on YOUR OWN newest comment, not the newest comment overall.
# The check must ASSERT, not merely display: a snippet that prints the body and leaves the
# comparison to whoever reads the output is fail-open — an unattended run proceeds to the
# wait no matter what came back. `tail` is also kept OUT of the substitution because a
# command substitution containing a pipeline reports the LAST element's status and
# `tail -n 1` exits 0 on empty input (measured), so a gh failure would read as success.
TRIGGER='{{REVIEW_COMMAND}}'   # e.g. /gemini review — the command you ACTUALLY posted
case "$TRIGGER" in *'{{'*|*'}}'*)
  echo 'TRIGGER placeholder not substituted — verification did NOT run' >&2; exit 1;; esac
ME=$(gh api user --jq .login) \
  || { echo 'gh api user FAILED — verification did NOT run; do not treat the trigger as posted' >&2; exit 1; }
# `@json` — NOT `gsub("\n";"\\n")` — is what makes each comment exactly one line. Both
# flatten newlines, but gsub is LOSSY: it renders a real trailing LF and a literal
# backslash-n someone typed into the body as the same two characters, so the assertion
# below could no longer tell them apart. `@json` escapes a real LF as \n and a literal
# backslash as \\, keeping them distinguishable — which is what lets the comparison accept
# the one benign variation (a trailing newline) without also accepting a doctored body.
# The id is projected alongside the body because the DELETE step below needs it, and this
# is the ONLY place it surfaces on the mangled-body path: half 2 computes the same id but
# echoes it only on its FAILURE branch, and a mangled body is a case where half 2 PASSES.
# Without it the reader falls back to "the newest comment", which lines below explicitly ban
# — the bot can reply within 60 s, and that DELETE would destroy the bot's review instead.
out=$(gh api "repos/<owner>/<repo>/issues/<N>/comments?per_page=100" --paginate \
  --jq "[.[] | select(((.user // {}).login // \"\")==\"$ME\")] | .[] | \"\(.id)\t\(.body | @json)\"") \
  || { echo 'comment readback FAILED (gh error) — do NOT treat the trigger as posted' >&2; exit 1; }
last=$(printf '%s' "$out" | tail -n 1)
# Empty is "no comment authored by $ME" — the verification did not run. Never a pass.
[ -n "$last" ] || { echo "no comment authored by $ME on this PR — verification did NOT run" >&2; exit 1; }
id=$(printf '%s' "$last" | cut -f1)
body=$(printf '%s' "$last" | cut -f2-)
# Exact match against the JSON-encoded trigger. Two accepted forms and no others: the bare
# command, and the command plus ONE trailing newline — the latter because the `--body-file`
# fallback above can bake an LF in (that is why it says `printf '%s'`, not `echo`), and a
# check that rejects a perfectly good trigger is as harmful as one that never fires. This
# also subsumes the `Program Files/Git` check: MSYS rewrites the whole argument, so a
# mangled body never matches, and neither does a body that merely CONTAINS the trigger
# inside other text — which Gemini does not act on anyway.
# Case-folded on BOTH sides, and ONLY case is folded. The trigger is not case-sensitive
# (measured: `/Gemini review` drew a full review — see the note above 6b), so a case-exact
# assertion would reject a working trigger; everything else stays exact so an MSYS-rewritten
# `C:/Program Files/Git/gemini review` is still rejected.
fold() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
want=$(fold '"'"$TRIGGER"'"')
want_nl=$(fold '"'"$TRIGGER"'\n"')
case "$(fold "$body")" in
  "$want"|"$want_nl") ;;
  *) printf 'posted body is NOT the trigger (got: %s, id=%s) — fix the invocation and repost\n' "$body" "$id" >&2; exit 1;;
esac
printf 'trigger body verified: %s (id=%s)\n' "$body" "$id"
```

**Select your own comment, and flatten newlines.** Two ways the naive `.[-1]`/`tail -n 1`
form gives a false "mangled" verdict on a correctly-posted trigger: (a) precondition 4
records the bot replying **within 60 seconds**, so by the time you check, the newest comment
may be the bot's; (b) `--jq` emits one line per line-of-body, so `tail -n 1` returns the last
*line* of a multi-line bot reply, not a whole record. Either way the doc's prescribed remedy
("delete it and repost") fires on a trigger that was actually fine — burning the very round
this check exists to save.

```powershell
# PowerShell — the step above told Windows users to post via PowerShell, so verify there too.
# No `tail`, and `\` is not a line continuation: use Select-Object -Last 1.
# Same two rules as the Bash form: select YOUR OWN comment (the bot can reply within 60 s
# and become "newest"), and flatten newlines so each comment is exactly one line.
# Do the filtering in PowerShell — do NOT pass a jq program containing double quotes.
# PowerShell 5.1 splits such a string when handing it to a native exe (measured: gh aborts
# with "accepts 1 arg(s), received 7"). --slurp merges --paginate's pages into an array OF
# PAGES, so flatten one level before filtering.
# ⚠️ The `--slurp` here is correct — do not "fix" it away: `gh api` does support it (measured
# on gh 2.91.0: exits 0 and returns the array of pages). Its only restriction is that it
# cannot be combined with `--jq`/`--template` (measured: the `--slurp` option is not
# supported with "--jq" or "--template") — see the same note at the sweep block below. Since
# the line above rules out `--jq` under PowerShell 5.1 anyway, the two never meet here.
# A review tool has false-flagged this as "gh does not support --slurp".
$TRIGGER = '{{REVIEW_COMMAND}}'   # e.g. /gemini review — the command you ACTUALLY posted
if ($TRIGGER -like '*{{*' -or $TRIGGER -like '*}}*') { throw "TRIGGER placeholder not substituted — verification did NOT run" }
$ME = gh api user --jq .login
$pages = gh api "repos/<owner>/<repo>/issues/<N>/comments?per_page=100" --paginate --slurp | ConvertFrom-Json
$all = $pages | ForEach-Object { $_ }
# `$_.user -and` is required, not belt-and-braces: a comment from a deleted account has
# `"user": null`, and under `Set-StrictMode -Version Latest` the bare `$_.user.login`
# throws PropertyNotFoundException (measured, PS 5.1) — aborting the whole verification.
# This mirrors the `(.user // {}).login // ""` guard the Bash form already has.
$mine = $all | Where-Object { $_.user -and $_.user.login -eq $ME } | Select-Object -Last 1
# Guard LOUDLY, do not swallow: under `Set-StrictMode -Version Latest` a null $mine makes
# `$mine.body` throw PropertyNotFoundException (measured, PS 5.1) — but wrapping the line in
# a plain `if ($mine) { ... }` is worse than the throw, because it turns "no comment found"
# into empty output, which is indistinguishable from "found it, body is empty". This is a
# fail-closed verification: no observation must ever render as a clean one.
if (-not $mine) { throw "no comment authored by $ME on this PR — verification did NOT run; do not treat the trigger as posted" }
# Match the Bash form: compare against the JSON encoding, so a real newline (\n) and a
# literal backslash-n someone typed stay distinguishable. ConvertTo-Json -Compress on a
# string yields exactly the JSON string literal, same as jq's @json.
$body = $mine.body | ConvertTo-Json -Compress
# Same as the Bash form: ASSERT, don't just display. Printing the body and leaving the
# comparison to the reader is fail-open on an unattended run. Exact equality subsumes the
# `Program Files/Git` check — MSYS rewrites the whole argument, so a mangled body never matches.
# `-ne` is already case-insensitive in PowerShell (the case-sensitive form is `-cne`), which
# is what we want here and matches the Bash form's explicit fold — the trigger is not
# case-sensitive (measured; see the note above 6b). Everything else stays exact.
if ($body -ne "`"$TRIGGER`"" -and $body -ne "`"$TRIGGER\n`"") { throw "posted body is NOT the trigger (got: $body, id=$($mine.id)) — fix the invocation and repost" }
"trigger body verified: $body (id=$($mine.id))"   # id printed for the DELETE step below
```

**`--paginate` is required.** Without it `gh api` returns only the first page (30 comments)
and issue comments come back *oldest-first*, so `.[-1]` is the 30th-oldest comment, not the
one you just posted. By the time this loop has run a few rounds the PR usually exceeds 30
comments — exactly when this check matters — so the naive form silently verifies an unrelated
old comment. (`--jq` runs per page, hence the `tail -n 1`.)

If the body is mangled, delete it and repost the correct way, then re-verify:

```bash
# <id> MUST be the id printed by the verification block above (`id=…`). Do NOT re-query for
# "the newest comment" to find it: the bot can reply within 60 s and become newest, so that
# DELETE would destroy the bot's review instead of your mangled trigger.
gh api -X DELETE "repos/<owner>/<repo>/issues/comments/<id>"   # endpoint has no leading slash → safe in Bash
```

Only start the wait once the posted body checks out. Skipping this burns a full review round waiting on a command Gemini never got — and it's easy to misdiagnose the silence as Gemini quota/cooldown.

### 7. Wait, then loop back

Wait for Gemini to respond. Two mechanisms — pick based on the wait length:

- **Short wait (≤ 270s):** a single `Bash` call with `sleep 150` (or whatever the user picked). Stays well inside the ~5 min prompt-cache TTL, so the next turn is cheap. Simple; keeps the session hot.
- **Long wait (≥ 300s):** use `ScheduleWakeup` with the same loop prompt. The session sleeps and re-enters the loop cleanly on wake. Cheaper for multi-minute waits because you're not holding the session open.

Default to **`sleep 150`** — optimistic first check. If there are no new Gemini comments yet, wait another 120 s and re-check. Do this up to ~3 more times (roughly 8 min total wall time in the slow case). Each individual sleep stays under the cache TTL so none of the re-checks cost a cache miss. If Gemini still hasn't responded after that, don't spin forever — tell the user and ask whether to wait more, re-post `/gemini review`, or exit.

After the wait, go back to step 1.

## Termination

Exit the loop when any of these hold:

1. **Gemini signals done** *(then run the final sweep below before declaring victory)*. The latest Gemini comment/review reads like "I have no feedback", "no further suggestions", "LGTM", or an approval verdict. Be a bit generous on wording — Gemini phrases this a few ways.
2. **User says stop.**
3. **Blocker that needs human judgment:** repeated test failure, unresolvable merge conflict, an unclear or controversial comment, or a design decision. Pause and report clearly.
4. **Review bot out of service:** the wait-and-retry budget is exhausted with zero bot response AND there's positive evidence the bot is gone — **an observed fact**, i.e. bot seat removed, app uninstalled, or the org revoked the installation. Three things are explicitly NOT such evidence:
   - The announced 2026-07-17 free-tier sunset — it did not take effect (see precondition 4).
   - A *quota/rate-limit* reply — the service is alive; wait for the window to reset and retry.
   - **"This repo was probably never onboarded"** — that is a *presumption*, not an observation. It may only be established by the full-repo-history bot scan in `AGENTS.md`「尚未接線上 bot 的專案怎麼辦」— **all** of: the three comment endpoints clean, every guard in that script passing, AND every account **and check/status name** the script's `census` pass lists classified. Note the asymmetry: only the comment endpoints have an *emptiness* criterion, because they are filtered by the anchored `BOTS` regex. The check/status channel is deliberately unfiltered, so on any repo with ordinary CI it is never empty — its output feeds the census list and is satisfied by *classification*, not by being clean. The parenthetical used to say just "three endpoints"; that is not the criterion, and treating it as one is the exact fail-open the census and the check/status channel were added to close. If that scan cannot be completed, escalate to the user rather than terminating here — a wrong "never onboarded" call is what the downstream docs convert into a merge exemption.

   Report it and suggest the Claude-native `gemini-grade-review` skill instead — don't keep re-posting into the void.

5. **Bounded exit when the bot keeps replying but never actually reviews.** This is a
   SEPARATE item, not a sub-point of the quota bullet above: two of the reply types below
   have no quota window to wait for, so hanging their cap on "wait for the window to reset"
   left them with no termination story at all. **Trigger:** reposting has spanned **≥48 h**
   (or ≥2 quota windows where a window even applies) and every attempt still failed to
   produce **actual review content** — a formal review in `pulls/<N>/reviews`, *including
   one that says it has no feedback*. These all count as "failed to produce it":
   - a quota warning, or a rate-limit reply;
   - a summary-only comment;
   - a CI check that is green but whose **status text** says *review limited*. ⚠️ Steps 1
     and 7 only read `pulls/<N>/comments`, `issues/<N>/comments` and `pulls/<N>/reviews`,
     so this one is **not observed by the normal loop** — check it explicitly with
     `gh pr checks "<N>" --repo "<owner>/<repo>"` (read the status text, not just the
     colour) before concluding. `--repo` is not optional here, for the same measured reason
     AGENTS.md gives for `gh pr view`: without it gh resolves the PR against cwd's remote —
     measured, asking for `28` from a clone whose origin is another repo returned that
     repo's #28 and `no checks reported`, which reads exactly like "this channel is clean".
     Plain `gh pr checks` is otherwise enough — no `--json description` needed: measured on
     gh 2.91.0, the default table has a `DESCRIPTION` column on a TTY and emits the same
     text as the last tab-separated field when piped (`CodeRabbit\tpass\t0\t\tReview completed`).

   ⚠️ **Measure that ≥48 h against the PR record, not against session memory.** Step 1 says
   the handled-comment set lives in memory because "the loop is one continuous session" — but
   this clock has to survive what that assumption does not: a `ScheduleWakeup` sleep, a
   context compaction, or the user re-running the skill tomorrow. Re-derive the elapsed time
   each round from a durable timestamp — the `created_at` of your **earliest** trigger comment
   that has not yet been answered with actual review content:

   ```bash
   # Same rule as 6b and the final sweep: its own tool call, so NOTHING survives from an
   # earlier one — every value is re-established here.
   set -o pipefail
   PR="<PR number>"
   REPO="<owner>/<repo>"      # explicit, so the block does not silently depend on cwd
   case "$PR$REPO" in *'<'*|*'>'*)
     echo "PR/REPO 未替換 — 端點會變成 issues//comments，錯誤會嫁禍給 gh" >&2; exit 1;; esac
   TRIGGER='{{REVIEW_COMMAND}}'   # e.g. /gemini review
   # Unsubstituted `{{...}}` matches no real comment body, and "no match" is byte-identical
   # to "the clock has not started" — i.e. it silently reads as *budget unspent*, which is
   # the exact direction this exit must never fail in. Same guard as the sweep's BOT.
   case "$TRIGGER" in *'{{'*|*'}}'*)
     echo "REVIEW_COMMAND 未替換 — 量不到時間就不得判定 48h 未滿" >&2; exit 1;; esac
   BOT='{{BOT_USER_LOGIN}}'       # e.g. gemini-code-assist[bot]
   case "$BOT" in *'{{'*|*'}}'*)
     echo "BOT_USER_LOGIN 未替換 — 分界線算不出來，不得判定 48h" >&2; exit 1;; esac
   ME=$(gh api user --jq .login) \
     || { echo "gh api user FAILED — 無法確認留言作者，時間戳不成立" >&2; exit 1; }
   # The clock starts at the **currently unanswered** stretch, not at the first time you ever
   # posted the trigger. Every round re-posts the same string, so without this boundary the
   # query would always return round 1 — and a long-lived PR that got real reviews for days
   # would read as "stuck ≥48 h" the very first time a quota warning appears, firing this
   # exit within minutes of the actual stall. Boundary = the bot's most recent formal review
   # (per this item, one that says it has no feedback still counts as review content).
   # No review yet ⇒ empty string, and every timestamp sorts above "" — i.e. the whole
   # history counts, which is exactly right when the bot has never answered.
   # ⚠️ `?per_page=100` and the aggregation OUTSIDE the jq are both load-bearing here, for the
   # reason this file already states at 6b and the sweep: **`--jq` runs once per page**. Write
   # `[...] | sort | last` inside it and each page emits its own answer — measured on this repo
   # with `per_page=2`, `last_review` came back as a three-line blob, the comparison below then
   # ran against the FIRST page's maximum, and the boundary silently slid back to a review that
   # two later reviews had already superseded. Both failure directions are live: a boundary that
   # slides back reads as "already ≥48 h" and fires this exit while the bot is still reviewing;
   # taking `tail -n 1` of a multi-line `first` reads as "not yet 48 h" and the bounded exit
   # never fires at all — the very deadlock this item exists to end. Multi-page is the norm
   # here, not an edge case: 48 h of reposting plus quota replies clears 30 comments easily.
   # GitHub logins are case-insensitive, so compare both sides folded — the same rule
   # AGENTS.md states for its scan script ("both places must use -i together"). `==` on a
   # folded value and NOT test(): `[bot]` is a character class in a jq regex, so a
   # test()-based filter silently matches nothing against a login ending in `[bot]`.
   BOT_LC=$(printf '%s' "$BOT" | tr 'A-Z' 'a-z')
   revs=$(gh api "repos/$REPO/pulls/$PR/reviews?per_page=100" --paginate \
     --jq "[.[] | select(((((.user // {}).login // \"\")) | ascii_downcase)==\"$BOT_LC\") | .submitted_at // empty] | .[]") \
     || { echo "讀 reviews 失敗 — 分界線不成立，不得判定 48h 未滿" >&2; exit 1; }
   last_review=$(printf '%s\n' "$revs" | sed '/^$/d' | sort | tail -n 1)
   # 正控，與 final sweep 同型：能走到本出口，代表你已重貼過好幾輪而 bot 一直有回應，它
   # 必然在本 PR 留過紀錄。三個端點全空只可能是 $BOT 拼錯或 bot 改名——而那條路的下場
   # 特別惡劣：last_review 變空字串後，下面的 `.created_at > ""` 會放行整段歷史，起算點
   # 滑回第一則觸發留言、ELAPSED 虛增，於是在 bot 仍正常審查的健康迴圈上誤觸終止。
   # （實測：只把 login 換成大小寫不同的合法寫法，ELAPSED 就從「沒有觸發留言」變成 46h，
   #  而 bot 其實 9.8 小時前才剛交過正式審查。）
   if [ -z "$last_review" ]; then
     seen=0
     for ep in "pulls/$PR/reviews" "pulls/$PR/comments" "issues/$PR/comments"; do
       out=$(gh api "repos/$REPO/$ep?per_page=100" --paginate \
         --jq "[.[] | select(((((.user // {}).login // \"\")) | ascii_downcase)==\"$BOT_LC\")] | .[] | .id") \
         || { echo "讀 $ep 失敗 — 正控不成立，不得判定 48h" >&2; exit 1; }
       if [ -n "$out" ]; then seen=$((seen + 1)); fi
     done
     # 訊息不可斷言成因：這三個都是**留言**管道，而本出口第三種型態（check 綠燈但 status
     # text 寫 review limited）正是靠只發 check/status、完全不留言的 bot 觸發的——那種 bot
     # 在這裡必然零命中，卻不是設定錯。斷言「login 拼錯」會讓操作者去查一個沒問題的欄位，
     # 而本出口該產出的量測結論就此拿不到。兩種成因都導向同一個處置（人工確認後裁決），
     # 但要講清楚該去看哪裡。（check/status 那一欄不能用 login 比對補救，見 AGENTS.md。）
     [ "$seen" -gt 0 ] || {
       echo "$BOT 在三個留言端點零命中 — 可能是 BOT_USER_LOGIN 拼錯／改名，也可能該 bot 只發 check/status 不留言（本出口第三種型態）。先用 gh pr checks \"$PR\" --repo \"$REPO\" 讀 status text 確認，不得逕自判定 48h" >&2
       exit 1; }
   fi
   # `// {}` and `// ""` are not decoration: a comment whose `body` is null makes a bare
   # `.body | startswith(...)` hard-error out, and that non-zero exit reads exactly like
   # "no trigger comment found" — the failure this clock exists to rule out.
   # Anchored prefix, not `contains`: this file's own escalation rule has you write the
   # repost history into the PR, so a comment like "因額度重貼了 /gemini review" is routine
   # here, not an edge case — and `contains` counts it. Since `first` takes the MINIMUM,
   # one such false hit only ever drags the start time backwards, inflating ELAPSED and
   # firing this exit on a loop that is still healthy. The trailing non-alphanumeric test
   # keeps `/gemini reviewer …` out while still allowing the trigger to carry a note after
   # it — the same predicate AGENTS.md already settled on for its post-verification guard
   # (`"$TRIGGER"|"$TRIGGER"[!A-Za-z0-9]*`); keep the two in step.
   cands=$(gh api "repos/$REPO/issues/$PR/comments?per_page=100" --paginate \
     --jq "[.[] | select(((.user // {}).login // \"\")==\"$ME\") | select(((.body // \"\") | startswith(\"$TRIGGER\")) and (((.body // \"\") | ltrimstr(\"$TRIGGER\")) | (. == \"\" or test(\"^[^A-Za-z0-9]\")))) | select(.created_at > \"$last_review\") | .created_at] | .[]") \
     || { echo "讀留言失敗 — 時間戳不成立，不得判定 48h 未滿" >&2; exit 1; }
   first=$(printf '%s\n' "$cands" | sed '/^$/d' | sort | head -n 1)
   # An observed empty set and a failed measurement are different observations — the same
   # distinction 6b's preamble spells out. The commonest reason `cands` is empty is the
   # *healthiest* state there is: the bot has answered every trigger you posted, so the
   # latest one sorts below `last_review`. Since this block is re-derived every round,
   # collapsing that into "escalate to the user" fires a spurious escalation on every
   # healthy round — the "kills a healthy loop early" direction this block warns about.
   # Both branches still exit 1 (the exit does not hold), only the next step differs.
   [ -n "$first" ] || {
     if [ -n "$last_review" ]; then
       echo "最後一則觸發留言已於 $last_review 獲得正式審查 — 本出口不成立，繼續迴圈" >&2
     else
       echo "查無你貼出的觸發留言，且 bot 未留下任何審查 — 時間戳未建立，交由使用者裁決" >&2
     fi
     exit 1; }
   # ASSERT, don't display — same rule 6b states twice. A block that prints the start time and
   # leaves "has 48 h passed?" to whoever reads the output is fail-open on an unattended run,
   # and this exit's outcome is *terminate the loop and escalate*. Getting it wrong in one
   # direction kills a healthy loop early; in the other it never fires and the PR hangs.
   to_epoch() {   # BSD (macOS) first, GNU second — the repo runs on both
     date -u -j -f '%Y-%m-%dT%H:%M:%SZ' "$1" +%s 2>/dev/null || date -u -d "$1" +%s
   }
   START=$(to_epoch "$first") \
     || { echo "時間戳無法換算 — 不得判定 48h 已滿" >&2; exit 1; }
   ELAPSED_H=$(( ($(date -u +%s) - START) / 3600 ))
   [ "$ELAPSED_H" -ge 48 ] || {
     echo "距最早未獲審查的觸發留言僅 ${ELAPSED_H}h（未滿 48h）— 不得走本出口，繼續迴圈" >&2
     exit 1; }
   printf 'clock starts at %s; elapsed=%sh (>=48h) — 本出口成立，寫進 PR 並交由使用者裁決\n' \
     "$first" "$ELAPSED_H"
   ```

   If you cannot establish that timestamp, say so and escalate rather than assuming the
   budget is unspent — an unmeasurable clock is what silently converts this bounded exit back
   into the open-ended wait it exists to end.

   Write the retry count, timestamps and each reply into the PR and **escalate to the user
   for adjudication**. Do not let it block indefinitely, and do not convert it into a merge
   exemption on your own. (This file is the authority both AGENTS.md and GEMINI.md defer to
   for termination, so the bounded exit they each carry must exist here too — otherwise a
   reader following only this file gets the deadlock those amendments removed.)

### Final sweep — run before reporting "done"

When Gemini signals done after a long review chain, comments from earlier rounds can have been silently dropped — addressed only partially, or stopped being repeated by Gemini for unrelated reasons (a later commit changed the surrounding code, Gemini's prioritisation shifted, etc.). Trusting "no feedback" alone misses these. Before reporting termination, do one explicit pass over every Gemini comment ever posted on this PR:


```bash
# Gemini-review: --paginate already fetches EVERY page, so all comments are listed.
# We deliberately do NOT add --slurp here: `gh api --slurp` errors when combined
# with --jq, and a standalone `jq` binary isn't guaranteed to be installed. The
# only cost is that --jq runs per page, so sort_by orders within each page rather
# than globally — cosmetic for an enumeration sweep where the point is that no
# comment is missed, not the exact ordering.
# Gemini-review: `(.user // {}).login` is the null-safe form and is FINAL. The
# null-user guard has oscillated across review rounds (`.user and .user.login`
# <-> `.user?.login`), each flagged as unsafe in turn. All three forms actually
# work (jq returns null for `null.login`, it does not throw), but `// {}`
# substitutes an empty object so there is nothing to debate. Not changing it again.
# All THREE channels are required. Gemini's formal review is a *submitted review* and
# exists ONLY in pulls/<N>/reviews — measured: on a real PR its review was absent from
# issues/comments entirely. Sweeping pulls/<N>/comments alone misses every summary-level
# finding, and the resulting empty list is indistinguishable from "nothing was dropped".
# That cuts both ways. A failing `gh` is guarded below — but so is a filter that matches
# nothing: a wrong or unreplaced login yields three empty lists with every call exiting 0,
# which is byte-identical to a clean sweep. Hence the placeholder guard and the positive
# control. AGENTS.md's full-history scan carries the same positive control for the same
# reason; guarding only one of the two zero-hit paths leaves the other untrustworthy.
set -o pipefail
# Same rule as 6b: this is its own tool call, so NOTHING survives from an earlier one.
PR="<PR number>"
REPO="<owner>/<repo>"      # explicit, so the block does not silently depend on cwd
case "$PR$REPO" in *'<'*|*'>'*)
  echo "PR/REPO 未替換 — 端點會變成 pulls//comments，錯誤會嫁禍給 gh" >&2; exit 1;; esac
BOT='{{BOT_USER_LOGIN}}'   # e.g. gemini-code-assist[bot]. GEMINI.md's copy hardcodes it.
case "$BOT" in *'{{'*|*'}}'*)
  echo "BOT_USER_LOGIN 未替換 — 零命中不具意義，不得宣告收斂" >&2; exit 1;; esac
# `==` on a folded value and NOT test(): `[bot]` is a character class in a jq regex, so a
# test()-based filter silently matches nothing against a login ending in `[bot]` (measured).
# 折疊大小寫的理由與 48h 區塊那處相同，兩處必須一起改：GitHub login 不分大小寫，這裡若
# 留著逐字比對，`{{BOT_USER_LOGIN}}` 一被填成大小寫不同的合法寫法，三個端點就必然全空、
# 下面的正控每次都 exit 1，「bot 宣告 done → 跑 sweep → 宣告收斂」這條唯一的正常終止路徑
# 永遠走不完（而 48h 區塊已折疊，不會再對同一個設定錯誤出聲）。
BOT_LC=$(printf '%s' "$BOT" | tr 'A-Z' 'a-z')
hits=0
for ep in "pulls/$PR/comments" "pulls/$PR/reviews" "issues/$PR/comments"; do
  out=$(gh api "repos/$REPO/$ep?per_page=100" --paginate \
    --jq "[.[] | select(((((.user // {}).login // \"\")) | ascii_downcase)==\"$BOT_LC\")] | sort_by(.submitted_at // .created_at) | .[] | \"\(.id)\t\(.submitted_at // .created_at)\t\(.path // \"(review body)\"):\(.line // \"?\")\"") \
    || { echo "SWEEP FAILED: $ep — do NOT declare termination" >&2; exit 1; }
  if [ -n "$out" ]; then printf '%s\n' "$out"; hits=$((hits + 1)); fi
done
# 正控：能跑到這一步，代表 bot 剛宣告 done，它必然在本 PR 留過紀錄。三個端點全空只可能是
# 篩選條件錯了（login 主體拼錯、漏掉 [bot] 後綴；大小寫已於上面折疊），不是「沒有東西被漏掉」。
[ "$hits" -gt 0 ] || {
  echo "SWEEP 三個端點都篩不到 $BOT，但 bot 剛回報 done — 篩選條件有誤，不得宣告收斂" >&2
  exit 1
}
```



For each comment, classify against the **current** code state:

- **Addressed** — the suggested change is reflected in the merged-into-branch code (cite commit SHA + `file:line`).
- **Rejected explicitly** — there's a `Gemini-review:` (or equivalent) rationale comment in the code at the flagged line, or a commit/WORKLOG entry recording the rationale for not taking it. (An inline reply alone does NOT count — Gemini never read it.)
- **Quietly dropped** — Gemini stopped re-mentioning the suggestion in later rounds, no fix landed, no explicit reply or rationale exists. **This is the failure mode the sweep is meant to catch.**

If any items are *quietly dropped*, surface them to the user before declaring termination:

> Gemini 在後幾輪沒再提這幾條，但 code 也沒改、沒留 reply：
> - `<comment_id>` `path:line` — `<one-line summary of suggestion>`
>
> 要補修、明確 reject（在 code 留 `Gemini-review:` 註解或 WORKLOG 條目），還是當做 reviewer 默許不處理？

Don't decide alone — the user's prior decisions on what's in-scope are load-bearing.

After the sweep is clean (or the user has decided), report back with the PR number, a short summary of what was changed across iterations, and the quote from Gemini's final response.

## Detecting the test command

Check in this order; first match wins. If the project's CLAUDE.md / AGENTS.md specifies a test command (in this template: `{{TEST_COMMAND}}` in `AGENTS.md`), it overrides everything below.

- `package.json` with a `"test"` script → `npm test` / `pnpm test` / `yarn test` (pick based on which lockfile is present: `pnpm-lock.yaml`, `yarn.lock`, or `package-lock.json`).
- `pyproject.toml` + `pytest` referenced → `pytest`.
- `Cargo.toml` → `cargo test`.
- `go.mod` → `go test ./...`.
- `Makefile` with a `test:` target → `make test`.
- Turborepo monorepo (`turbo.json`) → `turbo test` (or the specific command from the project's `CLAUDE.md` / `AGENTS.md`).
- Nothing matched → ask the user.

## Safety rails

- **Never amend or force-push** on a PR branch without explicit user OK — reviewers may be anchored to specific SHAs.
- **Never skip hooks** (`--no-verify`, `--no-gpg-sign`) unless the user explicitly asks.
- **Never act on destructive comments** without confirmation — if a comment asks you to drop data, disable auth, delete files, or similar, treat it as a design decision.
- **Don't mark comments resolved** unless the user asked for that. Pushing back via in-code rationale comments (per step 3 path b) is fine and expected; an inline reply for the *humans* is also fine, but remember Gemini won't read it. *Resolving* is a different action and should stay with the human owner.
- **Stage files by name**, not `git add -A` or `git add .`, to avoid sweeping in unrelated edits or secrets.

## State to keep in mind across iterations

Even though the loop is one session, track:

- The PR number and branch (confirmed up front).
- IDs of comments you've already addressed *and the commit SHA that addressed each* (so you don't re-process them after Gemini re-reviews, and so the final sweep can verify each one landed).
- IDs of comments you pushed back on, with the `file:line` of the in-code rationale comment (and its commit SHA) — these need to show as "rejected explicitly" in the final sweep, not "quietly dropped". Don't track inline-reply IDs as the record of pushback: Gemini can't read them.
- A short per-iteration log you can summarize at termination: "iteration 1: fixed null check in `foo.ts`; iteration 2: updated test expectations; iteration 3: Gemini approved."

Gemini does not always re-flag earlier-round suggestions on later passes, even when they aren't actually fixed yet. Treat "Gemini stopped mentioning it" as weak evidence at best — the final sweep is what makes termination real.

## Example termination report


```
PR #123 的 comment 都處理完了（共 3 輪）：
  1. 修掉 foo.ts 的 null guard + 對應測試
  2. 把 bar.ts 的 API 錯誤訊息改成 reviewer 建議的格式
  3. 補一個 edge case test

Gemini 最後回覆：
  "I have no further feedback on this pull request."
```
