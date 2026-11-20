# gemini-grade-review (bundled skill)

A **Claude-native**, severity-calibrated, Gemini-Code-Assist-grade detailed code
review skill, bundled into this repo so any project from this template can review
itself. See `SKILL.md` for the procedure.

- **Engine:** Claude (+subagents). No Gemini API key needed.
- **Deterministic core** (no LLM): `review_lib.py` + `review_tools.py` handle diff
  parsing, supply-chain registry grounding (PyPI/npm), line anchoring, and posting.
- **Path:** the skill references its scripts repo-relatively
  (`$(git rev-parse --show-toplevel)/.claude/skills/gemini-grade-review`), so it
  works wherever the repo is cloned.
- **Tests:** `python3 test_review_lib.py` (offline) / `--net` to hit real registries.

Invoke via `/gemini-grade-review [PR#]`, or just ask Claude for a "詳細 / 深度 /
Gemini 級 review". Reports by default; posts inline only on explicit request.
