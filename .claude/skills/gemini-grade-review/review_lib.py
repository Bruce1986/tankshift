#!/usr/bin/env python3
"""自建 review 工具的共用確定性零件（無 LLM、純標準庫）。

被 review_pr_v3.py（Gemini 引擎）與 review_tools.py（Claude skill 的零件 CLI）共用，
避免 diff 解析 / 行錨定 / registry grounding / 貼附邏輯重複（DRY）。
"""
import http.client
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# 共用庫刻意不在模組層 reconfigure 全域 sys.stdout/stderr：那是會影響匯入端的全域副作用，屬應用程式進入點的責任。
# CLI 入口（review_tools.py / review_pr_v3.py 的 main）已對 stdout+stderr reconfigure；其他匯入者請在自己的進入點處理。
CHARS_PER_TOKEN = 4
try:
    REGISTRY_TIMEOUT = int(os.environ.get("GGR_REGISTRY_TIMEOUT", "10"))  # PyPI / npm 查詢秒數（可用環境變數覆寫）
except ValueError:
    REGISTRY_TIMEOUT = 10
DEFAULT_USER_AGENT = "self-hosted-review-bot"

# diff 截斷標記：truncate_diff 寫入、cmd_post 反推共用同一常數，避免字串字面跨模組耦合
TRUNCATION_MARKER = "[... 其餘檔案因 diff 過長被截斷，本次未審 ...]"

SEVERITY_BADGE = {"high": "🔴 High", "medium": "🟡 Medium", "low": "🟢 Low"}

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
PY_EXTRAS_RE = re.compile(r"\[.*?\]")
PY_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(===?|~=|!=|<=|>=|<|>)?\s*([0-9][^\s;,#]*)?")
NPM_DEP_KEYS = ('"dependencies"', '"devDependencies"', '"peerDependencies"', '"optionalDependencies"')
NPM_KV_RE = re.compile(r'^\s*"([^"]+)"\s*:\s*"([^"]+)"\s*,?\s*$')
NPM_EXACT_RE = re.compile(r"^\d+\.\d+\.\d+$")  # 只對「完整釘死」的精確版本驗版號存在（不吃 range，如 "1.2.3 || >=2"）
NPM_NONREG_RE = re.compile(r"^\s*(workspace:|file:|link:|portal:|npm:|git|github:|gitlab:|bitbucket:|gist:|https?:|\.{0,2}/)")  # 非 npm registry spec（本地/私有/git/url/簡寫）


def sh(cmd, check=True):
    # 共用庫不直接 sys.exit（會讓呼叫端無法攔截/降級）；拋 RuntimeError 交由 CLI 入口處理
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise RuntimeError(f"找不到執行檔 '{cmd[0]}'，請確認已安裝且在 PATH 中") from e
    if check and r.returncode != 0:
        raise RuntimeError(f"指令失敗: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout


# ── diff 截斷與解析 ───────────────────────────────────────────────────────────
def truncate_diff(diff, max_tokens):
    """超過上限時在『檔案邊界』截斷，避免切壞 hunk 或多位元組字元。回傳 (diff, truncated)。"""
    cap = max_tokens * CHARS_PER_TOKEN
    if len(diff) <= cap:
        return diff, False
    cut = diff.rfind("\ndiff --git", 0, cap)
    if cut <= 0:
        cut = cap  # 單一檔案本身就超過上限：退而硬切（極罕見）
    return diff[:cut] + "\n" + TRUNCATION_MARKER, True


def _unquote_path(p):
    """git 對含非 ASCII / 特殊字元的路徑會加引號並用 C-style 八進位跳脫（如 +++ "b/\\346...")；
    還原成原始 UTF-8 路徑，否則該檔的 finding 永遠對不上、無法錨定。"""
    if len(p) >= 2 and p.startswith('"') and p.endswith('"'):
        try:
            p = (p[1:-1].encode("latin-1", "backslashreplace")
                 .decode("unicode_escape").encode("latin-1").decode("utf-8"))
        except ValueError:  # 含 UnicodeDecode/EncodeError 與不完整跳脫序列（如孤立 \\u、\\x）
            p = p[1:-1]  # 還原失敗至少去引號
    return p


def parse_diff(diff):
    """解析 unified diff → {path: {commentable:set[int], hunks:[[start,end]], events:[(kind,line,text)]}}。
    commentable = 出現於 RIGHT 側（新增 '+' 與 context ' '）的新檔行號，唯這些行 GitHub inline review 才掛得上。"""
    files = {}
    path, new_line, in_hunk = None, None, False
    lines = diff.rstrip("\n").split("\n")  # 去掉結尾換行造成的尾端空字串（含多個連續），免得殘留
    for raw in lines:
        if raw.startswith("diff --git"):
            path, in_hunk = None, False
            continue
        if not in_hunk and raw.startswith("+++ "):
            p = _unquote_path(raw[4:].rstrip())
            if p.startswith("b/"):
                p = p[2:]
            path = None if p == "/dev/null" else p
            if path:
                files.setdefault(path, {"commentable": set(), "hunks": [], "events": []})
            continue
        m = HUNK_RE.match(raw)
        if m:
            if path:
                new_line = int(m.group(1))
                in_hunk = True
                files[path]["hunks"].append([new_line, new_line])
                files[path]["events"].append(("hunk", None, raw))
            continue
        if in_hunk and path:
            f = files[path]
            if raw.startswith("+"):
                f["commentable"].add(new_line)
                f["hunks"][-1][1] = new_line
                f["events"].append(("add", new_line, raw))
                new_line += 1
            elif raw.startswith("-"):
                f["events"].append(("del", None, raw))
            elif raw.startswith(" ") or raw == "":  # 空白 context 行（含被去尾隨空白的裸空行；尾端空行已先 pop）
                f["commentable"].add(new_line)
                f["hunks"][-1][1] = new_line
                f["events"].append(("ctx", new_line, raw))
                new_line += 1
            # 其他（"\ No newline at end of file" 等）→ 略過，不佔行號
    return files


def anchor_finding(path, line, diffmap):
    """把一條 finding 對齊到可評論行。
    回傳 (ok, anchored_line, note)。ok=True 可掛 inline；line 可能被吸附到同 hunk 最近的可評論行。"""
    if not path or not isinstance(line, int) or isinstance(line, bool):
        return False, None, "缺 path 或 line 非整數"
    info = diffmap.get(path)
    if not info:
        return False, None, f"{path} 不在本次 diff（可能指到未變更的檔）"
    if line in info["commentable"]:
        return True, line, ""
    for s, e in info["hunks"]:
        if s <= line <= e:
            cands = [c for c in info["commentable"] if s <= c <= e]
            if cands:
                nearest = min(cands, key=lambda c: abs(c - line))
                return True, nearest, f"原行 {line} 非可評論行，吸附到 {nearest}"
    return False, None, f"行 {line} 不落在任何 diff hunk"


# ── 供應鏈 registry grounding ────────────────────────────────────────────────
def _http_json(url):
    """GET 一個 JSON；404 → ("missing", None)；成功 → ("ok", data)；其餘錯誤 → ("error", msg)。"""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": DEFAULT_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=REGISTRY_TIMEOUT) as r:
            return "ok", json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "missing", None
        return "error", f"HTTP {e.code}"
    except (OSError, http.client.HTTPException, ValueError, json.JSONDecodeError) as e:
        # OSError 涵蓋 URLError/TimeoutError/SSLError/ConnectionReset；HTTPException 涵蓋 IncompleteRead 等
        return "error", str(e)[:120]


def _added_python_reqs(info):
    """從 requirements*.txt 的新增行取 [(line, name, op, version)]。"""
    out = []
    for kind, line, text in info["events"]:
        if kind != "add":
            continue
        body = text[1:].strip()
        # 略過註解/選項/URL/git+/PEP 508 直接引用(@)/本地路徑(/、\) → 免對非 PyPI 套件誤查
        if (not body or body[0] in "#-" or "://" in body or body.startswith("git+")
                or "@" in body or "/" in body or "\\" in body):
            continue
        m = PY_REQ_RE.match(PY_EXTRAS_RE.sub("", body))
        if m and m.group(1):
            out.append((line, m.group(1), m.group(2) or "", m.group(3) or ""))
    return out


def _added_npm_deps(info):
    """從 package.json 的新增行取 [(line, name, spec)]，只認 dependencies 類區塊內的行。
    用粗略括號深度 + hunk 斷點重置，寧可漏不可亂報（registry 還會再確認）。

    刻意只解析 diff、不讀磁碟上的完整 package.json：本工具須能對「遠端 PR 的 gh pr diff」運作，
    該情境下工作目錄可能停在別的分支（或根本沒 checkout 該 PR），磁碟上的 package.json 會是錯的版本，
    讀它反而比對到不相干的依賴清單。hunk 起點落在 `"dependencies": {` 之後造成的少數漏報，屬此處明定的
    prefer-miss 取捨（registry grounding 本就是 best-effort 補強，非完備保證）。"""
    out = []
    depth = 0
    section_depth = None
    for kind, line, text in info["events"]:
        if kind == "hunk":
            section_depth = None  # hunk 間可能跳行，深度推估不可信 → 保守退出區塊
            depth = 0  # 重置深度：hunk 間不連續，免得前一 hunk 未平衡的括號累積到下一 hunk
            continue
        if kind == "del":
            if section_depth is not None and text and "}" in text:  # text 防 falsy，與下方 content 防禦一致
                section_depth = None  # 刪除行含關閉括號 → 區塊邊界不可信，保守退出（含「關閉又另開」一行；prefer-miss）
            continue
        content = text[1:] if text else ""
        if section_depth is None:
            if any(k in content for k in NPM_DEP_KEYS) and "{" in content:
                section_depth = depth
        else:
            m = NPM_KV_RE.match(content)
            if m and kind == "add":
                out.append((line, m.group(1), m.group(2)))
        depth += content.count("{") - content.count("}")
        if section_depth is not None and depth <= section_depth:
            section_depth = None
    return out


def _norm_ver(v):
    """寬鬆版號正規化（近 PEP 440）：小寫 + 去尾綴 '.0' 段，讓 1.0 ≡ 1.0.0（pip 視為同一版、裝得起來）。
    pre-release/local（rc1、+local、dev0）不會被去零吃掉，故不遮蔽真正不同的版號。"""
    parts = v.strip().lower().split(".")
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    return ".".join(parts)


def _version_present(ver, releases):
    """ver 是否存在於 releases（key 集合）：先字面、再 PEP 440 正規化比對。
    releases 來自外部 API，key 不保證是字串 → 非字串 key 一律略過（_norm_ver 會對 int 拋 AttributeError）。"""
    if ver in releases:
        return True
    target = _norm_ver(ver)
    return any(isinstance(k, str) and _norm_ver(k) == target for k in releases)


def _prefix_extends(k, base):
    """release key `k` 是否為 wildcard base 的合法延伸（見 _version_pin_present 的 PEP 440 說明）。
    `4.2` 的延伸包含 `4.2`、`4.2.7`、`4.2rc1`，但不含 `4.20.1`（緊接數字 = 不同的 release segment）。"""
    if not k.startswith(base):
        return False
    rest = k[len(base):]
    return rest == "" or not rest[0].isdigit()


def _version_pin_present(ver, releases):
    """`==` pin 指向的版本是否真實存在。回傳 True / False / None（無法判定→不報）。

    wildcard pin（`4.2.*`）與 PEP 440 local version（`2.1.0+cu118`，公開索引不收 local 段）
    都無法逐字比對，但**仍可驗基底版號**。整條放行會放過 `totallyfakepkg==99.99.*` 這類
    幻覺版號，正好架空這個檢查的目的，故只放寬比對方式、不放棄比對。"""
    if "*" in ver:
        if not ver.endswith(".*") or "*" in ver[:-2]:
            return None                                    # 非單純 trailing wildcard，語法不明 → 不判定
        base = ver[:-2]                                    # "4.2.*" → "4.2"
        # PEP 440：`==4.2.*` 對 release segment 零填補後逐段比對。涵蓋三種形式（已對 packaging 驗證）：
        #   裸版號本身 4.2（pytz 這類 calendar versioning 只發 2024.1、不發 2024.1.x）
        #   後接新的一段 4.2.7
        #   後接無中介點的 pre/post/dev 修飾字 4.2rc1、4.2a1
        # 但**不**涵蓋 4.20.1 —— 那是把 base 當數字延伸，故只擋「base 後緊接數字」這一種。
        return (_version_present(base, releases)
                or any(isinstance(k, str) and _prefix_extends(k, base) for k in releases))
    if "+" in ver:
        base = ver.split("+", 1)[0]
        return _version_present(base, releases) if base else None
    return _version_present(ver, releases)


def _check_pypi(path, info):
    findings = []
    for line, name, op, ver in _added_python_reqs(info):
        status, data = _http_json(f"https://pypi.org/pypi/{urllib.parse.quote(name)}/json")
        if status == "missing":
            findings.append((path, line, "high",
                f"依賴 `{name}` 在 PyPI 查無此套件（404）。可能是拼錯、未標明私有來源，或幻覺/slopsquatting 套件，請確認名稱與來源。", ""))
        elif status == "ok" and isinstance(data, dict) and op in ("==", "===") and ver:
            releases = data.get("releases")
            if not isinstance(releases, dict):  # 外部 API 結構防禦
                releases = {}
            # PEP 440：1.0 ≡ 1.0.0（pip 裝得起來）→ 正規化比對，避免對等價 pin 誤報「版本不存在」；
            # wildcard / local version 改驗基底版號（見 _version_pin_present），None = 無法判定不報
            if _version_pin_present(ver, releases) is False:
                pinfo = data.get("info")
                latest = pinfo.get("version", "?") if isinstance(pinfo, dict) else "?"
                findings.append((path, line, "high",
                    f"`{name}=={ver}` 的版本不存在於 PyPI（套件存在，但無此版號；目前最新 {latest}）。可能是幻覺版號或筆誤，請改用真實版本。", ""))
        elif status == "error":
            print(f"  ⚠ PyPI 查詢 {name} 失敗（{data}）→ 略過此套件", file=sys.stderr)
    return findings


def _check_npm(path, info):
    findings = []
    for line, name, spec in _added_npm_deps(info):
        if NPM_NONREG_RE.match(spec):
            continue  # 本地/私有/git/url spec：不可當公開 registry 套件查（否則 monorepo/私有依賴會被誤報 404）
        quoted = urllib.parse.quote(name, safe="@")  # @scope/pkg → @scope%2Fpkg（並安全編碼其他特殊字元）
        status, data = _http_json(f"https://registry.npmjs.org/{quoted}")
        if status == "missing":
            if name.startswith("@"):
                continue  # scoped 套件 404 多半是私有（monorepo 內部依賴），跳過避免假陽性
            findings.append((path, line, "high",
                f"依賴 `{name}` 在 npm registry 查無此套件（404）。可能是拼錯或幻覺/slopsquatting 套件，請確認名稱與來源。", ""))
        elif status == "ok" and isinstance(data, dict) and NPM_EXACT_RE.fullmatch(spec):
            versions = data.get("versions")
            if not isinstance(versions, dict):  # 外部 API 結構防禦
                versions = {}
            if spec not in versions:
                dist_tags = data.get("dist-tags")
                latest = dist_tags.get("latest", "?") if isinstance(dist_tags, dict) else "?"
                findings.append((path, line, "high",
                    f"`{name}` 的精確版本 `{spec}` 不存在於 npm registry（最新 {latest}）。請改用真實版本。", ""))
        elif status == "error":
            print(f"  ⚠ npm 查詢 {name} 失敗（{data}）→ 略過此套件", file=sys.stderr)
    return findings


def dependency_findings(diffmap):
    """回傳確定性供應鏈 findings: [(path, line, severity, issue, suggestion)]。"""
    out = []
    for path, info in diffmap.items():
        base = path.rsplit("/", 1)[-1]
        dirs = path.split("/")[:-1]
        # requirements.txt / requirements-dev.txt，或 requirements/ 目錄拆分（requirements/prod.txt 等）
        if base.endswith(".txt") and (base.startswith("requirements") or "requirements" in dirs):
            out += _check_pypi(path, info)
        elif base == "package.json":
            out += _check_npm(path, info)
    return out


# ── findings 整理與輸出 ───────────────────────────────────────────────────────
def loads_lenient(text):
    """容錯解析模型 JSON：去除 ```json 圍欄、擷取最外層大括號 {…} 或中括號 […]；失敗回 {}。"""
    if not isinstance(text, str) or not text:  # 外部模型輸出：非字串/空 → 直接回 {}
        return {}
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        # 物件 {…} 或陣列 […]（LLM 有時夾說明文字回 bare array）：兩者都收集，按起始位置排序依序試，
        # 免得前面的無效區塊（如先出現但壞掉的 {…}）擋住後面有效的另一型結構
        ci, cj = t.find("{"), t.rfind("}")
        bi, bj = t.find("["), t.rfind("]")
        spans = []
        if 0 <= ci < cj:
            spans.append((ci, cj))
        if 0 <= bi < bj:
            spans.append((bi, bj))
        spans.sort(key=lambda s: s[0])
        for a, b in spans:
            try:
                return json.loads(t[a:b + 1])
            except json.JSONDecodeError:
                pass
    print(f"  ⚠ 無法解析模型輸出為 JSON（前 200 字）：{t[:200]}", file=sys.stderr)
    return {}


def normalize(f):
    """把一筆 finding dict 收斂成 (path,line,sev,issue,sugg)；非 high/medium 或缺 path/issue → None。"""
    if not isinstance(f, dict):  # 共用庫公開 API：非 dict 輸入直接淘汰，免 .get() 崩
        return None
    sev = str(f.get("severity", "")).lower()
    if sev not in ("high", "medium"):
        return None
    path = f.get("path")
    if not isinstance(path, str) or not path:  # 外部 JSON：path 須為非空字串
        return None
    issue = f.get("issue")
    if not isinstance(issue, str) or not issue.strip():  # issue 須為非空字串，免 .strip() 對非字串崩
        return None
    line = f.get("line")
    if isinstance(line, str) and line.isdigit():
        line = int(line)
    valid_line = isinstance(line, int) and not isinstance(line, bool)
    sugg = f.get("suggestion")
    sugg = sugg.strip() if isinstance(sugg, str) else ""
    return (path, line if valid_line else None, sev, issue.strip(), sugg)


def dedup(findings):
    """以 (path, line, issue 前 40 字) 去重，保序。findings: list of tuple。"""
    seen, out = set(), []
    for t in findings:
        key = (t[0], t[1], t[3][:40])
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def to_body(sev, issue, suggestion):
    body = f"**{SEVERITY_BADGE.get(sev, sev)}**\n\n{issue}"
    if suggestion:
        body += f"\n\n```suggestion\n{suggestion}\n```"
    return body


def split_anchored(findings, diffmap):
    """findings: [(path,line,sev,issue,sugg)] → (anchored, unanchored)。
    anchored: [(path, anchored_line, sev, issue, sugg)]；unanchored: [(path, line, sev, issue, note)]。"""
    anchored, unanchored, seen = [], [], set()
    for path, line, sev, issue, sugg in findings:
        ok, aline, note = anchor_finding(path, line, diffmap)
        if not ok:
            unanchored.append((path, line, sev, issue, note))
            continue
        key = (path, aline, issue[:40])  # 錨定後再去重一次：不同原始行吸附到同一行時不貼重複 inline
        if key in seen:
            continue
        seen.add(key)
        anchored.append((path, aline, sev, issue, sugg))
    return anchored, unanchored


def post_review(repo, pr, summary, anchored, unanchored, truncated, tag="v3"):
    """貼成 GitHub inline review（event=COMMENT）。
    anchored: [(path, line, sev, issue, sugg)]；unanchored: [(path, line, sev, issue, note)]。"""
    if not repo or not isinstance(repo, str) or repo.count("/") != 1:
        print(f"⚠️ repo 格式錯誤（預期 'owner/repo'）：{repo}", file=sys.stderr)
        return False
    owner, name = repo.split("/")
    if not owner or not name:
        print(f"⚠️ repo 格式錯誤（owner 或 name 為空）：{repo}", file=sys.stderr)
        return False
    head = f"## 🤖 自建詳審 ({tag})\n\n{summary}"
    if truncated:
        head += "\n\n> ⚠️ 本次 diff 過長已在檔界截斷，後段檔案未審。"
    if unanchored:
        head += "\n\n---\n### ⚠️ 下列問題無法對齊 diff 行（改列於此）\n"
        for path, line, sev, issue, note in unanchored:
            head += f"\n- **{SEVERITY_BADGE.get(sev, sev)}** `{path}:{line}` — {issue}　_({note})_"
    comments = [{"path": p, "line": ln, "side": "RIGHT", "body": to_body(sev, issue, sg)}
                for (p, ln, sev, issue, sg) in anchored]
    payload = {"body": head, "event": "COMMENT", "comments": comments}
    try:
        r = subprocess.run(["gh", "api", f"repos/{owner}/{name}/pulls/{pr}/reviews",
                            "--method", "POST", "--input", "-"],
                           input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True, encoding="utf-8", errors="replace")
    except FileNotFoundError as e:
        raise RuntimeError("找不到執行檔 'gh'，請確認已安裝且在 PATH 中") from e
    if r.returncode == 0:
        print(f"✓ 已貼 inline review（{len(comments)} 條 inline、{len(unanchored)} 條併入 summary）", file=sys.stderr)
        return True
    # 行錨定已先驗過，理論上極少走到這；保底退化成單則留言，並印出原因供查
    print(f"⚠️ inline 貼附失敗 → 退化成單則留言：{r.stderr[:300]}", file=sys.stderr)
    blob = head + "\n\n---\n"
    for p, ln, sev, issue, sg in anchored:
        blob += f"\n### `{p}:{ln}`\n{to_body(sev, issue, sg)}\n"
    try:  # 用 -F - 經 stdin 傳 body，避免大留言超過命令列長度上限（Windows ARG_MAX 8191）
        subprocess.run(["gh", "pr", "comment", str(pr), "--repo", repo, "-F", "-"],
                       input=blob, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    except FileNotFoundError as e:
        raise RuntimeError("找不到執行檔 'gh'，請確認已安裝且在 PATH 中") from e
    except subprocess.CalledProcessError as e:
        print(f"⚠️ 單則留言貼附也失敗：{(e.stderr or '')[:300]}", file=sys.stderr)
        return False
    print("✓ 已改用單則留言貼附", file=sys.stderr)
    return True  # 退化成單則留言也算成功送達，呼叫端不應誤判為完全失敗
