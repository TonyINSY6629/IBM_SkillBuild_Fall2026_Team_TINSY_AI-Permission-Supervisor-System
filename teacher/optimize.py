"""
teacher/optimize.py — DSPy Teacher Prompt Optimization + Bulk Labeling

Two modes:
  1. optimize  — Run BootstrapFewShot on a sample of training_set.jsonl to
                 optimize the few-shot prompt. Saves the compiled program to
                 teacher/compiled_program.json.

  2. label     — Run the compiled (or zero-shot) teacher on all unlabeled
                 training examples. Writes teacher labels to
                 dataset/teacher_labeled.jsonl.

Usage:
  # Optimize the prompt (run once, needs ~20–50 labeled examples):
  python teacher/optimize.py optimize

  # Bulk label all training examples:
  python teacher/optimize.py label

  # Validate on held-out test set (prints accuracy):
  python teacher/optimize.py validate

Environment variables (from .env):
  WATSONX_API_KEY, WATSONX_PROJECT_ID, WATSONX_URL
  TRAINING_SET_PATH   — default: dataset/training_set.jsonl
  HELD_OUT_PATH       — default: dataset/held_out_test_set.jsonl
  COMPILED_PROGRAM    — default: teacher/compiled_program.json
  OPTIMIZE_SAMPLE_N   — number of examples to use for optimization, default: 50
  LABEL_BATCH_SIZE    — examples per API batch, default: 10
"""

from __future__ import annotations

import json
import os
import random  # Fix 6: needed to shuffle the training examples
import sys
import time
from pathlib import Path

# Load .env before importing teacher modules that read env vars.
try:
    from dotenv import load_dotenv
    load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env", override=False)
except ImportError:
    pass

import dspy

# Fix 7: "python teacher/optimize.py" crashed with "No module named 'teacher'".
# Python only adds the teacher/ folder to the path, not the project folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from teacher.dspy_config import configure_dspy, TEACHER_MODEL_ID
from teacher.policy_excerpt import build_excerpt, load_policy
from teacher.signature import PermissionDecision, PermissionDecisionCoT

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent
TRAINING_SET_PATH = Path(os.environ.get("TRAINING_SET_PATH",
                                        str(PROJECT_ROOT / "dataset" / "training_set.jsonl")))
HELD_OUT_PATH = Path(os.environ.get("HELD_OUT_PATH",
                                    str(PROJECT_ROOT / "dataset" / "held_out_test_set.jsonl")))
COMPILED_PROGRAM_PATH = Path(os.environ.get("COMPILED_PROGRAM",
                                            str(PROJECT_ROOT / "teacher" / "compiled_program.json")))
TEACHER_LABELED_PATH = PROJECT_ROOT / "dataset" / "teacher_labeled.jsonl"

OPTIMIZE_SAMPLE_N: int = int(os.environ.get("OPTIMIZE_SAMPLE_N", "50"))
LABEL_BATCH_SIZE: int = int(os.environ.get("LABEL_BATCH_SIZE", "10"))

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_policy: dict | None = None


def _get_policy() -> dict:
    global _policy
    if _policy is None:
        _policy = load_policy()
    return _policy


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def _example_to_dspy(ex: dict) -> dspy.Example:
    """Convert one training example dict to a DSPy Example."""
    policy = _get_policy()
    category = ex.get("category", "unclassified")
    excerpt = build_excerpt(category=category, policy=policy)
    tool_input_raw = ex.get("tool_input", {})
    tool_input_str = json.dumps(tool_input_raw, ensure_ascii=False) if isinstance(tool_input_raw, dict) else str(tool_input_raw)

    return dspy.Example(
        tool_name=ex.get("tool_name", ""),
        tool_input=tool_input_str,
        category=category,
        action_type=str(ex.get("action_type", "OTHER")),
        inside_workspace=str(ex.get("inside_workspace", False)).lower(),
        touches_secrets=str(ex.get("touches_secrets", False)).lower(),
        prompt_text=ex.get("prompt_text") or "",
        policy_excerpt=excerpt,
        # Ground-truth labels from human answers (used as expected outputs):
        label=str(ex.get("label", "")),
        action=_map_action(ex.get("human_decision", "")),
    ).with_inputs(
        "tool_name", "tool_input", "category", "action_type",
        "inside_workspace", "touches_secrets", "prompt_text", "policy_excerpt",
    )


def _map_action(human_decision: str) -> str:
    """Map human_decision (allow/deny) to the teacher action vocabulary."""
    if human_decision == "allow":
        return "allow"
    if human_decision == "deny":
        return "deny"
    return "escalate"  # unknown → treat as escalate for training


def _action_metric(example: dspy.Example, prediction, trace=None) -> float:
    """
    1.0 if the teacher's allow/deny matches the human's answer, else 0.0.

    Fix 4: score allow/deny only. The old version gave 0.5 for matching the
    label, but every gold label is "Warning" from the rule engine, so that
    gave meaningless points. Baseline (always predict allow) ≈ 77% on this
    data — the teacher must beat that.
    """
    pred_action = getattr(prediction, "action", "").strip().lower()
    return 1.0 if pred_action == example.action.strip().lower() else 0.0


def _shuffled_training() -> list[dict]:
    """
    Training examples in a fixed random order (same order every run).

    Fix 6: use a shuffled sample instead of the first 50 in time order.
    The first 50 were mostly early shell commands. Seed 42 ensures optimize
    and validate never overlap.
    """
    examples = _load_jsonl(TRAINING_SET_PATH)
    random.Random(42).shuffle(examples)
    return examples


# ---------------------------------------------------------------------------
# Mode: optimize
# ---------------------------------------------------------------------------

def cmd_optimize() -> None:
    print(f"Configuring DSPy with model: {TEACHER_MODEL_ID}")
    configure_dspy()

    examples = _shuffled_training()  # Fix 6: shuffled instead of time order
    if len(examples) < 10:
        print(f"ERROR: only {len(examples)} training examples found at {TRAINING_SET_PATH}.")
        print("Run scripts/export_to_dataset.py first to generate the training set.")
        sys.exit(1)

    sample = examples[:OPTIMIZE_SAMPLE_N]
    print(f"Optimizing on {len(sample)} training examples (of {len(examples)} total).")

    dspy_examples = [_example_to_dspy(ex) for ex in sample]

    # Split: 80% train, 20% dev for the optimizer
    n_train = max(1, int(len(dspy_examples) * 0.8))
    train_set = dspy_examples[:n_train]
    dev_set = dspy_examples[n_train:]

    program = dspy.ChainOfThought(PermissionDecisionCoT)

    optimizer = dspy.BootstrapFewShot(
        metric=_action_metric,
        max_bootstrapped_demos=4,
        max_labeled_demos=4,
    )

    print("Running BootstrapFewShot optimization...")
    # Fix 2: removed valset=dev_set — BootstrapFewShot has no valset parameter
    # and crashed with it. Dev set is still used for the evaluation below.
    compiled = optimizer.compile(program, trainset=train_set)

    COMPILED_PROGRAM_PATH.parent.mkdir(parents=True, exist_ok=True)
    compiled.save(str(COMPILED_PROGRAM_PATH))
    print(f"Compiled program saved to: {COMPILED_PROGRAM_PATH}")

    # Quick dev-set accuracy report
    print("\nDev-set evaluation:")
    _evaluate(compiled, dev_set)


# ---------------------------------------------------------------------------
# Mode: label
# ---------------------------------------------------------------------------

def cmd_label() -> None:
    print(f"Configuring DSPy with model: {TEACHER_MODEL_ID}")
    configure_dspy()

    # Load compiled program if available, otherwise zero-shot.
    if COMPILED_PROGRAM_PATH.exists():
        program = dspy.ChainOfThought(PermissionDecisionCoT)
        program.load(str(COMPILED_PROGRAM_PATH))
        print(f"Loaded compiled program from: {COMPILED_PROGRAM_PATH}")
    else:
        program = dspy.ChainOfThought(PermissionDecisionCoT)
        print("WARNING: No compiled program found — running zero-shot. "
              "Run 'optimize' first for better quality.")

    examples = _load_jsonl(TRAINING_SET_PATH)
    if not examples:
        print(f"ERROR: No training examples found at {TRAINING_SET_PATH}.")
        sys.exit(1)

    # Fix 8: resume — skip examples already in teacher_labeled.jsonl so a
    # crash halfway does not spend tokens on them again (Lite ≈ 300k tokens/month).
    done_ids = {r.get("tool_use_id") for r in _load_jsonl(TEACHER_LABELED_PATH)}
    examples = [ex for ex in examples if ex.get("tool_use_id") not in done_ids]
    print(f"{len(done_ids)} already labeled, {len(examples)} left to label.")

    print(f"Labeling {len(examples)} training examples in batches of {LABEL_BATCH_SIZE}...")

    TEACHER_LABELED_PATH.parent.mkdir(parents=True, exist_ok=True)
    labeled_count = 0
    error_count = 0

    # Fix 8: "a" = append instead of "w" which wiped the file on each run.
    with TEACHER_LABELED_PATH.open("a", encoding="utf-8") as out:
        for i in range(0, len(examples), LABEL_BATCH_SIZE):
            batch = examples[i:i + LABEL_BATCH_SIZE]
            for ex in batch:
                dspy_ex = _example_to_dspy(ex)
                try:
                    pred = program(
                        tool_name=dspy_ex.tool_name,
                        tool_input=dspy_ex.tool_input,
                        category=dspy_ex.category,
                        action_type=dspy_ex.action_type,
                        inside_workspace=dspy_ex.inside_workspace,
                        touches_secrets=dspy_ex.touches_secrets,
                        prompt_text=dspy_ex.prompt_text,
                        policy_excerpt=dspy_ex.policy_excerpt,
                    )
                    labeled_ex = {
                        **ex,
                        "teacher_label": getattr(pred, "label", "").strip(),
                        "teacher_action": getattr(pred, "action", "").strip(),
                        "teacher_reason": getattr(pred, "reason", "").strip(),
                        "teacher_confidence": getattr(pred, "confidence", "").strip(),
                        "teacher_reasoning": getattr(pred, "reasoning", "").strip(),
                        "teacher_model": TEACHER_MODEL_ID,
                    }
                    out.write(json.dumps(labeled_ex, ensure_ascii=False) + "\n")
                    labeled_count += 1
                except Exception as exc:
                    error_count += 1
                    # Fix 9: show which example failed, not the batch number
                    print(f"  ERROR on {ex.get('tool_use_id')}: {exc}")

            print(f"  Labeled {min(i + LABEL_BATCH_SIZE, len(examples))}/{len(examples)} ...")
            time.sleep(0.5)  # brief pause between batches to avoid rate limiting

    print(f"\nLabeling complete: {labeled_count} labeled, {error_count} errors.")
    print(f"Output: {TEACHER_LABELED_PATH}")


# ---------------------------------------------------------------------------
# Mode: validate
# ---------------------------------------------------------------------------

def cmd_validate() -> None:
    print(f"Configuring DSPy with model: {TEACHER_MODEL_ID}")
    configure_dspy()

    if COMPILED_PROGRAM_PATH.exists():
        program = dspy.ChainOfThought(PermissionDecisionCoT)
        program.load(str(COMPILED_PROGRAM_PATH))
    else:
        program = dspy.ChainOfThought(PermissionDecisionCoT)
        print("WARNING: Running zero-shot (no compiled program found).")

    # Fix 5: validate uses 30 training examples that optimize did not use.
    # The held-out set is only for the final Evaluation Harness gate (Step 5 item 7)
    # — DSPy must never touch it.
    examples = _shuffled_training()[OPTIMIZE_SAMPLE_N:OPTIMIZE_SAMPLE_N + 30]
    if not examples:
        print("ERROR: No training examples left after the optimize sample.")
        sys.exit(1)

    print(f"Validating on {len(examples)} training examples not used by optimize...")
    dspy_examples = [_example_to_dspy(ex) for ex in examples]
    _evaluate(program, dspy_examples)


def _evaluate(program, examples: list[dspy.Example]) -> None:
    correct_action = 0
    correct_label = 0
    total = len(examples)

    for ex in examples:
        try:
            pred = program(
                tool_name=ex.tool_name,
                tool_input=ex.tool_input,
                category=ex.category,
                action_type=ex.action_type,
                inside_workspace=ex.inside_workspace,
                touches_secrets=ex.touches_secrets,
                prompt_text=ex.prompt_text,
                policy_excerpt=ex.policy_excerpt,
            )
            if getattr(pred, "action", "").strip().lower() == ex.action.strip().lower():
                correct_action += 1
            if getattr(pred, "label", "").strip() == ex.label.strip():
                correct_label += 1
        except Exception as exc:
            print(f"  ERROR: {exc}")

    # Note: label accuracy always looks perfect because every gold label is
    # "Warning" (set by the rule engine). Focus on action accuracy. Baseline
    # (always predict allow) ≈ 77% — the teacher must beat that.
    print(f"\nResults on {total} examples:")
    print(f"  Action accuracy : {correct_action}/{total} = {correct_action/total:.1%}")
    print(f"  Label accuracy  : {correct_label}/{total} = {correct_label/total:.1%} (ignore — always Warning)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "help"
    if mode == "optimize":
        cmd_optimize()
    elif mode == "label":
        cmd_label()
    elif mode == "validate":
        cmd_validate()
    else:
        print("Usage: python teacher/optimize.py [optimize|label|validate]")
        sys.exit(1)
