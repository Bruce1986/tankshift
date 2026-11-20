#!/usr/bin/env python3
"""review_lib.py 確定性零件的離線單元測試（不需 GEMINI_API_KEY、不需網路）。
涵蓋：diff 解析、行錨定、依賴擷取、嚴重度校準、檔界截斷、JSON 容錯。

執行: python3 test_review_lib.py            # 加 --net 才打真實 PyPI/npm
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # 讓本檔旁的 review_lib/review_tools 可被 import
import review_lib as lib  # noqa: E402
import review_tools as tools  # noqa: E402（與 lib 共用同一個 review_lib 模組實例）

# 涵蓋多種情形的合成 unified diff：
# - calc.py：兩個 hunk、含刪除行（不應佔新檔行號）
# - requirements.txt：新增 pinned/unpinned/註解
# - package.json：dependencies 區塊 vs scripts 區塊（後者不應被當依賴）
# - docs/old.md：刪除整檔（+++ /dev/null，不應被當有效檔）
DIFF = """diff --git a/app/calc.py b/app/calc.py
index 111..222 100644
--- a/app/calc.py
+++ b/app/calc.py
@@ -10,6 +10,8 @@ def total(items):
     s = 0
     for it in items:
-        s += it
+        s += it.price
+        s += tax(it)
     return s
@@ -40,2 +42,3 @@ def tax(it):
     rate = 0.05
+    return it.price * rate
diff --git a/requirements.txt b/requirements.txt
index 333..444 100644
--- a/requirements.txt
+++ b/requirements.txt
@@ -1,2 +1,5 @@
 requests==2.31.0
+pandas==2.2.0
+totallyfakepkg==9.9.9
+# a comment
+flask
diff --git a/package.json b/package.json
index 555..666 100644
--- a/package.json
+++ b/package.json
@@ -5,6 +5,9 @@
   "version": "1.0.0",
   "dependencies": {
     "react": "^18.2.0",
+    "left-pad": "1.3.0",
+    "definitely-not-real-pkg": "0.0.1"
   },
   "scripts": {
+    "build": "webpack"
   }
diff --git a/docs/old.md b/docs/old.md
deleted file mode 100644
index 777..000
--- a/docs/old.md
+++ /dev/null
@@ -1,2 +0,0 @@
-# gone
-bye
"""

_FAILED = []


def check(name, cond, extra=""):
    print(f"  {'✓' if cond else '✗ FAIL'}  {name}{'  ' + extra if extra else ''}")
    if not cond:
        _FAILED.append(name)


def test_parse_diff():
    print("[parse_diff]")
    dm = lib.parse_diff(DIFF)
    check("3 個有效檔（/dev/null 不算）",
          set(dm) == {"app/calc.py", "requirements.txt", "package.json"}, str(sorted(dm)))
    calc = dm["app/calc.py"]["commentable"]
    check("新增行 12,13 可評論", {12, 13} <= calc, str(sorted(calc)))
    check("刪除行不佔新檔行號（context 10,11,14 對齊）", {10, 11, 14, 42, 43} <= calc, str(sorted(calc)))


def test_anchor():
    print("[anchor_finding / split_anchored]")
    dm = lib.parse_diff(DIFF)
    check("命中可評論行 → ok", lib.anchor_finding("app/calc.py", 12, dm)[:2] == (True, 12))
    check("context 行可評論 → ok", lib.anchor_finding("app/calc.py", 10, dm)[:2] == (True, 10))
    check("hunk 外的行 → 降級", lib.anchor_finding("app/calc.py", 999, dm)[0] is False)
    check("hunk 間隙的行 → 降級", lib.anchor_finding("app/calc.py", 41, dm)[0] is False)
    check("檔不在 diff → 降級", lib.anchor_finding("nope.py", 3, dm)[0] is False)
    anc, un = lib.split_anchored(
        [("app/calc.py", 12, "high", "真問題", ""), ("nope.py", 1, "medium", "對不上", "")], dm)
    check("split_anchored 正確分流", len(anc) == 1 and len(un) == 1 and anc[0][1] == 12)
    anc2, _ = lib.split_anchored([("app/calc.py", 12, "high", "dup", ""), ("app/calc.py", 12, "high", "dup", "")], dm)
    check("split_anchored 同行同 issue 去重（防重複 inline）", len(anc2) == 1)
    check("line=True(bool) → 降級（不當第 1 行，#6）", lib.anchor_finding("app/calc.py", True, dm)[0] is False)


def test_dep_extract():
    print("[依賴擷取]")
    dm = lib.parse_diff(DIFF)
    reqs = lib._added_python_reqs(dm["requirements.txt"])
    names = {n for (_, n, _, _) in reqs}
    check("PyPI：pandas/totallyfakepkg/flask（# 註解略過）",
          names == {"pandas", "totallyfakepkg", "flask"}, str(sorted(names)))
    pinned = {n: ver for (_, n, op, ver) in reqs if op in ("==", "===")}
    check("PyPI：釘死版本被擷取", pinned == {"pandas": "2.2.0", "totallyfakepkg": "9.9.9"}, str(pinned))
    lp = lib._added_python_reqs(lib.parse_diff(
        'diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n'
        '@@ -1,1 +1,2 @@\n requests==2.31.0\n+local_packages/my_package\n')["requirements.txt"])
    check("本地路徑依賴（/）被排除", "local_packages" not in {n for (_, n, _, _) in lp})
    npm = {n for (_, n, _) in lib._added_npm_deps(dm["package.json"])}
    check("npm：只認 dependencies 區塊（scripts 的 build 不算）",
          npm == {"left-pad", "definitely-not-real-pkg"}, str(sorted(npm)))
    # PEP 508 直接引用（含 @）要被排除（Gemini round4）。fixture 刻意只含 '@'、不含 '/'、'://'、'git+'，
    # 才真的驗到 '@' 那條排除分支——若用 `mypkg @ file:///…`（原寫法），'/' 與 '://' 分支已先排除它，
    # 拿掉 '@' 條件測試照樣綠（恆過假綠）。
    at_reqs = lib._added_python_reqs(lib.parse_diff(
        'diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n'
        '@@ -1,1 +1,2 @@\n requests==2.31.0\n+mypkg @ localbuild\n')["requirements.txt"])
    check("PEP 508 直接引用（@）被排除", "mypkg" not in {n for (_, n, _, _) in at_reqs})


def test_normalize():
    print("[嚴重度校準]")
    check("critical 超出 high/medium contract → 丟棄",
          lib.normalize({"path": "a", "line": 1, "severity": "critical", "issue": "x"}) is None)
    check("low 被丟", lib.normalize({"path": "a", "line": 1, "severity": "low", "issue": "x"}) is None)
    check("medium 保留", lib.normalize({"path": "a", "line": 1, "severity": "medium", "issue": "x"})[2] == "medium")
    check("line 字串數字轉 int", lib.normalize({"path": "a", "line": "5", "severity": "high", "issue": "x"})[1] == 5)
    check("缺 issue 丟掉", lib.normalize({"path": "a", "line": 1, "severity": "high", "issue": ""}) is None)
    check("line=True(bool) → line 收斂成 None（#6）",
          lib.normalize({"path": "a", "line": True, "severity": "high", "issue": "x"})[1] is None)
    check("非 dict 輸入 → None", lib.normalize("not a dict") is None)
    check("issue 非字串(int) → None", lib.normalize({"path": "a", "line": 1, "severity": "high", "issue": 5}) is None)
    check("path 非字串(list) → None", lib.normalize({"path": ["a"], "line": 1, "severity": "high", "issue": "x"}) is None)
    check("suggestion 非字串 → 空字串",
          lib.normalize({"path": "a", "line": 1, "severity": "high", "issue": "x", "suggestion": 9})[4] == "")
    # 多行縮排 suggestion：只去頭尾框架換行、保留內部縮排（否則套用 ```suggestion``` 後縮排壞掉）
    check("多行 suggestion 保留第一行縮排（不被 strip 吃掉）",
          lib.normalize({"path": "a", "line": 1, "severity": "high", "issue": "x",
                         "suggestion": "\n    if ok:\n        run()\n"})[4] == "    if ok:\n        run()")


def test_truncate():
    print("[檔界截斷]")
    idx2 = DIFF.index("\ndiff --git", 5)
    idx3 = DIFF.index("\ndiff --git", idx2 + 5)
    cap_tokens = ((idx2 + idx3) // 2) // lib.CHARS_PER_TOKEN
    d, trunc = lib.truncate_diff(DIFF, cap_tokens)
    kept = d[:d.rindex("\n[... ")]
    check("切在 diff --git 邊界、保留完整檔",
          trunc and DIFF.startswith(kept) and DIFF[len(kept):].startswith("\ndiff --git"))
    d2, trunc2 = lib.truncate_diff(DIFF, 10 ** 6)
    check("未超限 → 不截斷", trunc2 is False and d2 == DIFF)
    oversized = "diff --git a/huge.py b/huge.py\n--- a/huge.py\n+++ b/huge.py\n@@ -0,0 +1,2 @@\n+" + "x" * 100
    d3, trunc3 = lib.truncate_diff(oversized, 1)
    check("首檔超限 → 整檔略過、不留下半截 hunk",
          trunc3 and d3.startswith(lib.TRUNCATION_MARKER) and not any(
              d3.startswith(oversized[:prefix_len]) for prefix_len in range(1, min(len(oversized), 16) + 1)
          ))


def test_lenient_json():
    print("[loads_lenient]")
    check("去 ```json 圍欄", lib.loads_lenient('```json\n{"a":1}\n```') == {"a": 1})
    check("擷取最外層大括號", lib.loads_lenient('垃圾{"a":2}尾巴') == {"a": 2})
    check("擷取最外層中括號（JSON array）", lib.loads_lenient('垃圾[1, 2]尾巴') == [1, 2])
    check("array of objects", lib.loads_lenient('say:\n[{"a": 1}]') == [{"a": 1}])
    check("物件在前(無效)、陣列在後(有效) → 取到陣列", lib.loads_lenient('{bad} [1,2]') == [1, 2])
    check("壞 JSON → {}", lib.loads_lenient("not json") == {})


def test_type_defense_round16():
    print("[loads_lenient / _added_npm_deps 型別防禦（Gemini round16）]")
    check("loads_lenient 非字串(dict) → {}", lib.loads_lenient({"a": 1}) == {})
    check("loads_lenient None → {}", lib.loads_lenient(None) == {})
    check("_added_python_reqs text=None/int 不崩",
          lib._added_python_reqs({"events": [("add", 1, None), ("add", 2, 7)]}) == [])
    check("_added_npm_deps text=None/int 不崩",
          lib._added_npm_deps({"events": [("del", 1, None), ("del", 2, 7), ("add", 3, 7)]}) == [])


def test_registry_net():
    """需網路：對真實 PyPI/npm 確認假套件被擋、真套件放行。"""
    print("[registry grounding（需網路）]")
    dm = lib.parse_diff(DIFF)
    issues = [f[3] for f in lib.dependency_findings(dm)]
    check("totallyfakepkg 被擋（PyPI 404）", any("totallyfakepkg" in v for v in issues))
    check("definitely-not-real-pkg 被擋（npm 404）", any("definitely-not-real-pkg" in v for v in issues))
    check("真套件 pandas/flask/react/left-pad 未誤擋",
          not any(p in v for v in issues for p in ("pandas", "flask", "react", "left-pad")))


def test_npm_spec_regexes():
    print("[npm spec 正則：精確版本 vs range / 非 registry（#4/#5）]")
    check("NPM_EXACT_RE 認完整精確版本 1.2.3", bool(lib.NPM_EXACT_RE.match("1.2.3")))
    check("NPM_EXACT_RE 認 prerelease", bool(lib.NPM_EXACT_RE.match("1.2.3-beta.1")))
    check("NPM_EXACT_RE 認 build metadata", bool(lib.NPM_EXACT_RE.match("1.2.3+build.5")))
    check("NPM_EXACT_RE 不吃 range '1.2.3 || >=2'", lib.NPM_EXACT_RE.match("1.2.3 || >=2") is None)
    check("NPM_EXACT_RE 不吃 caret range", lib.NPM_EXACT_RE.match("^1.2.3") is None)
    for s in ("workspace:*", "file:../x", "git+https://x", "github:a/b", "gitlab:a/b",
              "bitbucket:me/repo", "gist:abc123", "https://x.tgz", "npm:y@1.0.0",
              "./local", "../p", "link:../p"):
        check(f"NPM_NONREG_RE 認非 registry spec：{s}", bool(lib.NPM_NONREG_RE.match(s)))
    check("NPM_NONREG_RE 不誤判 ^1.2.3 / 1.2.3",
          lib.NPM_NONREG_RE.match("^1.2.3") is None and lib.NPM_NONREG_RE.match("1.2.3") is None)


def test_registry_nondict():
    print("[registry 非 dict 防禦（A1）]")
    orig = lib._http_json
    try:
        for bad in (["not", "a", "dict"], "json-string", 42):  # 合法 JSON 但非 dict（list/str/num）
            lib._http_json = lambda url, _b=bad: ("ok", _b)
            try:
                lib.dependency_findings(lib.parse_diff(DIFF))
                check(f"非 dict registry 不 crash（{type(bad).__name__}）", True)
            except AttributeError:
                check(f"非 dict registry 不 crash（{type(bad).__name__}）", False, "AttributeError")
    finally:
        lib._http_json = orig


_test_tmpdir = None


def _write(name, content):
    if _test_tmpdir is None:
        raise RuntimeError("測試暫存目錄尚未初始化")
    p = os.path.join(_test_tmpdir, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    return p


def test_npm_build_metadata_equivalence():
    print("[npm SemVer build metadata 等價]")
    info = lib.parse_diff('diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n'
                          '@@ -1,2 +1,3 @@\n   "dependencies": {\n+    "pkg": "1.2.3+client"\n   }\n')["package.json"]
    orig = lib._http_json
    try:
        lib._http_json = lambda url: ("ok", {"versions": {"1.2.3": {}}, "dist-tags": {"latest": "1.2.3"}})
        check("不同／缺省 build metadata 仍視為同一 SemVer", lib._check_npm("package.json", info) == [])
        lib._http_json = lambda url: ("ok", {"versions": {"1.2.3-beta.1+server": {}}, "dist-tags": {}})
        check("prerelease 不同仍不可誤判等價", any("pkg" in f[3] for f in lib._check_npm("package.json", info)))
    finally:
        lib._http_json = orig


def test_tools_load_findings():
    print("[review_tools _load_findings（B1/B2）]")
    try:
        tools._load_findings("/no/such/ggr_findings.json")
        check("缺檔 → SystemExit", False)
    except SystemExit:
        check("缺檔 → SystemExit", True)
    try:
        tools._load_findings(_write("ggr_bad.json", "{not json"))
        check("壞 JSON → SystemExit", False)
    except SystemExit:
        check("壞 JSON → SystemExit", True)
    nokey = _write("ggr_nokey.json", '{"issues":[{"path":"a","line":1,"severity":"high","issue":"x"}]}')
    check("dict 有 issues 鍵 → 相容載入 1 條", len(tools._load_findings(nokey)) == 1)
    try:
        tools._load_findings(_write("ggr_nokey2.json", '{"other": 1}'))
        check("dict 無 findings/issues 鍵 → SystemExit", False)
    except SystemExit:
        check("dict 無 findings/issues 鍵 → SystemExit", True)
    scal = _write("ggr_scalars.json", '["a",1,{"path":"f","line":2,"severity":"medium","issue":"real"}]')
    try:
        tools._load_findings(scal)
        check("list 含壞項目 → SystemExit（不靜默過濾）", False)
    except SystemExit:
        check("list 含壞項目 → SystemExit（不靜默過濾）", True)
    try:
        tools._load_findings(_write("ggr_all_bad.json", '[{"path":"","severity":"low","issue":""}]'))
        check("非空但全壞 → 不可當 clean", False)
    except SystemExit:
        check("非空但全壞 → 不可當 clean", True)
    check("顯式空 list → clean", tools._load_findings(_write("ggr_empty.json", '[]')) == [])
    try:
        tools._load_findings(_write("ggr_nonlist.json", '{"findings": 7}'))
        check("findings 非 list → SystemExit", False)
    except SystemExit:
        check("findings 非 list → SystemExit", True)
    blist = _write("ggr_list.json", '[{"path":"x","line":1,"severity":"high","issue":"ok"}]')
    check("頂層 list 正常解析", len(tools._load_findings(blist)) == 1)


def test_tools_fetch_diff():
    print("[review_tools _fetch_diff 參數驗證（B3）]")
    try:
        tools._fetch_diff(None, "5", None)
        check("PR 模式缺 --repo → SystemExit", False)
    except SystemExit:
        check("PR 模式缺 --repo → SystemExit", True)
    try:
        tools._fetch_diff("o/r", "5", "main")
        check("--pr 與 --base 並用 → SystemExit", False)
    except SystemExit:
        check("--pr 與 --base 並用 → SystemExit", True)
    try:
        tools._fetch_diff(None, None, None)
        check("--pr/--base 皆無 → SystemExit", False)
    except SystemExit:
        check("--pr/--base 皆無 → SystemExit", True)
    _osh = tools.sh
    captured = []
    tools.sh = lambda cmd, check=True: captured.append(cmd) or "DIFF"  # 不實際呼叫 gh/git，只驗合法組合＋捕捉命令
    try:
        check("base-only 通過驗證", tools._fetch_diff(None, None, "main") == "DIFF")
        # base 模式必須釘住 a/、b/ 前綴，否則呼叫端的 diff.noprefix/mnemonicPrefix 會讓 parse_diff
        # 的 '+++ b/…' 假設落空、該檔所有 finding 靜默變「不在 diff」。
        base_cmd = captured[-1]
        check("base 模式 git diff 釘住 --src-prefix=a/", "--src-prefix=a/" in base_cmd, str(base_cmd))
        check("base 模式 git diff 釘住 --dst-prefix=b/", "--dst-prefix=b/" in base_cmd, str(base_cmd))
        check("pr+repo 通過驗證", tools._fetch_diff("o/r", "5", None) == "DIFF")
        check("PR head SHA 去空白", tools._fetch_pr_head("o/r", "5") == "DIFF")
    finally:
        tools.sh = _osh


def test_tools_pr_head_guards():
    print("[review_tools PR head 綁定]")
    import types
    orig_head, orig_diff = tools._fetch_pr_head, tools._fetch_diff
    try:
        tools._fetch_pr_head = lambda repo, pr: (_ for _ in ()).throw(AssertionError("不應先取 head"))
        tools._fetch_diff = lambda repo, pr, base: (_ for _ in ()).throw(AssertionError("不應先取 diff"))
        missing_repo = types.SimpleNamespace(repo=None, pr="5", base=None, max_diff_tokens=100,
                                             diff_out="unused", no_deps=True)
        try:
            tools.cmd_prep(missing_repo)
            check("cmd_prep 缺 --repo → 在 subprocess 前 SystemExit", False)
        except SystemExit:
            check("cmd_prep 缺 --repo → 在 subprocess 前 SystemExit", True)

        heads = iter(["oldsha", "newsha"])
        tools._fetch_pr_head = lambda repo, pr: next(heads)
        tools._fetch_diff = lambda repo, pr, base: "diff --git a/x b/x\n"
        args = types.SimpleNamespace(repo="o/r", pr="5", base=None, max_diff_tokens=100,
                                     diff_out="unused", no_deps=True)
        try:
            tools.cmd_prep(args)
            check("prep 期間 head 移動 → SystemExit", False)
        except SystemExit:
            check("prep 期間 head 移動 → SystemExit", True)

        tools._fetch_pr_head = lambda repo, pr: "newsha"
        post_args = types.SimpleNamespace(repo="o/r", pr="5", commit_id="oldsha",
                                          diff="unused", findings="unused", summary="",
                                          summary_file=None, tag="test")
        try:
            tools.cmd_post(post_args)
            check("post commit_id 不同於當前 head → SystemExit", False)
        except SystemExit:
            check("post commit_id 不同於當前 head → SystemExit", True)
    finally:
        tools._fetch_pr_head, tools._fetch_diff = orig_head, orig_diff


def test_pypi_version_norm():
    print("[PyPI 版號 PEP440 正規化（收回 #7）]")
    check("1.0 ≡ 1.0.0", lib._norm_ver("1.0") == lib._norm_ver("1.0.0"))
    check("1.10 ≠ 1.1（不誤縮）", lib._norm_ver("1.10") != lib._norm_ver("1.1"))
    check("1.0rc1 不被去零吃掉", lib._norm_ver("1.0rc1") != lib._norm_ver("1.0"))
    # local 版本識別段（+…）整段保留：1.0+abc.0 與 1.0+abc 是 PEP 440 不同版，不可被去零縮成相同
    check("1.0+abc.0 ≠ 1.0+abc（local 段的 .0 不被去零吃掉）",
          lib._norm_ver("1.0+abc.0") != lib._norm_ver("1.0+abc"))
    # 但公開 release 段仍正規化：1.0+abc ≡ 1.0.0+abc（同版、只是 release 段寫法不同）
    check("1.0+abc ≡ 1.0.0+abc（公開段照常正規化、local 保留）",
          lib._norm_ver("1.0+abc") == lib._norm_ver("1.0.0+abc"))
    rel = {"1.0.0": [], "2.3": []}
    check("pin 1.0 命中 1.0.0（等價、不誤報）", lib._version_present("1.0", rel))
    check("pin 2.3.0 命中 2.3", lib._version_present("2.3.0", rel))
    check("pin 9.9.9 確實不存在", not lib._version_present("9.9.9", rel))


def test_pypi_emit():
    print("[_check_pypi 正規化比對：等價 pin 不誤報、真缺要抓（收回 #7）]")
    diff = ("diff --git a/requirements.txt b/requirements.txt\n"
            "--- a/requirements.txt\n+++ b/requirements.txt\n"
            "@@ -1,1 +1,7 @@\n requests==2.31.0\n+baz==2.31.0\n+foo==1.0\n"
            "+wild==4.2.*\n+spell==1.0alpha1\n+weird==1.0alpha1\n+bar==9.9.9\n")
    info = lib.parse_diff(diff)["requirements.txt"]
    orig = lib._http_json
    def fake_pypi(url):
        if "/bar/9.9.9/json" in url:
            return "missing", None
        if "/spell/1.0alpha1/json" in url:
            return "ok", {"info": {"version": "1.0a1"}}
        if "/weird/1.0alpha1/json" in url:
            return "ok", ["malformed-version-response"]
        return "ok", {"releases": {"1.0.0": [], "2.31.0": [], "4.2.0": [], "4.2.1": []},
                      "info": {"version": "4.2.1"}}
    lib._http_json = fake_pypi
    try:
        findings = lib._check_pypi("requirements.txt", info)
        flagged = " ".join(f[3] for f in findings)
        check("baz==2.31.0 精確命中 → 不報", "baz" not in flagged)
        check("foo==1.0 ≡ 已發 1.0.0（PEP440）→ 不誤報", "foo" not in flagged)
        check("wild==4.2.* 非 exact pin → 只驗套件存在、不誤報版號", "wild" not in flagged)
        check("spell 等價拼法由 version endpoint 確認 → 不誤報", "spell" not in flagged)
        check("version endpoint 200 但非 dict → 略過、不誤報不存在", "weird" not in flagged)
        check("bar==9.9.9 不存在 → 抓 high", "bar" in flagged)
        check("剛好一條（bar）", len(findings) == 1 and findings[0][2] == "high")
    finally:
        lib._http_json = orig


def test_truncation_roundtrip():
    print("[截斷標記跨模組往返（B5）]")
    big = "\n".join(f"diff --git a/f{i} b/f{i}\n+x" for i in range(50))
    out, trunc = lib.truncate_diff(big, 1)  # cap 極小 → 必截
    check("truncate_diff 回 truncated=True", trunc)
    check("輸出含共用常數 TRUNCATION_MARKER", lib.TRUNCATION_MARKER in out)


def test_truncated_flag_persisted_not_sniffed():
    print("[cmd_post 的 truncated 走 sidecar 權威值、不掃 diff 內容（L1 自我指涉假陽性）]")
    # 核心回歸：一份「未截斷」但『內容剛好含 TRUNCATION_MARKER 字面』的 diff——例如任何修改
    # review_lib.py（該常數的定義處）或在測試/文件引用該常數的 PR——不可被 _load_truncated 誤判為截斷。
    marker_as_content = (f"diff --git a/review_lib.py b/review_lib.py\n"
                         f"--- a/review_lib.py\n+++ b/review_lib.py\n"
                         f"@@ -1,1 +1,2 @@\n x\n+TRUNCATION_MARKER = \"{lib.TRUNCATION_MARKER}\"\n")
    diff_path = _write("ggr_untruncated.diff", marker_as_content)
    # 對應 sidecar 標明未截斷（prep 會這樣寫）
    _write("ggr_untruncated.diff.meta.json", json.dumps({"truncated": False}))
    check("sidecar=false → 不誤判為截斷（即使 diff 內含 marker 字面）",
          tools._load_truncated(diff_path, explicit=False) is False)

    # sidecar 標明已截斷 → 取用 True
    _write("ggr_truncated.diff.meta.json", json.dumps({"truncated": True}))
    tp = _write("ggr_truncated.diff", "diff --git a/f b/f\n+y\n")
    check("sidecar=true → truncated=True", tools._load_truncated(tp, explicit=False) is True)

    # sidecar 不存在 → 退回顯式旗標（預設 False），仍不掃 diff 內容
    np = _write("ggr_nometa.diff", marker_as_content)
    check("無 sidecar + explicit=False → False（不因 diff 含 marker 而 True）",
          tools._load_truncated(np, explicit=False) is False)
    check("無 sidecar + explicit=True → True", tools._load_truncated(np, explicit=True) is True)
    # sidecar 壞掉 → 不阻斷、退回顯式旗標
    _write("ggr_badmeta.diff.meta.json", "{not json")
    bp = _write("ggr_badmeta.diff", "diff --git a/f b/f\n+y\n")
    check("sidecar 壞 JSON → 退回 explicit（此處 False）", tools._load_truncated(bp, explicit=False) is False)


def test_parse_diff_edges():
    print("[parse_diff 邊界：空白行號 / 尾端空行 / 引號 CJK 路徑（#1/#2）]")
    # canonical 空白 context（前導空白）要佔行號 → +b 在第 3 行
    d = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
         "@@ -1,3 +1,4 @@\n a\n \n+b\n c\n")  # 第 2 行是 " "（空白 context）
    adds = [ln for (k, ln, t) in lib.parse_diff(d)["x.py"]["events"] if k == "add"]
    check("空白 context 行佔行號 → +b 在第 3 行", adds == [3], f"adds={adds}")
    # 尾端多個換行不可造出 EOF 之後的幻影可評論行（#1 修正避免幻影）
    eof = lib.parse_diff("diff --git a/y.py b/y.py\n--- a/y.py\n+++ b/y.py\n@@ -0,0 +1 @@\n+only\n\n")
    check("尾端 \\n\\n 不造幻影行（只第 1 行可評論）", eof["y.py"]["commentable"] == {1}, str(eof["y.py"]["commentable"]))
    # git 引號 + 八進位 CJK 路徑還原
    cjk = ('diff --git "a/\\346\\226\\207.py" "b/\\346\\226\\207.py"\n'
           '--- "a/\\346\\226\\207.py"\n+++ "b/\\346\\226\\207.py"\n'
           '@@ -0,0 +1 @@\n+x\n')
    check("git 引號 + 八進位 CJK 路徑還原成 文.py", "文.py" in lib.parse_diff(cjk), "")
    # 被去尾隨空白的「裸空行」"" 也要佔行號（Gemini #1 high：避免行偏移）
    db = "diff --git a/z.py b/z.py\n--- a/z.py\n+++ b/z.py\n@@ -1,3 +1,4 @@\n a\n\n+b\n c\n"
    adds_db = [ln for (k, ln, t) in lib.parse_diff(db)["z.py"]["events"] if k == "add"]
    check('裸空行 "" 佔行號 → +b 仍在第 3 行（不偏移）', adds_db == [3], f"adds={adds_db}")
    check("_unquote_path 不完整跳脫不崩（攔 ValueError）", lib._unquote_path('"b/bad\\x"') == "b/bad\\x")


def test_npm_del_brace():
    print("[npm 刪除括號：含 } 即保守退出（#3，prefer-miss）]")
    base = ('diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n'
            '@@ -1,6 +1,6 @@\n   "dependencies": {\n     "react": "^18.0.0"\n')
    # (a) 純關閉 -}, → 退出，scripts 內 fakepkg 不算依賴
    na = {n for (_, n, _) in lib._added_npm_deps(lib.parse_diff(
        base + '-  },\n   "scripts": {\n+    "fakepkg": "9.9.9"\n   }\n')["package.json"])}
    check("純關閉 -}, → fakepkg 不算依賴", "fakepkg" not in na, str(na))
    # (b) 關閉又另開一行 -}, "scripts": {（括號平衡）→ 仍退出，後續不誤報
    nb = {n for (_, n, _) in lib._added_npm_deps(lib.parse_diff(
        base + '-  }, "scripts": {\n+    "build": "9.9.9"\n   }\n')["package.json"])}
    check("關閉又另開一行 → build 不被誤報依賴", "build" not in nb, str(nb))
    # (c) 刪平衡 {…} 行 → 也保守退出（可接受 FN：寧可漏不可亂報）
    nc = {n for (_, n, _) in lib._added_npm_deps(lib.parse_diff(
        base + '-    "old": { "x": true },\n+    "realdep": "9.9.9"\n   }\n')["package.json"])}
    check("刪平衡 {…} → realdep 保守丟棄（FN 可接受）", "realdep" not in nc, str(nc))
    # 跨 hunk：前一 hunk 的括號不平衡不影響後一 hunk（hunk 邊界 depth 重置，Gemini #3）
    nm = {n for (_, n, _) in lib._added_npm_deps(lib.parse_diff(
        'diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n'
        '@@ -1,2 +1,2 @@\n+  "leftover": {\n@@ -9,3 +9,4 @@\n   "dependencies": {\n'
        '+    "realpkg": "1.0.0"\n   }\n')["package.json"])}
    check("跨 hunk depth 重置 → 後一 hunk 的 realpkg 偵測正確", "realpkg" in nm, str(nm))


def test_sh_raises():
    print("[sh 失敗拋 RuntimeError（非 sys.exit）]")
    fail = ["python3", "-c", "import sys; sys.exit(1)"]
    try:
        lib.sh(fail)
        check("失敗指令拋 RuntimeError", False)
    except RuntimeError:
        check("失敗指令拋 RuntimeError", True)
    except SystemExit:
        check("失敗指令拋 RuntimeError", False, "拋了 SystemExit（舊行為）")
    check("check=False 不拋、回 stdout", lib.sh(fail, check=False) == "")
    try:
        lib.sh(["no-such-binary-xyz-123"])
        check("找不到執行檔 → RuntimeError", False)
    except RuntimeError:
        check("找不到執行檔 → RuntimeError", True)


def test_npm_scoped_skip():
    print("[npm scoped 套件 404 跳過避免假陽性（Gemini round4）]")
    diff = ('diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n'
            '@@ -1,3 +1,5 @@\n   "dependencies": {\n'
            '+    "@myorg/private": "1.0.0",\n+    "realfake": "1.0.0"\n   }\n')
    info = lib.parse_diff(diff)["package.json"]
    orig = lib._http_json
    lib._http_json = lambda url: ("missing", None)  # 兩個都 404
    try:
        issues = " ".join(f[3] for f in lib._check_npm("package.json", info))
        check("scoped @myorg/private 404 → 跳過（不報）", "@myorg/private" not in issues, issues[:60])
        check("非 scoped realfake 404 → 仍報", "realfake" in issues)
    finally:
        lib._http_json = orig


def test_post_review_return():
    print("[post_review 回傳值 / gh 不存在（Gemini round5 #3/#4）]")
    import types
    orig_run, orig_sh = lib.subprocess.run, lib.sh
    try:
        success_calls = []
        def _inline_ok(*args, **kwargs):
            success_calls.append((args, kwargs))
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        lib.subprocess.run = _inline_ok
        check("inline 成功 → True", lib.post_review("o/r", 1, "s", [], [], False, "abc123") is True)
        check("inline payload 綁定 reviewed commit_id",
              '"commit_id": "abc123"' in success_calls[0][1].get("input", ""))

        calls = []
        def _fallback_ok(*args, **kwargs):
            calls.append((args, kwargs))
            if len(calls) == 1:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="inline err")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        lib.subprocess.run = _fallback_ok
        lib.sh = lambda *args, **kwargs: "abc123\n"
        check("inline 失敗但 fallback 成功 → True",
              lib.post_review("o/r", 1, "s", [], [], False, "abc123") is True)
        check("fallback 使用 gh pr comment + stdin",
              len(calls) == 2 and calls[1][0][0][:3] == ["gh", "pr", "comment"]
              and calls[1][1].get("input") is not None)

        fail_calls = 0
        def _fallback_fail(*args, **kwargs):
            nonlocal fail_calls
            fail_calls += 1
            if fail_calls == 1:
                return types.SimpleNamespace(returncode=1, stdout="", stderr="inline err")
            raise lib.subprocess.CalledProcessError(1, args[0], stderr="fallback err")
        lib.subprocess.run = _fallback_fail
        lib.sh = lambda *args, **kwargs: "abc123\n"
        check("inline 與 fallback 都失敗 → False",
              lib.post_review("o/r", 1, "s", [], [], False, "abc123") is False)

        moved_calls = []
        def _inline_fail(*args, **kwargs):
            moved_calls.append((args, kwargs))
            return types.SimpleNamespace(returncode=1, stdout="", stderr="inline err")
        lib.subprocess.run = _inline_fail
        lib.sh = lambda *args, **kwargs: "newsha\n"
        check("fallback 前 head 已移動 → False 且不貼一般留言",
              lib.post_review("o/r", 1, "s", [], [], False, "abc123") is False
              and len(moved_calls) == 1)

        def _nf(*a, **k):
            raise FileNotFoundError()
        lib.subprocess.run = _nf
        try:
            lib.post_review("o/r", 1, "s", [], [], False, "abc123")
            check("gh 不存在 → RuntimeError", False)
        except RuntimeError:
            check("gh 不存在 → RuntimeError", True)
    finally:
        lib.subprocess.run, lib.sh = orig_run, orig_sh


def test_post_review_timeout_no_duplicate():
    print("[post_review：inline POST 逾時＝不確定結果 → 硬中止、不重貼 fallback（避免重複，Codex R4）]")
    import types
    orig_run, orig_sh = lib.subprocess.run, lib.sh
    try:
        calls = []
        def _timeout_then_track(*args, **kwargs):
            calls.append(list(args[0][:3]))
            if len(calls) == 1:  # 第一次＝inline gh api → 逾時（不確定 GitHub 是否已建立 review）
                raise lib.subprocess.TimeoutExpired(cmd=args[0], timeout=1)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")  # 若誤走到 fallback 會被此記錄抓到
        lib.subprocess.run = _timeout_then_track
        lib.sh = lambda *a, **k: "abc123\n"
        # 攔例外並轉成可斷言的值（硬中止＝raise RuntimeError），不讓例外竄出崩掉腳本（崩潰不算有效紅綠）。
        try:
            result = lib.post_review("o/r", 1, "s", [], [], False, "abc123")
        except RuntimeError:
            result = "raised"
        except Exception as e:  # noqa: BLE001 — 非預期例外型別也轉成值，讓斷言明確失敗而非崩潰
            result = f"raised:{type(e).__name__}"
        check("inline POST 逾時 → 拋 RuntimeError（結果不確定、硬中止）", result == "raised", str(result))
        # 核心：逾時的 POST 可能已在伺服器端成功，故不可再貼 fallback 單則留言（否則重複回饋）
        check("逾時後不貼 fallback 單則留言（避免對已成功的非冪等寫入重複）",
              ["gh", "pr", "comment"] not in calls)
    finally:
        lib.subprocess.run, lib.sh = orig_run, orig_sh


def test_post_review_repo_guard():
    print("[post_review repo 格式守衛（Gemini #4）]")
    check("無斜線 repo → 回 False 不崩", lib.post_review("noslash", 1, "s", [], [], False, "abc") is False)
    check("多斜線 repo → 回 False 不崩", lib.post_review("a/b/c", 1, "s", [], [], False, "abc") is False)
    check("None repo → 回 False 不崩（型別防禦）", lib.post_review(None, 1, "s", [], [], False, "abc") is False)
    check("owner/（name 空）→ False", lib.post_review("owner/", 1, "s", [], [], False, "abc") is False)
    check("/repo（owner 空）→ False", lib.post_review("/repo", 1, "s", [], [], False, "abc") is False)
    check("缺 commit_id → False", lib.post_review("o/r", 1, "s", [], [], False, "") is False)


def test_registry_nested_nondict():
    print("[registry 巢狀 info/dist-tags 非 dict 防禦（Gemini round8）]")
    orig = lib._http_json
    try:
        pinfo = lib.parse_diff("diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
                               "+++ b/requirements.txt\n@@ -1,1 +1,2 @@\n requests==2.31.0\n+foo==9.9.9\n")["requirements.txt"]
        lib._http_json = lambda url: (("missing", None) if "/foo/9.9.9/json" in url else
                                      ("ok", {"releases": {"1.0.0": []}, "info": "not-a-dict"}))
        check("PyPI info 非 dict 仍不崩、有發 finding", any("foo" in f[3] for f in lib._check_pypi("requirements.txt", pinfo)))
        ninfo = lib.parse_diff('diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n'
                               '@@ -1,2 +1,3 @@\n   "dependencies": {\n+    "fakepkg": "9.9.9"\n   }\n')["package.json"]
        lib._http_json = lambda url: ("ok", {"versions": {"1.0.0": {}}, "dist-tags": "not-a-dict"})
        check("npm dist-tags 非 dict 仍不崩、有發 finding", any("fakepkg" in f[3] for f in lib._check_npm("package.json", ninfo)))
        lib._http_json = lambda url: (("missing", None) if "/foo/9.9.9/json" in url else
                                      ("ok", {"releases": "not-a-dict", "info": {}}))
        check("PyPI releases 非 dict 仍不崩", any("foo" in f[3] for f in lib._check_pypi("requirements.txt", pinfo)))
        arbitrary = lib.parse_diff("diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
                                   "+++ b/requirements.txt\n@@ -1,1 +1,2 @@\n requests==2.31.0\n+foo===9.9.9\n")["requirements.txt"]
        lib._http_json = lambda url: ("ok", {"releases": "not-a-dict", "info": {}})
        check("PyPI releases 非 dict + === → 略過、不誤報不存在",
              lib._check_pypi("requirements.txt", arbitrary) == [])
        lib._http_json = lambda url: ("ok", {"versions": "not-a-dict", "dist-tags": {}})
        check("npm versions 非 dict → 略過、不誤報不存在", lib._check_npm("package.json", ninfo) == [])
    finally:
        lib._http_json = orig


def test_reqs_subdir():
    print("[requirements 子目錄偵測（Gemini round15）]")
    called, orig = [], lib._check_pypi
    try:
        lib._check_pypi = lambda path, info: called.append(path) or []
        lib.dependency_findings({"requirements/prod.txt": {}, "deps/requirements/base.txt": {},
                                 "requirements-dev.txt": {}, "docs/notes.txt": {}})
        check("requirements/prod.txt 被當依賴檔", "requirements/prod.txt" in called)
        check("deps/requirements/base.txt 被當依賴檔", "deps/requirements/base.txt" in called)
        check("requirements-dev.txt 被當依賴檔", "requirements-dev.txt" in called)
        check("docs/notes.txt 不被當依賴檔", "docs/notes.txt" not in called)
    finally:
        lib._check_pypi = orig


def test_dedup_direct():
    print("[dedup 直接測：issue 前 40 字為 key、保留首見、保序（T3）]")
    a = ("f.py", 10, "high", "X" * 40 + "AAA", "")
    b = ("f.py", 10, "high", "X" * 40 + "BBB", "")  # 與 a 的 (path,line,issue[:40]) 相同 → 視為重複
    c = ("f.py", 11, "high", "另一條不同的問題", "")
    out = lib.dedup([a, b, c])
    check("前 40 字相同的兩條收斂成一條", sum(1 for t in out if t[1] == 10) == 1)
    check("保留首見（a，尾 AAA），非 b", any(t[1] == 10 and t[3].endswith("AAA") for t in out))
    check("不同 key（line 11）保留", any(t[1] == 11 for t in out))
    check("保序：[a, c]", out == [a, c], str(out))
    # 前 40 字內就不同 → 不可被誤收斂
    d = ("g.py", 5, "high", "alpha 問題敘述", "")
    e = ("g.py", 5, "high", "beta 問題敘述", "")
    check("前 40 字內即不同 → 兩條都保留", len(lib.dedup([d, e])) == 2)


def test_pypi_custom_index_skips_grounding():
    print("[requirements 有自訂索引（--index-url 等）→ 跳過 PyPI grounding，私有套件不誤報（Codex R5）]")
    orig = lib._http_json
    try:
        lib._http_json = lambda url: ("missing", None)  # 假裝 public PyPI 一律 404
        # 有 --index-url：整檔跳過 → privpkg 不被誤報
        with_idx = lib.parse_diff("diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
                                  "+++ b/requirements.txt\n@@ -1,1 +1,3 @@\n requests==2.31.0\n"
                                  "+--index-url https://private.example/simple\n+privpkg==1.0.0\n")["requirements.txt"]
        check("含 --index-url → 跳過 grounding、privpkg 不誤報",
              lib._check_pypi("requirements.txt", with_idx) == [])
        # 指令在 context 行（既存 --index-url、本 PR 只在其下加套件）也要偵測到 → 跳過
        ctx_idx = lib.parse_diff("diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
                                 "+++ b/requirements.txt\n@@ -1,2 +1,3 @@\n --index-url https://private.example/simple\n"
                                 " requests==2.31.0\n+privpkg2==1.0.0\n")["requirements.txt"]
        check("--index-url 在 context 行（既存指令）也偵測到 → 跳過",
              lib._check_pypi("requirements.txt", ctx_idx) == [])
        # 對照：沒有索引指令時，同樣 404 的 nopkg 仍要被抓（確認跳過是因指令、非因空迴圈）
        no_idx = lib.parse_diff("diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
                                "+++ b/requirements.txt\n@@ -1,1 +1,2 @@\n requests==2.31.0\n+nopkg==1.0.0\n")["requirements.txt"]
        check("無索引指令 → 404 的 nopkg 照常抓 high（對照組）",
              any("nopkg" in f[3] for f in lib._check_pypi("requirements.txt", no_idx)))
    finally:
        lib._http_json = orig


def test_estimate_tokens_cjk_truncation():
    print("[跨語言 token 估計：CJK-heavy diff 不因字元數低估而漏截（Codex R5）]")
    # 純 ASCII：估計等同舊的 len/CHARS_PER_TOKEN，行為不變
    ascii_txt = "x" * 400
    check("ASCII：400 字 ≈ 100 token（等同舊估計）", abs(lib._estimate_tokens(ascii_txt) - 100) < 1e-9)
    # CJK：每字約 1 token，遠高於 len/4
    cjk_txt = "中" * 100
    check("CJK：100 字估計 ≈ 100 token（非 25）", lib._estimate_tokens(cjk_txt) == 100)
    # 關鍵回歸（刻意選在「舊 len//4 估計會判為未超限、新 CJK 估計會判為超限」的區間，
    # 讓此整合斷言真的能區分修好與否）：6 檔 × 40 CJK 字，總長 ~414 字。
    #   舊 len//4 ≈ 103 token ≤ 200 → 不會截（漏截）；新估計 ≈ 284 token > 200 → 正確截斷。
    big_cjk = "".join(f"diff --git a/f{i}.py b/f{i}.py\n+{'測試中文' * 10}\n" for i in range(6))
    check("對照：舊 len//CHARS_PER_TOKEN 會低估此 diff 為未超限（證明測試有鑑別力）",
          len(big_cjk) / lib.CHARS_PER_TOKEN <= 200 < lib._estimate_tokens(big_cjk))
    d, trunc = lib.truncate_diff(big_cjk, 200)
    check("CJK-heavy 超限 diff 被截斷（trunc=True；舊估計會漏截）", trunc,
          f"len//4={len(big_cjk)//4} est={lib._estimate_tokens(big_cjk):.0f}")
    check("截斷輸出含 TRUNCATION_MARKER", lib.TRUNCATION_MARKER in d)
    # 同尺寸但純 ASCII 且在上限內 → 不截（證明差異來自語言估計、非單純長度）
    small_ascii = "diff --git a/a.py b/a.py\n+" + "x" * 100 + "\n"
    check("小 ASCII diff 在上限內 → 不截", lib.truncate_diff(small_ascii, 200)[1] is False)


def test_registry_error_not_missing():
    print("[registry 查詢錯誤（暫時性）≠ 404：不可誤報套件/版號不存在（T2）]")
    pinfo = lib.parse_diff("diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
                           "+++ b/requirements.txt\n@@ -1,1 +1,2 @@\n requests==2.31.0\n+flaky==9.9.9\n")["requirements.txt"]
    ninfo = lib.parse_diff('diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n'
                           '@@ -1,2 +1,3 @@\n   "dependencies": {\n+    "flakypkg": "9.9.9"\n   }\n')["package.json"]
    orig = lib._http_json
    try:
        lib._http_json = lambda url: ("error", "timeout")
        check("PyPI 套件查詢 error → 不報（暫時性失敗非 404）", lib._check_pypi("requirements.txt", pinfo) == [])
        check("npm 套件查詢 error → 不報（暫時性失敗非 404）", lib._check_npm("package.json", ninfo) == [])

        def pkg_ok_ver_error(url):
            # 套件存在（package endpoint 200），但版號 endpoint 逾時 → error 分支必須略過、不誤報版號不存在
            if url.endswith("/flaky/json"):
                return "ok", {"releases": {"1.0.0": []}, "info": {"version": "1.0.0"}}
            return "error", "timeout"
        lib._http_json = pkg_ok_ver_error
        check("PyPI 版號查詢 error → 略過該版號（不誤報不存在）", lib._check_pypi("requirements.txt", pinfo) == [])
    finally:
        lib._http_json = orig


def main():
    global _test_tmpdir
    with tempfile.TemporaryDirectory(prefix="ggr-tests-") as _test_tmpdir:
        test_parse_diff()
        test_anchor()
        test_dep_extract()
        test_normalize()
        test_truncate()
        test_lenient_json()
        test_sh_raises()
        test_parse_diff_edges()
        test_npm_spec_regexes()
        test_npm_build_metadata_equivalence()
        test_npm_del_brace()
        test_npm_scoped_skip()
        test_post_review_return()
        test_post_review_timeout_no_duplicate()
        test_post_review_repo_guard()
        test_pypi_version_norm()
        test_registry_nondict()
        test_registry_nested_nondict()
        test_reqs_subdir()
        test_type_defense_round16()
        test_tools_load_findings()
        test_tools_fetch_diff()
        test_tools_pr_head_guards()
        test_pypi_emit()
        test_truncation_roundtrip()
        test_truncated_flag_persisted_not_sniffed()
        test_dedup_direct()
        test_pypi_custom_index_skips_grounding()
        test_estimate_tokens_cjk_truncation()
        test_registry_error_not_missing()
        if "--net" in sys.argv:
            test_registry_net()
        else:
            print("[registry grounding] 跳過（加 --net 對真實 PyPI/npm 測試）")
        print("\n" + ("全部通過 ✅" if not _FAILED else f"失敗 {len(_FAILED)} 項 ❌: {_FAILED}"))
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
