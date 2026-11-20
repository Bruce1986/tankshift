---
name: gemini-grade-review
description: Perform a Gemini-Code-Assist-grade, high-signal code review with independent Codex reviewer and verifier agents, deterministic diff grounding, severity calibration, and optional fix-to-convergence. Use for requests such as "Gemini-grade review", "detailed review", "deep review", "thorough review", "仔細審查", "詳細 review", "Gemini 級審查", "review 到收斂", "審到沒問題為止", or "fix to convergence". Report only by default; modify code only when the user asks to fix, and never post, commit, or push without explicit authorization.
---

# Gemini-Grade Review

Produce a high-recall review, independently verify every candidate, and ground mechanical facts with the bundled scripts. Keep findings high-signal: emit only `high` or `medium`, never invent low-value nits.

## Choose the mode

- Use **review mode** by default: inspect once and report without changing files.
- Use **fix-loop mode** only when the user asks to fix or converge: review, fix confirmed findings one at a time, validate, and repeat until two consecutive clean passes or a safety stop.
- Use a remote PR diff when the user names a PR. Otherwise review the current branch and working tree against its default/base branch.
- Post inline comments only after an explicit request to post and only for a PR.

## Prepare the target

1. Resolve the repository root, owner/repo, default or PR base branch, and optional PR number. For a named PR, also resolve and record its head repository, head ref, exact head SHA, exact base SHA, and the initial merge-base SHA; freeze those SHAs for the run.
2. Read `AGENTS.md`, the repository's required instructions, and `.gemini/styleguide.md` when present.
3. Resolve this selected skill's directory and use its `scripts/` directory as `TOOLS`. Verify:

   ```bash
   python3 "$TOOLS/review_tools.py" --help
   ```

4. Create a unique temporary directory with `mktemp -d`.
5. Build the actual diff under review:
   - Remote report-only PR: run `review_tools.py prep --repo <owner/repo> --pr <N> --diff-out <temp>/diff`. Inspect files from the recorded head SHA (for example with a temporary detached worktree or exact-SHA blob reads), never from an unrelated current checkout.
   - PR fix-loop: require a writable checkout or dedicated worktree whose `HEAD` equals the recorded PR head SHA. Revalidate that invariant before every round and stop if the PR head moves. Rebuild each working-tree diff against the frozen initial merge-base SHA, not a mutable base ref. Never apply PR fixes from an unrelated branch or a read-only fork checkout.
   - Clean committed branch: `prep --base <base> --diff-out <temp>/diff` is acceptable.
   - Dirty non-PR branch or each non-PR fix-loop round: compute `merge_base=$(git merge-base <base> HEAD)` and run `git diff "$merge_base" -- <scoped paths> > <temp>/diff`. For a named-PR fix-loop, set `merge_base` to the frozen initial merge-base SHA recorded in step 1 instead of recomputing it from a moving base ref. This form includes committed and tracked working-tree changes. Do not use `base..HEAD` or `base...HEAD` after fixes because both omit uncommitted changes.
   - If the loop creates an untracked file, use `git add -N <file>` so the diff includes it. Do not alter pre-existing untracked files merely to review them; inspect and report their exclusion separately.
6. For a manually built diff, run dependency grounding against a separate committed diff when useful, but never overwrite the working-tree diff reviewers and anchoring consume. State the helper's scope: only added exact pins in `requirements*.txt` and `package.json`; it does not prove every dependency change safe.
7. If prep reports truncation, disclose that later files were not reviewed.

## Run one review pass

### 1. Recall with a fresh reviewer

Use fresh review subagents; the orchestrator must not be the sole reviewer. This skill explicitly authorizes the required reviewer and verifier subagents.

- Small diff (about 8 files or fewer and 600 changed lines or fewer): give one fresh reviewer the whole diff.
- Large diff: fan out by file or small related groups. The orchestrator owns the fan-out; scoped reviewers must not re-delegate.
- Pass only task-local context and the real diff/current files. Carry deliberate-design decisions from earlier rounds so settled points are not reopened.
- In fix loops, use ordinary review agents for rounds 1-3, then a fresh higher-reasoning tracer on every fourth round when supported. Repeat this cadence through round 20.
- Require each reviewer to return only a JSON array in its final answer, prefixed by `GGR_FINAL_V1`. Never treat commentary, an intermediate `[]`, or an answer without the sentinel as a clean result.

Each finding must be:

```json
{"path":"src/file.py","line":42,"severity":"medium","issue":"Full causal chain and impact.","suggestion":"Directly applicable replacement, or empty string."}
```

Apply this rubric:

- Correctness: broken behavior, boundary cases, off-by-one, empty input, division by zero, path variations, and compatibility regressions.
- Exceptions: enumerate concrete failures at every I/O, parse, subprocess, and external-call boundary; reject bare `except` and incomplete handling.
- Values and types: distinguish `None` from valid falsy values; validate external JSON/API containers and nested fields before use; make public functions reject unexpected input safely.
- Security: parameterize SQL, avoid `shell=True`, prevent injection and path traversal, and reject embedded secrets.
- Concurrency and time: inspect shared state, async re-entry, locks, timers, races, and require timezone-aware UTC timestamps.
- Resources and portability: use context managers; specify subprocess `encoding="utf-8"` and `errors="replace"`; reconfigure output only at application entry points; pass large bodies via stdin.
- Consistency: compare sibling modules and the repository styleguide, including serialization, deduplication, and error-tolerance conventions.
- Tests: require normal, boundary, and failure coverage; reject tautological tests, expected values copied from implementation output, or mocks that bypass the behavior. Treat missing red-green evidence for new or materially changed tests as `medium` unless the PR, commit, or `WORKLOG.md` documents a concrete exemption.
- Documentation: verify heading hierarchy, rendering, symbol consistency, examples, links, and a table of contents for long documents.
- Dependencies: accept deterministic registry findings from prep as already verified.

Calibrate severity:

- `high`: crash, exploitable security flaw, data loss/corruption, or a clear existing-behavior break.
- `medium`: real robustness, correctness-edge, performance, exception-handling, test-integrity, or maintainability defect.
- Drop pure style preferences and anything that would be `low`.

### 2. Precision with a fresh verifier

Give all recall candidates to a different fresh verifier agent. Require `GGR_FINAL_V1` followed only by a JSON array containing each candidate plus `keep: true|false` and a concise reason.

The verifier must:

- Read the supporting diff and current code.
- Drop unsupported, duplicate, out-of-scope, speculative, and style-only candidates.
- Regrade borderline but real issues to `medium`; never retain a `low` issue.
- Verify external-system behavior or syntax empirically with one minimal safe call when the finding depends on it. If that requires network access or material external state, follow the normal approval rules.
- Verify current model IDs with official provider documentation rather than training-memory recency.

Merge only `keep:true` findings with deterministic dependency findings. Deduplicate by path, line, and issue gist. Assign each confirmed defect a stable ID derived from normalized path, defect category, affected symbol or line region, and causal gist; reconcile reworded candidates with existing IDs semantically.

### 3. Anchor findings

Write confirmed findings as JSON and run:

```bash
python3 "$TOOLS/review_tools.py" anchor --diff <temp>/diff --findings <temp>/findings.json
```

Use `anchored` items for line-specific reporting or inline comments. Put `unanchored` items in prose with the returned reason; never fabricate a commentable line.

## Deliver review mode

Lead with the outcome. Group findings by file and report `path:line`, severity, causal issue, and concrete fix. Include unanchored and truncation notes.

- Clean: say `I have no feedback to provide.` Add only a short evidence-based qualifier if useful.
- Near-clean with no high findings: lead with specific praise, then list the few real suggestions.
- Any high finding: present findings directly without forced praise.

To post after explicit authorization, use `review_tools.py post` with the repository, PR, diff, findings, summary file, and the `--commit-id` returned by prep. The helper must confirm the PR head is unchanged before posting. Branch mode never posts.

## Run fix-loop mode

### Establish the baseline

Before round 1, discover and run the project's required lint, typecheck, and test commands. Record the exact failing test identifiers, not just the count. If no validation command exists, proceed but label all changes unvalidated.

Track `round`, `clean_streak`, stable finding IDs, attempted IDs, and confirmed deliberate-design decisions.

### Iterate

1. Run a full recall, precision, and anchor pass on the current working-tree diff.
2. If there are no confirmed findings, increment `clean_streak`. On the first clean pass, run an additional fresh independent cross-check before allowing it to count.
3. Otherwise reset `clean_streak` and apply confirmed fixes one at a time.
4. After each fix, rerun relevant validation and compare failing test IDs with baseline:
   - Re-run any new failure once to guard against flakes.
   - If a test not in baseline still fails, revert only that fix and keep the finding unresolved.
   - A newly added test that exposes a defect is a finding, not a pre-existing regression.
   - If a fix changes test configuration or command selection, establish a new baseline.
5. Rebuild the diff from the merge-base against the working tree, then re-review current on-disk files.
6. Mark a stable finding ID that survives its fix attempt as no-progress; do not retry it blindly. A fresh reviewer's rewording of the same causal defect must retain the prior ID.

Declare convergence only after two consecutive authoritative clean passes.

### Stop safely

- Stop after 20 rounds and ask whether to continue; the final round should be a higher-reasoning tracer when supported.
- Stop if the attempted-but-still-open set fails to strictly shrink for two consecutive rounds.
- Stop retrying no-progress findings and report the needed human decision.
- Never commit, push, post, merge, or trigger an external review service mid-loop.
- At convergence or a safety stop, summarize fixes, validation, remaining findings, truncation, and unvalidated areas. Offer outward actions; do not perform them without explicit authorization.

## Deterministic helper scope

Use bundled `scripts/review_tools.py` and `scripts/review_lib.py` for diff parsing, boundary-safe truncation, PyPI/npm exact-version grounding, GitHub commentable-line anchoring, and opt-in posting. Run `python3 "$TOOLS/test_review_lib.py"` when modifying these helpers (`$TOOLS` is the skill's `scripts/` directory resolved in step 3; the bare relative path `scripts/…` would not resolve from the repository root).
