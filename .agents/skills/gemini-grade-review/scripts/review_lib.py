#!/usr/bin/env python3
"""Gemini-grade review 的共用確定性零件（無 LLM、純標準庫）。

提供 Codex skill 的 diff 解析、行錨定、registry grounding 與貼附邏輯。
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
try:
    # gh / git 子行程秒數上限（可用環境變數覆寫）：無此上限時，卡住的 gh（過期認證觸發互動提示、
    # 網路停滯、rate-limit 退避）會讓無人值守的 fix-loop 永久阻塞。
    SUBPROCESS_TIMEOUT = int(os.environ.get("GGR_SUBPROCESS_TIMEOUT", "120"))
except ValueError:
    SUBPROCESS_TIMEOUT = 120
DEFAULT_USER_AGENT = "self-hosted-review-bot"
PYPI_REGISTRY_URL = "https://pypi.org/pypi"
NPM_REGISTRY_URL = "https://registry.npmjs.org"

# diff 截斷標記：truncate_diff 寫入、cmd_post 反推共用同一常數，避免字串字面跨模組耦合
TRUNCATION_MARKER = "[... diff 因過長被截斷；未保留的檔案或區段本次未審 ...]"

# Gemini-review: intentional — `critical` is absent because this skill's contract permits only high/medium findings.
SEVERITY_BADGE = {"high": "🔴 High", "medium": "🟡 Medium", "low": "🟢 Low"}

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
PY_EXTRAS_RE = re.compile(r"\[.*?\]")
PY_REQ_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(===?|~=|!=|<=|>=|<|>)?\s*([0-9][^\s;,#]*)?")
NPM_DEP_KEYS = ('"dependencies"', '"devDependencies"', '"peerDependencies"', '"optionalDependencies"')
NPM_KV_RE = re.compile(r'^\s*"([^"]+)"\s*:\s*"([^"]+)"\s*,?\s*$')
NPM_EXACT_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)  # 完整 SemVer（含 prerelease/build），不吃 range
NPM_NONREG_RE = re.compile(r"^\s*(workspace:|file:|link:|portal:|npm:|git|github:|gitlab:|bitbucket:|gist:|https?:|\.{0,2}/)")  # 非 npm registry spec（本地/私有/git/url/簡寫）


def sh(cmd, check=True):
    # 共用庫不直接 sys.exit（會讓呼叫端無法攔截/降級）；拋 RuntimeError 交由 CLI 入口處理
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=SUBPROCESS_TIMEOUT)  # 無上限時卡住的 gh/git 會讓無人值守流程永久阻塞
    except FileNotFoundError as e:
        raise RuntimeError(f"找不到執行檔 '{cmd[0]}'，請確認已安裝且在 PATH 中") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"指令逾時（>{SUBPROCESS_TIMEOUT}s）: {' '.join(cmd)}") from e
    if check and r.returncode != 0:
        raise RuntimeError(f"指令失敗: {' '.join(cmd)}\n{r.stderr}")
    return r.stdout


# ── diff 截斷與解析 ───────────────────────────────────────────────────────────
def _estimate_tokens(text):
    """保守的跨語言 token 估計：ASCII 約 CHARS_PER_TOKEN 字/token，非 ASCII（CJK 等 token 密集語言）
    每字約 ≥1 token。純 `len(text) / CHARS_PER_TOKEN` 會嚴重低估 CJK-heavy diff 的 token 數，讓實際
    超限的 diff 卻回 truncated=False、後段檔案被模型端靜默截斷（審查漏檔而不自知）。"""
    ascii_n = sum(1 for c in text if ord(c) < 128)
    return ascii_n / CHARS_PER_TOKEN + (len(text) - ascii_n)


def truncate_diff(diff, max_tokens):
    """超過上限時在『檔案邊界』截斷，避免切壞 hunk 或多位元組字元。回傳 (diff, truncated)。
    以 _estimate_tokens 的跨語言估計判斷，CJK-heavy diff 不會因純字元數低估而漏截。"""
    if _estimate_tokens(diff) <= max_tokens:
        return diff, False
    # 單次掃描累加估計，找出「估計首度超限」的字元位置（純 ASCII 時等同舊的 max_tokens*CHARS_PER_TOKEN）
    est, cutpos = 0.0, len(diff)
    for i, c in enumerate(diff):
        est += 1 if ord(c) >= 128 else 1 / CHARS_PER_TOKEN
        if est > max_tokens:
            cutpos = i
            break
    cut = diff.rfind("\ndiff --git", 0, cutpos)
    if cut <= 0:
        # 首檔本身超限時不能硬切 hunk 後假裝已審；寧可明確整份略過。
        return TRUNCATION_MARKER + "\n[... 首個變更檔案本身超過上限，整個檔案未審 ...]", True
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
        body = text[1:].strip() if isinstance(text, str) else ""
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
            if section_depth is not None and isinstance(text, str) and "}" in text:
                section_depth = None  # 刪除行含關閉括號 → 區塊邊界不可信，保守退出（含「關閉又另開」一行；prefer-miss）
            continue
        content = text[1:] if isinstance(text, str) else ""
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
    """寬鬆版號正規化（近 PEP 440）：小寫 + 去公開 release 段尾綴 '.0'，讓 1.0 ≡ 1.0.0（pip 視為同一版、裝得起來）。
    pre-release/local（rc1、+local、dev0）不會被去零吃掉，故不遮蔽真正不同的版號。
    local 版本識別（'+…'）先切開、整段保留：否則 1.0+abc.0 會被縮成 1.0+abc，而 PEP 440 視兩者為不同版
    （PyPI 不託管 local 版故現實中 releases 不含此段、不可利用，但仍與上句宣稱一致、避免將來誤用）。"""
    v = v.strip().lower()
    local = ""
    if "+" in v:
        v, local = v.split("+", 1)  # 只正規化 '+' 之前的公開 release 段，local 整段原樣保留
    parts = v.split(".")
    while len(parts) > 1 and parts[-1] == "0":
        parts.pop()
    base = ".".join(parts)
    return f"{base}+{local}" if local else base


def _version_present(ver, releases):
    """ver 是否存在於 releases：先字面、再處理常見尾端零等價。"""
    if ver in releases:
        return True
    target = _norm_ver(ver)
    return any(_norm_ver(k) == target for k in releases)


def _has_custom_pypi_index(info):
    """requirements 檔是否配置自訂索引（--index-url / --extra-index-url / -i / --find-links）。
    有的話套件可能只存在於私有索引，對 public PyPI 查 404 會是假陽性 high，應跳過該檔 grounding。
    僅能看到 diff hunk 內的指令行（add + context），屬 best-effort，與本模組其他 grounding 一致。"""
    directives = ("--index-url", "--extra-index-url", "-i ", "--find-links")
    for kind, _, text in info["events"]:
        if kind in ("add", "ctx") and isinstance(text, str):
            body = text[1:].strip().lower()
            if body.startswith(directives):
                return True
    return False


def _check_pypi(path, info):
    findings = []
    if _has_custom_pypi_index(info):
        print(f"  ⚠ {path} 配置自訂 PyPI 索引（--index-url 等）→ 跳過 PyPI grounding，避免私有套件被誤報 404", file=sys.stderr)
        return findings
    for line, name, op, ver in _added_python_reqs(info):
        status, data = _http_json(f"{PYPI_REGISTRY_URL}/{urllib.parse.quote(name)}/json")
        if status == "missing":
            findings.append((path, line, "high",
                f"依賴 `{name}` 在 PyPI 查無此套件（404）。可能是拼錯、未標明私有來源，或幻覺/slopsquatting 套件，請確認名稱與來源。", ""))
        elif status == "ok" and isinstance(data, dict) and op in ("==", "===") and ver:
            releases = data.get("releases")
            if not isinstance(releases, dict):  # 外部 API 結構防禦
                if op == "===":
                    print(f"  ⚠ PyPI 回應 {name} 的 releases 欄位格式異常 → 略過 arbitrary-equality 版號驗證", file=sys.stderr)
                    continue
                releases = {}
            if op == "==" and ver.endswith(".*"):
                # wildcard 是範圍、不是 exact pin；不以不完整自製 PEP 440 parser 宣告 high。
                # 套件存在性已由上方 package endpoint grounding，版號範圍留給套件管理器解析。
                continue
            if op == "===":
                present = ver in releases  # arbitrary equality 不套 PEP 440 正規化
            else:
                present = _version_present(ver, releases)
                if not present:
                    # 交由 PyPI 的 version endpoint 做完整 PEP 440 canonicalization；
                    # 404 才能確定版號不存在，查詢錯誤則寧可漏報、不可 false-high。
                    vurl = (f"{PYPI_REGISTRY_URL}/{urllib.parse.quote(name)}/"
                            f"{urllib.parse.quote(ver, safe='')}/json")
                    vstatus, vdata = _http_json(vurl)
                    present = vstatus == "ok" and isinstance(vdata, dict)
                    if vstatus == "ok" and not isinstance(vdata, dict):
                        print(f"  ⚠ PyPI 版號查詢 {name}=={ver} 回應格式異常 → 略過此版號", file=sys.stderr)
                        continue
                    if vstatus == "error":
                        print(f"  ⚠ PyPI 版號查詢 {name}=={ver} 失敗（{vdata}）→ 略過此版號", file=sys.stderr)
                        continue
            if not present:
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
        status, data = _http_json(f"{NPM_REGISTRY_URL}/{quoted}")
        if status == "missing":
            if name.startswith("@"):
                continue  # scoped 套件 404 多半是私有（monorepo 內部依賴），跳過避免假陽性
            findings.append((path, line, "high",
                f"依賴 `{name}` 在 npm registry 查無此套件（404）。可能是拼錯或幻覺/slopsquatting 套件，請確認名稱與來源。", ""))
        elif status == "ok" and isinstance(data, dict) and NPM_EXACT_RE.match(spec):
            versions = data.get("versions")
            if not isinstance(versions, dict):  # 外部 API 結構防禦
                print(f"  ⚠ npm 回應 {name} 的 versions 欄位格式異常 → 略過版號驗證", file=sys.stderr)
                continue
            # SemVer 的 build metadata（+foo）不參與 precedence / 相容性判定。
            # registry 可能以不同 build metadata（或不含 metadata）發布等價版本。
            requested = spec.split("+", 1)[0]
            present = any(
                isinstance(version, str) and version.split("+", 1)[0] == requested
                for version in versions
            )
            if not present:
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
    # Gemini-review: intentional — reject out-of-contract severities instead of silently expanding the review rubric.
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
    # 只去頭尾框架換行、保留內部縮排：suggestion 會原樣放進 ```suggestion``` 區塊由 GitHub 套用，
    # 用 .strip() 會連多行片段第一行的縮排一起吃掉（"    if x:\n        y" → "if x:\n        y"），
    # 套用後產生錯誤縮排／無效程式碼。
    sugg = sugg.strip("\r\n") if isinstance(sugg, str) else ""
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


def post_review(repo, pr, summary, anchored, unanchored, truncated, commit_id, tag="v3"):
    """貼成 GitHub inline review（event=COMMENT）。
    anchored: [(path, line, sev, issue, sugg)]；unanchored: [(path, line, sev, issue, note)]。"""
    if not repo or not isinstance(repo, str) or repo.count("/") != 1:
        print(f"⚠️ repo 格式錯誤（預期 'owner/repo'）：{repo}", file=sys.stderr)
        return False
    owner, name = repo.split("/")
    if not owner or not name:
        print(f"⚠️ repo 格式錯誤（owner 或 name 為空）：{repo}", file=sys.stderr)
        return False
    if not isinstance(commit_id, str) or not commit_id.strip():
        print("⚠️ 缺少 reviewed commit_id，拒絕把未綁定 head 的 review 貼到 PR", file=sys.stderr)
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
    payload = {"body": head, "event": "COMMENT", "commit_id": commit_id, "comments": comments}
    try:
        r = subprocess.run(["gh", "api", f"repos/{owner}/{name}/pulls/{pr}/reviews",
                            "--method", "POST", "--input", "-"],
                           input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=SUBPROCESS_TIMEOUT)
    except FileNotFoundError as e:
        raise RuntimeError("找不到執行檔 'gh'，請確認已安裝且在 PATH 中") from e
    except subprocess.TimeoutExpired as e:
        # 刻意不像「非零退出」那樣退化成單則留言：非零退出是**確定**的失敗（POST 沒成功、可安全改貼
        # fallback），但 POST 逾時是**不確定**結果——GitHub 可能已建立該 review，只是回應沒在時限內回來。
        # 此時再貼 fallback 會對已成功的非冪等寫入產生重複回饋。故硬中止並請人工確認，不盲目重貼。
        raise RuntimeError(f"貼附 review 逾時（>{SUBPROCESS_TIMEOUT}s）；結果不確定（GitHub 可能已收到該 review），"
                           f"請人工確認 PR #{pr} 現況後再決定是否重試，以免重複貼附") from e
    if r.returncode == 0:
        print(f"✓ 已貼 inline review（{len(comments)} 條 inline、{len(unanchored)} 條併入 summary）", file=sys.stderr)
        return True
    # 非零退出＝確定失敗；行錨定已先驗過，理論上極少走到這，保底退化成單則留言並印出原因供查
    print(f"⚠️ inline 貼附失敗 → 退化成單則留言：{r.stderr[:300]}", file=sys.stderr)
    current_head = sh(["gh", "pr", "view", str(pr), "--repo", repo,
                       "--json", "headRefOid", "--jq", ".headRefOid"]).strip()
    if current_head != commit_id:
        print(f"⚠️ PR head 已從 {commit_id} 變更為 {current_head}；拒絕貼 stale fallback 留言", file=sys.stderr)
        return False
    blob = head + "\n\n---\n"
    for p, ln, sev, issue, sg in anchored:
        blob += f"\n### `{p}:{ln}`\n{to_body(sev, issue, sg)}\n"
    try:  # 用 -F - 經 stdin 傳 body，避免大留言超過命令列長度上限（Windows ARG_MAX 8191）
        subprocess.run(["gh", "pr", "comment", str(pr), "--repo", repo, "-F", "-"],
                       input=blob, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
                       timeout=SUBPROCESS_TIMEOUT)
    except FileNotFoundError as e:
        raise RuntimeError("找不到執行檔 'gh'，請確認已安裝且在 PATH 中") from e
    except subprocess.TimeoutExpired:
        print(f"⚠️ 單則留言貼附逾時（>{SUBPROCESS_TIMEOUT}s）", file=sys.stderr)
        return False
    except subprocess.CalledProcessError as e:
        print(f"⚠️ 單則留言貼附也失敗：{(e.stderr or '')[:300]}", file=sys.stderr)
        return False
    print("✓ 已改用單則留言貼附", file=sys.stderr)
    return True  # 退化成單則留言也算成功送達，呼叫端不應誤判為完全失敗
