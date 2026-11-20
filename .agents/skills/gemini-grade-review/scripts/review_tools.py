#!/usr/bin/env python3
"""review_tools.py — Codex(+subagents) 詳審 skill 的確定性零件 CLI。

把 v3 的非 LLM 工作（取/截斷/解析 diff、供應鏈 grounding、行錨定、貼 inline review）
包成可從 shell 呼叫的子命令，讓 skill 專心做審查推理、把機械部分交給確定性程式。
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
          --commit-id <prep-head-sha> --diff /tmp/d.diff --findings /tmp/f.json \
          --summary-file /tmp/s.txt
"""
import argparse
import json
import sys

from review_lib import (
    dedup, dependency_findings, normalize, parse_diff,
    post_review, sh, split_anchored, truncate_diff,
)


def _prep_meta_path(diff_path):
    return diff_path + ".meta.json"


def _load_truncated(diff_path, explicit):
    """post 端解出「diff 是否被截斷」：優先讀 prep 寫出的 sidecar（權威布林），缺檔則用顯式 --truncated 旗標。

    刻意不用「diff 內含 TRUNCATION_MARKER」來反推——該標記是 review_lib 的字串常數，任何修改
    review_lib.py（或在測試/文件裡引用該常數）的 PR 都會讓標記以 diff 內容原樣出現，導致 cmd_post
    對這個 skill 自己的原始碼誤報「後段檔案未審」的自我指涉假陽性。"""
    try:
        with open(_prep_meta_path(diff_path), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("truncated"), bool):
            return data["truncated"]
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        pass  # sidecar 壞掉不阻斷貼附；退回顯式旗標（預設 False，寧可漏警告不可假警告）
    return bool(explicit)


def _fetch_diff(repo, pr, base):
    if pr and base:
        sys.exit("prep：--pr 與 --base 互斥，擇一")
    if pr:
        if not repo:
            sys.exit("prep：PR 模式需要 --repo <owner/repo>")
        return sh(["gh", "pr", "diff", str(pr), "--repo", repo])
    if base:
        # 明確釘住 a/、b/ 前綴：parse_diff 假設 '+++ b/…'，但呼叫端的 git 設定若有
        # diff.noprefix / diff.mnemonicPrefix / 自訂 src/dstPrefix，前綴會不同，導致
        # 該檔所有 finding 靜默變「不在 diff」而無從診斷。這也對齊 gh 的 GitHub-API diff 格式。
        return sh(["git", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false",
                   "diff", "--src-prefix=a/", "--dst-prefix=b/", f"{base}...HEAD"])
    sys.exit("prep 需要 --pr <num>（PR 模式）或 --base <ref>（本機分支模式）")


def _fetch_pr_head(repo, pr):
    """取得 PR 當前 head SHA；空值視為失敗，避免未綁定 head 的 review。"""
    sha = sh(["gh", "pr", "view", str(pr), "--repo", repo,
              "--json", "headRefOid", "--jq", ".headRefOid"]).strip()
    if not sha:
        raise RuntimeError(f"PR #{pr} 未回傳 head SHA")
    return sha


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
    if not items:
        return []
    normalized = []
    invalid = []
    for idx, item in enumerate(items):
        value = normalize(item) if isinstance(item, dict) else None
        if value is None:
            invalid.append(idx)
        else:
            normalized.append(value)
    if invalid:
        sys.exit(f"findings 含格式錯誤項目（index: {invalid}）；拒絕把壞輸出當成 clean review")
    return dedup(normalized)


def cmd_prep(args):
    if args.pr and not args.repo:
        sys.exit("prep：PR 模式需要 --repo <owner/repo>")
    head_sha = _fetch_pr_head(args.repo, args.pr) if args.pr else None
    raw_diff = _fetch_diff(args.repo, args.pr, args.base)
    if args.pr and _fetch_pr_head(args.repo, args.pr) != head_sha:
        sys.exit("prep 期間 PR head 已變更；請重新執行以取得一致 diff")
    diff, truncated = truncate_diff(raw_diff, args.max_diff_tokens)
    diffmap = parse_diff(diff)
    with open(args.diff_out, "w", encoding="utf-8") as fh:
        fh.write(diff)
    # sidecar：把 truncated 布林持久化，供 cmd_post 權威取用（見 _load_truncated：不靠掃描 diff 內容反推）
    with open(_prep_meta_path(args.diff_out), "w", encoding="utf-8") as fh:
        json.dump({"truncated": truncated}, fh)
    deps = [] if args.no_deps else dependency_findings(diffmap)
    out = {
        "diff_path": args.diff_out,
        "head_sha": head_sha,
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
    current_head = _fetch_pr_head(args.repo, args.pr)
    if current_head != args.commit_id:
        sys.exit(f"PR head 已從 {args.commit_id} 變更為 {current_head}；拒絕貼 stale review")
    with open(args.diff, encoding="utf-8") as fh:
        diff = fh.read()
    diffmap = parse_diff(diff)
    truncated = _load_truncated(args.diff, args.truncated)
    anchored, unanchored = split_anchored(_load_findings(args.findings), diffmap)
    summary = args.summary or ""
    if args.summary_file:
        with open(args.summary_file, encoding="utf-8") as fh:
            summary = fh.read()
    if not post_review(args.repo, args.pr, summary, anchored, unanchored, truncated,
                       args.commit_id, tag=args.tag):
        sys.exit("錯誤：貼附 review 失敗")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # 中文 JSON 輸出在非 UTF-8 終端（Windows CP950 等）不崩
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")  # 中文警告/錯誤訊息也不崩
    ap = argparse.ArgumentParser(description="Codex 詳審 skill 的確定性零件 CLI")
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
    o.add_argument("--commit-id", required=True, help="prep 回傳的 reviewed PR head SHA")
    o.add_argument("--diff", required=True)
    o.add_argument("--findings", required=True)
    o.add_argument("--summary", default="")
    o.add_argument("--summary-file")
    o.add_argument("--truncated", action="store_true",
                   help="diff 已被截斷（後段檔案未審）。一般不需手動帶——prep 會寫 sidecar 供 post 自動取用；"
                        "此旗標僅為 sidecar 不在時的後備")
    o.add_argument("--tag", default="gemini-grade-review")
    o.set_defaults(func=cmd_post)

    args = ap.parse_args()
    try:
        args.func(args)
    except (RuntimeError, OSError) as e:  # 指令失敗(RuntimeError) 與檔案 I/O 錯誤(OSError) 都乾淨退出
        sys.exit(f"錯誤: {e}")


if __name__ == "__main__":
    main()
