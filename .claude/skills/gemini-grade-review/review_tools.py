#!/usr/bin/env python3
"""review_tools.py — Claude(+subagents) 詳審 skill 的「確定性零件」CLI。

把 v3 的非 LLM 工作（取/截斷/解析 diff、供應鏈 grounding、行錨定、貼 inline review）
包成可從 shell 呼叫的子命令，讓 skill 專心做 LLM 推理、把機械部分交給確定性程式。
共用 review_lib.py（與 review_pr_v3.py 同源，DRY）。

子命令:
  prep   取 diff（PR 或本機分支）→ 檔界截斷 → 寫出 diff 檔 + 印出 {truncated, files, deps}
  anchor 把 findings 對齊 diff 可評論行 → 印出 {anchored, unanchored}（dry-run 預覽用）
  post   把 findings 錨定後貼成 GitHub inline review（需 --pr）

範例:
  python3 review_tools.py prep --repo Bruce1986/us_stock --pr 308 --diff-out /tmp/d.diff
  python3 review_tools.py prep --base origin/main --diff-out /tmp/d.diff           # 本機分支
  python3 review_tools.py anchor --diff /tmp/d.diff --findings /tmp/f.json
  python3 review_tools.py post --repo Bruce1986/us_stock --pr 308 \
          --diff /tmp/d.diff --findings /tmp/f.json --summary-file /tmp/s.txt
"""
import argparse
import json
import sys

from review_lib import (
    TRUNCATION_MARKER, dedup, dependency_findings, normalize, parse_diff,
    post_review, sh, split_anchored, truncate_diff,
)


def _fetch_diff(repo, pr, base):
    if pr and base:
        sys.exit("prep：--pr 與 --base 互斥，擇一")
    if pr:
        if not repo:
            sys.exit("prep：PR 模式需要 --repo <owner/repo>")
        return sh(["gh", "pr", "diff", str(pr), "--repo", repo])
    if base:
        return sh(["git", "diff", f"{base}...HEAD"])
    sys.exit("prep 需要 --pr <num>（PR 模式）或 --base <ref>（本機分支模式）")


def _load_findings(path):
    """讀 findings JSON（list[dict] 或 {findings:[...]}）→ 經 normalize/dedup 的 tuple 清單。"""
    # findings 是本工具自己寫出的檔，應為合法 JSON：用嚴格 json.load，壞檔時明確報錯退出，
    # 不用 loads_lenient（那是給原始模型 API 輸出去圍欄用的）——以免把解析失敗默默吞成空 review（靜默失敗）。
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        sys.exit(f"findings 檔不存在: {path}")
    except (json.JSONDecodeError, OSError) as e:
        sys.exit(f"findings 檔讀取/解析失敗: {path}: {e}")
    if isinstance(data, dict):
        items = data.get("findings")
        if items is None:
            items = data.get("issues")  # 相容別名
        if items is None:  # 缺鍵不默默回空（靜默失敗）→ 明確報錯
            sys.exit('findings 檔格式錯誤：dict 中缺少 "findings"（或 "issues"）鍵')
    else:
        items = data
    if not isinstance(items, list):
        sys.exit('findings 應為 list 或 {"findings": [...]}')
    items = [f for f in items if isinstance(f, dict)]
    return dedup([t for t in (normalize(f) for f in items) if t])


def cmd_prep(args):
    diff, truncated = truncate_diff(_fetch_diff(args.repo, args.pr, args.base), args.max_diff_tokens)
    diffmap = parse_diff(diff)
    with open(args.diff_out, "w", encoding="utf-8") as fh:
        fh.write(diff)
    deps = [] if args.no_deps else dependency_findings(diffmap)
    out = {
        "diff_path": args.diff_out,
        "truncated": truncated,
        "file_count": len(diffmap),
        "commentable_total": sum(len(i["commentable"]) for i in diffmap.values()),
        "files": [{"path": p, "commentable": len(i["commentable"]), "hunks": i["hunks"]}
                  for p, i in diffmap.items()],
        "deps": [{"path": p, "line": ln, "severity": s, "issue": i, "suggestion": sg}
                 for (p, ln, s, i, sg) in deps],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_anchor(args):
    with open(args.diff, encoding="utf-8") as fh:
        diffmap = parse_diff(fh.read())
    anchored, unanchored = split_anchored(_load_findings(args.findings), diffmap)
    out = {
        "anchored": [{"path": p, "line": ln, "severity": s, "issue": i, "suggestion": sg}
                     for (p, ln, s, i, sg) in anchored],
        "unanchored": [{"path": p, "line": ln, "severity": s, "issue": i, "note": n}
                       for (p, ln, s, i, n) in unanchored],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_post(args):
    if not args.pr:
        sys.exit("post 需要 --pr（inline review 只能貼到 PR）")
    with open(args.diff, encoding="utf-8") as fh:
        diff = fh.read()
    diffmap = parse_diff(diff)
    truncated = TRUNCATION_MARKER in diff
    anchored, unanchored = split_anchored(_load_findings(args.findings), diffmap)
    summary = args.summary or ""
    if args.summary_file:
        with open(args.summary_file, encoding="utf-8") as fh:
            summary = fh.read()
    if not post_review(args.repo, args.pr, summary, anchored, unanchored, truncated, tag=args.tag):
        sys.exit("錯誤：貼附 review 失敗")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 中文 JSON 輸出在非 UTF-8 終端（Windows CP950 等）不崩
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")  # 中文警告/錯誤訊息也不崩
    ap = argparse.ArgumentParser(description="Claude 詳審 skill 的確定性零件 CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prep", help="取/截斷/解析 diff + 供應鏈 grounding")
    p.add_argument("--repo")
    p.add_argument("--pr")
    p.add_argument("--base", help="本機分支模式：對此 ref 取 git diff <base>...HEAD")
    p.add_argument("--diff-out", required=True, help="把（截斷後的）diff 寫到這個檔")
    p.add_argument("--max-diff-tokens", type=int, default=80000)
    p.add_argument("--no-deps", action="store_true")
    p.set_defaults(func=cmd_prep)

    a = sub.add_parser("anchor", help="把 findings 對齊 diff 可評論行（dry-run 預覽）")
    a.add_argument("--diff", required=True)
    a.add_argument("--findings", required=True)
    a.set_defaults(func=cmd_anchor)

    o = sub.add_parser("post", help="把 findings 貼成 GitHub inline review")
    o.add_argument("--repo", required=True)
    o.add_argument("--pr", required=True)
    o.add_argument("--diff", required=True)
    o.add_argument("--findings", required=True)
    o.add_argument("--summary", default="")
    o.add_argument("--summary-file")
    o.add_argument("--tag", default="claude-deep-review")
    o.set_defaults(func=cmd_post)

    args = ap.parse_args()
    try:
        args.func(args)
    except (RuntimeError, OSError) as e:  # 指令失敗(RuntimeError) 與檔案 I/O 錯誤(OSError) 都乾淨退出
        sys.exit(f"錯誤: {e}")


if __name__ == "__main__":
    main()
