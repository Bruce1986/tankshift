#!/usr/bin/env python3
"""review_lib.py 確定性零件的離線單元測試（不需 GEMINI_API_KEY、不需網路）。
涵蓋：diff 解析、行錨定、依賴擷取、嚴重度校準、檔界截斷、JSON 容錯。

執行: python3 test_review_lib.py            # 加 --net 才打真實 PyPI/npm
"""
import os
import sys

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
    # PEP 508 直接引用（含 @）要被排除（Gemini round4）
    at_reqs = lib._added_python_reqs(lib.parse_diff(
        'diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n+++ b/requirements.txt\n'
        '@@ -1,1 +1,2 @@\n requests==2.31.0\n+mypkg @ file:///local/mypkg\n')["requirements.txt"])
    check("PEP 508 直接引用（@）被排除", "mypkg" not in {n for (_, n, _, _) in at_reqs})


def test_normalize():
    print("[嚴重度校準]")
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
    check("_added_npm_deps del text=None 不崩", lib._added_npm_deps({"events": [("del", 1, None)]}) == [])


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
    check("NPM_EXACT_RE 不吃 range '1.2.3 || >=2'", lib.NPM_EXACT_RE.match("1.2.3 || >=2") is None)
    check("NPM_EXACT_RE 不吃帶後綴 1.2.3-2.0.0", lib.NPM_EXACT_RE.match("1.2.3-2.0.0") is None)
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


_created_files = []


def _write(name, content):
    import tempfile
    try:
        d = tempfile.gettempdir()
    except FileNotFoundError:  # 受限沙箱可能無可用 temp 目錄 → 退到測試檔所在目錄
        d = os.path.dirname(os.path.abspath(__file__))
    p = os.path.join(d, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    _created_files.append(p)  # 供 main() 收尾清理
    return p


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
    r = tools._load_findings(scal)
    check("list 含非 dict → 只留合法那條", len(r) == 1 and r[0][0] == "f")
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
    tools.sh = lambda cmd, check=True: "DIFF"  # 不實際呼叫 gh/git，只驗合法組合通過驗證
    try:
        check("base-only 通過驗證", tools._fetch_diff(None, None, "main") == "DIFF")
        check("pr+repo 通過驗證", tools._fetch_diff("o/r", "5", None) == "DIFF")
    finally:
        tools.sh = _osh


def test_pypi_version_norm():
    print("[PyPI 版號 PEP440 正規化（收回 #7）]")
    check("1.0 ≡ 1.0.0", lib._norm_ver("1.0") == lib._norm_ver("1.0.0"))
    check("1.10 ≠ 1.1（不誤縮）", lib._norm_ver("1.10") != lib._norm_ver("1.1"))
    check("1.0rc1 不被去零吃掉", lib._norm_ver("1.0rc1") != lib._norm_ver("1.0"))
    rel = {"1.0.0": [], "2.3": []}
    check("pin 1.0 命中 1.0.0（等價、不誤報）", lib._version_present("1.0", rel))
    check("pin 2.3.0 命中 2.3", lib._version_present("2.3.0", rel))
    check("pin 9.9.9 確實不存在", not lib._version_present("9.9.9", rel))


def test_pypi_emit():
    print("[_check_pypi 正規化比對：等價 pin 不誤報、真缺要抓（收回 #7）]")
    diff = ("diff --git a/requirements.txt b/requirements.txt\n"
            "--- a/requirements.txt\n+++ b/requirements.txt\n"
            "@@ -1,1 +1,4 @@\n requests==2.31.0\n+baz==2.31.0\n+foo==1.0\n+bar==9.9.9\n")
    info = lib.parse_diff(diff)["requirements.txt"]
    orig = lib._http_json
    lib._http_json = lambda url: ("ok", {"releases": {"1.0.0": [], "2.31.0": []},
                                         "info": {"version": "2.31.0"}})
    try:
        findings = lib._check_pypi("requirements.txt", info)
        flagged = " ".join(f[3] for f in findings)
        check("baz==2.31.0 精確命中 → 不報", "baz" not in flagged)
        check("foo==1.0 ≡ 已發 1.0.0（PEP440）→ 不誤報", "foo" not in flagged)
        check("bar==9.9.9 不存在 → 抓 high", "bar" in flagged)
        check("剛好一條（bar）", len(findings) == 1 and findings[0][2] == "high")
    finally:
        lib._http_json = orig


def test_pypi_nonliteral_pin():
    print("[_check_pypi：wildcard pin 與 PEP440 local version 改驗『基底版號』]")
    # 單元層：_version_pin_present 三分支
    rel = {"2.1.0": [], "4.2.7": [], "2.31.0": []}
    check("4.2.* 前綴有 release → True", lib._version_pin_present("4.2.*", rel) is True)
    check("99.99.* 前綴無 release → False（幻覺版號要抓）", lib._version_pin_present("99.99.*", rel) is False)
    check("2.1.0+cu118 基底存在 → True", lib._version_pin_present("2.1.0+cu118", rel) is True)
    check("88.7.6+build 基底不存在 → False", lib._version_pin_present("88.7.6+build", rel) is False)
    check("非 trailing wildcard（1.*.3）→ None 不判定", lib._version_pin_present("1.*.3", rel) is None)
    # wildcard 前綴精確度（PEP 440 逐段比對）——兩側都要釘，否則「帶不帶那個點」沒人守
    check("4.2.* 不吃 4.20.1（過寬側）", lib._version_pin_present("4.2.*", {"4.20.1": []}) is False)
    check("4.2.* 命中裸版號 4.2（過嚴側；pytz 這類只發裸版號）",
          lib._version_pin_present("4.2.*", {"4.2": []}) is True)
    check("4.* 命中裸版號 4", lib._version_pin_present("4.*", {"4": []}) is True)
    check("releases key 非字串不崩", lib._version_pin_present("4.2.*", {4: []}) is False)
    # pre/post/dev 修飾字與 release segment 之間沒有點，仍屬 ==X.Y.* 的涵蓋範圍
    # （期望值取自 packaging.specifiers.SpecifierSet，210 組矩陣對照零誤差；此處寫死以免測試相依外部套件）
    check("4.2.* 命中 4.2rc1（修飾字前無點）", lib._version_pin_present("4.2.*", {"4.2rc1": []}) is True)
    check("2.0.0.* 命中 2.0.0rc1", lib._version_pin_present("2.0.0.*", {"2.0.0rc1": []}) is True)
    check("4.2.* 命中 4.2.post1", lib._version_pin_present("4.2.*", {"4.2.post1": []}) is True)
    check("4.2.* 仍不吃 4.20.1（緊接數字＝不同段）",
          lib._version_pin_present("4.2.*", {"4.20.1": []}) is False)
    # base 比 release 長的情形只有 _version_present 的 PEP440 正規化接得住（前綴比對接不到）
    check("2.0.0.* 命中較短的 release 2.0（靠正規化）",
          lib._version_pin_present("2.0.0.*", {"2.0": []}) is True)

    # 端到端：誤報側與漏報側同時驗
    diff = ("diff --git a/requirements.txt b/requirements.txt\n"
            "--- a/requirements.txt\n+++ b/requirements.txt\n"
            "@@ -1,1 +1,6 @@\n requests==2.31.0\n"
            "+pandas==4.2.*\n+torch==2.1.0+cu118\n"
            "+fakewild==99.99.*\n+fakelocal==88.7.6+madeup\n+bar==9.9.9\n")
    info = lib.parse_diff(diff)["requirements.txt"]
    orig = lib._http_json
    lib._http_json = lambda url: ("ok", {"releases": dict(rel), "info": {"version": "2.31.0"}})
    try:
        findings = lib._check_pypi("requirements.txt", info)
        flagged = " ".join(f[3] for f in findings)
        # 誤報側：真實存在的基底不該被報
        check("pandas==4.2.* → 不誤報", "pandas" not in flagged)
        check("torch==2.1.0+cu118 → 不誤報", "torch" not in flagged)
        # 漏報側：基底本身是幻覺就要抓（整條放行會架空這個檢查）
        check("fakewild==99.99.* → 要抓", "fakewild" in flagged)
        check("fakelocal==88.7.6+madeup → 要抓", "fakelocal" in flagged)
        check("bar==9.9.9 → 要抓", "bar" in flagged)
        check("剛好三條", len(findings) == 3)
    finally:
        lib._http_json = orig


def test_truncation_roundtrip():
    print("[截斷標記跨模組往返（B5）]")
    big = "\n".join(f"diff --git a/f{i} b/f{i}\n+x" for i in range(50))
    out, trunc = lib.truncate_diff(big, 1)  # cap 極小 → 必截
    check("truncate_diff 回 truncated=True", trunc)
    check("輸出含共用常數 TRUNCATION_MARKER", lib.TRUNCATION_MARKER in out)
    check("review_tools 用同一 TRUNCATION_MARKER（cmd_post 偵測得到）",
          tools.TRUNCATION_MARKER == lib.TRUNCATION_MARKER and lib.TRUNCATION_MARKER in out)


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
        lib.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr="")
        check("inline 成功 → True", lib.post_review("o/r", 1, "s", [], [], False) is True)
        lib.subprocess.run = lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="err")
        lib.sh = lambda *a, **k: ""
        check("inline 失敗但 fallback 成功 → True", lib.post_review("o/r", 1, "s", [], [], False) is True)

        def _nf(*a, **k):
            raise FileNotFoundError()
        lib.subprocess.run = _nf
        try:
            lib.post_review("o/r", 1, "s", [], [], False)
            check("gh 不存在 → RuntimeError", False)
        except RuntimeError:
            check("gh 不存在 → RuntimeError", True)
    finally:
        lib.subprocess.run, lib.sh = orig_run, orig_sh


def test_post_review_repo_guard():
    print("[post_review repo 格式守衛（Gemini #4）]")
    check("無斜線 repo → 回 False 不崩", lib.post_review("noslash", 1, "s", [], [], False) is False)
    check("多斜線 repo → 回 False 不崩", lib.post_review("a/b/c", 1, "s", [], [], False) is False)
    check("None repo → 回 False 不崩（型別防禦）", lib.post_review(None, 1, "s", [], [], False) is False)
    check("owner/（name 空）→ False", lib.post_review("owner/", 1, "s", [], [], False) is False)
    check("/repo（owner 空）→ False", lib.post_review("/repo", 1, "s", [], [], False) is False)


def test_registry_nested_nondict():
    print("[registry 巢狀 info/dist-tags 非 dict 防禦（Gemini round8）]")
    orig = lib._http_json
    try:
        pinfo = lib.parse_diff("diff --git a/requirements.txt b/requirements.txt\n--- a/requirements.txt\n"
                               "+++ b/requirements.txt\n@@ -1,1 +1,2 @@\n requests==2.31.0\n+foo==9.9.9\n")["requirements.txt"]
        lib._http_json = lambda url: ("ok", {"releases": {"1.0.0": []}, "info": "not-a-dict"})
        check("PyPI info 非 dict 仍不崩、有發 finding", any("foo" in f[3] for f in lib._check_pypi("requirements.txt", pinfo)))
        ninfo = lib.parse_diff('diff --git a/package.json b/package.json\n--- a/package.json\n+++ b/package.json\n'
                               '@@ -1,2 +1,3 @@\n   "dependencies": {\n+    "fakepkg": "9.9.9"\n   }\n')["package.json"]
        lib._http_json = lambda url: ("ok", {"versions": {"1.0.0": {}}, "dist-tags": "not-a-dict"})
        check("npm dist-tags 非 dict 仍不崩、有發 finding", any("fakepkg" in f[3] for f in lib._check_npm("package.json", ninfo)))
        lib._http_json = lambda url: ("ok", {"releases": "not-a-dict", "info": {}})
        check("PyPI releases 非 dict 仍不崩", any("foo" in f[3] for f in lib._check_pypi("requirements.txt", pinfo)))
        lib._http_json = lambda url: ("ok", {"versions": "not-a-dict", "dist-tags": {}})
        check("npm versions 非 dict 仍不崩", any("fakepkg" in f[3] for f in lib._check_npm("package.json", ninfo)))
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


def main():
    try:
        test_parse_diff()
        test_anchor()
        test_dep_extract()
        test_normalize()
        test_truncate()
        test_lenient_json()
        test_sh_raises()
        test_parse_diff_edges()
        test_npm_spec_regexes()
        test_npm_del_brace()
        test_npm_scoped_skip()
        test_post_review_return()
        test_post_review_repo_guard()
        test_pypi_version_norm()
        test_registry_nondict()
        test_registry_nested_nondict()
        test_reqs_subdir()
        test_type_defense_round16()
        test_tools_load_findings()
        test_tools_fetch_diff()
        test_pypi_emit()
        test_pypi_nonliteral_pin()
        test_truncation_roundtrip()
        if "--net" in sys.argv:
            test_registry_net()
        else:
            print("[registry grounding] 跳過（加 --net 對真實 PyPI/npm 測試）")
        print("\n" + ("全部通過 ✅" if not _FAILED else f"失敗 {len(_FAILED)} 項 ❌: {_FAILED}"))
    finally:
        for p in _created_files:  # 清掉測試臨時檔，不污染工作區
            try:
                os.remove(p)
            except OSError:
                pass
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
