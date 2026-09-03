"""Concatenate ``src/strikt/agent/prompts/*.md`` into ``PROMPTS.md`` (the brief's deliverable).

Run ``make prompts`` (or ``uv run python scripts/build_prompts_md.py``). ``tests/test_prompts.py``
asserts that the committed PROMPTS.md is in sync.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "src" / "strikt" / "agent" / "prompts"
OUTPUT = ROOT / "PROMPTS.md"
ORDER = ("coach", "onboarding", "proactive", "verify", "summarize", "import")

HEADER = """# Strikt — prompts

Generated from `src/strikt/agent/prompts/*.md` by `scripts/build_prompts_md.py`. Do not edit
this file by hand; edit the source prompt and run `make prompts`.

How the prompts are used (PLAN §6):

- **coach.md** is `system[0]`, cached for one hour; it never contains user-specific text.
- The profile block (`system[1]`: profile, active protocol, active notes) is rendered by code;
  while onboarding is unfinished **onboarding.md** is appended to it with the checklist state.
- **proactive.md** drives `proactive_decide` (structured output `{send, text}`, effort low).
- **verify.md** is the Reflexion re-check when the draft reply's numbers disagree with the DB.
- **summarize.md** writes day and week summaries (text + data JSON).
- **import.md** tells the model the row shapes for `import_history`.
"""


def build() -> str:
    parts = [HEADER]
    for name in ORDER:
        path = PROMPTS_DIR / f"{name}.md"
        body = path.read_text(encoding="utf-8").rstrip() + "\n"
        parts.append(f"\n---\n\n<!-- source: src/strikt/agent/prompts/{name}.md -->\n\n{body}")
    return "".join(parts)


def main(argv: list[str]) -> int:
    content = build()
    if "--check" in argv:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != content:
            print("PROMPTS.md is out of date; run `make prompts`", file=sys.stderr)
            return 1
        print("PROMPTS.md is up to date")
        return 0
    OUTPUT.write_text(content, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({len(content)} chars)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
