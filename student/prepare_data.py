"""Turn the teacher-labeled examples into training files.

Reads   dataset/teacher_labeled.jsonl   (158 examples labeled by the teacher)
Writes  dataset/student_train.jsonl     (about 90% of them, for training)
        dataset/student_val.jsonl       (about 10%, to watch training)

Run from the project folder:
    py -3.13 -m student.prepare_data
"""
import json
import random
from collections import Counter
from pathlib import Path

from student.format import SYSTEM_PROMPT, build_answer, build_user_message

SOURCE = Path("dataset/teacher_labeled.jsonl")
TRAIN_OUT = Path("dataset/student_train.jsonl")
VAL_OUT = Path("dataset/student_val.jsonl")
VAL_SHARE = 0.10
SEED = 42  # fixed, so the split is the same every time
DENY_REPEAT = 4  # deny examples are rare, so training sees each one 4 times


def to_chat(example: dict) -> dict:
    return {
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(example)},
            {
                "role": "assistant",
                "content": build_answer(example["teacher_action"], example.get("teacher_reason", "")),
            },
        ]
    }


def main() -> None:
    rows = [json.loads(line) for line in SOURCE.read_text(encoding="utf-8").splitlines() if line.strip()]
    usable = [r for r in rows if r.get("teacher_action") in ("allow", "deny")]
    skipped = len(rows) - len(usable)

        # Split allow and deny separately, so both kinds end up in the val file.
    rng = random.Random(SEED)
    train, val = [], []
    for action in ("allow", "deny"):
        group = [r for r in usable if r["teacher_action"] == action]
        rng.shuffle(group)
        n_val = max(1, round(len(group) * VAL_SHARE))
        val += group[:n_val]
        train += group[n_val:] * (DENY_REPEAT if action == "deny" else 1)
    rng.shuffle(train)
    rng.shuffle(val)

    for path, part in ((TRAIN_OUT, train), (VAL_OUT, val)):
        with path.open("w", encoding="utf-8") as f:
            for r in part:
                f.write(json.dumps(to_chat(r), ensure_ascii=False) + "\n")

    print(f"read {len(rows)} examples, skipped {skipped} without allow/deny")
    print(f"teacher actions: {dict(Counter(r['teacher_action'] for r in usable))}")
    print(f"wrote {len(train)} to {TRAIN_OUT} {dict(Counter(r['teacher_action'] for r in train))}")
    print(f"wrote {len(val)} to {VAL_OUT} {dict(Counter(r['teacher_action'] for r in val))}")


if __name__ == "__main__":
    main()