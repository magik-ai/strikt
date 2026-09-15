"""No long dash survives anywhere: not in the repo, not in anything the bot sends (CLAUDE.md law 5).

The owner banned the em dash, the en dash, the horizontal bar and the minus sign across the whole
project. Two defences, both tested here: the build fails when one is committed, and
``plain_dashes`` rewrites every outgoing message, so a model that writes one anyway cannot deliver
it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from strikt.telegram.messenger import FakeMessenger
from strikt.telegram.render import LONG_DASHES, plain_dashes

ROOT = Path(__file__).resolve().parent.parent
SKIP_SUFFIX = {".lock", ".png", ".jpg", ".jpeg", ".ico", ".svg", ".webp", ".pdf"}
#: The one place a long dash is allowed: a regex that matches what a *user* may paste.
ALLOWED = {"src/strikt/onboarding/importer.py"}


def tracked_files() -> list[Path]:
    # -z: a path with a space or a non-ASCII name must not be silently skipped by the guard
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split("\0")
    return [ROOT / name for name in out if name and name not in ALLOWED]


def test_no_long_dash_is_committed() -> None:
    offenders: list[str] = []
    for path in tracked_files():
        if path.suffix.lower() in SKIP_SUFFIX or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if any(dash in line for dash in LONG_DASHES):
                offenders.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()[:80]}")
    assert not offenders, "long dashes found (write a hyphen with spaces):\n" + "\n".join(
        offenders[:20]
    )


EM, EN, MINUS = "\u2014", "\u2013", "\u2212"


def test_plain_dashes_rewrites_every_shape() -> None:
    assert plain_dashes(f"слово {EM} слово") == "слово - слово"
    assert plain_dashes(f"a{EM}b") == "a - b"
    assert plain_dashes(f"20{EN}40 %") == "20-40 %"  # a range stays tight
    assert plain_dashes(f"минус {MINUS}5 кг") == "минус -5 кг"  # a sign stays on its number
    assert plain_dashes(f"Итого {EM} 1900 ккал") == "Итого - 1900 ккал"  # prose, not a sign
    assert plain_dashes(f"План:\n{EM} яйца\n{EM} рис") == "План:\n- яйца\n- рис"
    assert plain_dashes(f"{EM} в начале") == "- в начале"
    assert plain_dashes(f"План:\n  {EM} курица") == "План:\n  - курица"  # indentation survives
    assert plain_dashes("no dash here") == "no dash here"


async def test_nothing_reaches_telegram_with_a_long_dash() -> None:
    messenger = FakeMessenger()
    message_id = await messenger.send(1, f"Баскетбол {EM} мощно. Отбой {EM} пораньше.")
    assert messenger.sent[0].text == "Баскетбол - мощно. Отбой - пораньше."

    await messenger.edit(1, message_id, f"Итого 1 900 {EM} день закрыт")
    assert messenger.edits[0][2] == "Итого 1 900 - день закрыт"
