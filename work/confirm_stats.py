"""Stage 3 acceptance: how well did real speech get through confirmation?

Reads data/agent.db, which only holds what was heard while VOICE_DEMO_CONFIRM=1 was on.
"""
from collections import Counter
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from memory_store import ActionLog  # noqa: E402


def main():
    sys.stdout.reconfigure(errors="replace")
    rows = ActionLog(ROOT / "data" / "agent.db").recent(limit=1000)
    heard = [row for row in rows if row["tool"] == "confirm_heard"]
    print("What each utterance did:", dict(Counter(row["outcome"] for row in heard)))
    for (outcome, text), count in Counter((row["outcome"], row["detail"]) for row in heard).most_common():
        print(f"{count:>4}  {outcome:<10} {text!r}")
    print("Tool outcomes:", dict(Counter(row["outcome"] for row in rows if row["tool"] == "demo_confirm")))


if __name__ == "__main__":
    main()
