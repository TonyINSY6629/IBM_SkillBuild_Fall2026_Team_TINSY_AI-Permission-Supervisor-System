"""
tests/test_export.py

Tests for scripts/export_to_dataset.py: one labeled example per human answer,
joined to its decision, with a held-out split that never moves.

Run with:
    python -m pytest tests/test_export.py -v
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import export_to_dataset as ex


def _decision(tid, tool="execute_command", action="escalate", review=False):
    return {"written_at": "t", "snapshot": {
        "tool_use_id": tid, "tool_name": tool, "tool_input": {"command": "x"},
        "category": "shell_exec", "action": action, "prompt_text": "do it",
        "decided_by": "hard_rail" if review else "rule", "for_review_only": review}}


def _answer(tid, outcome="human_deny"):
    return {"written_at": "t2", "snapshot": {
        "record_type": "human_feedback", "tool_use_id": tid, "outcome": outcome,
        "final_action": "allow" if outcome == "human_allow" else "deny",
        "sent_to_pipeline": outcome != "timeout", "for_review_only": False}}


def _log(n=40):
    entries = []
    for i in range(n):
        entries += [_decision(f"t{i}"), _answer(f"t{i}", "human_allow" if i % 3 else "human_deny")]
    entries += [_decision(f"a{i}", action="allow") for i in range(30)]            # auto-allows
    entries += [_decision(f"h{i}", action="deny", review=True) for i in range(5)]  # hard rails
    return entries


def test_one_example_per_answer_with_request_fields():
    examples, _, _ = ex.build_examples(_log())
    assert len(examples) == 40
    x = examples[0]
    assert x["tool_name"] == "execute_command"
    assert x["prompt_text"] == "do it"
    assert x["human_decision"] in ("allow", "deny")


def test_auto_allows_are_not_examples():
    examples, _, _ = ex.build_examples(_log())
    assert not any(x["tool_use_id"].startswith("a") for x in examples)


def test_hard_rails_go_to_review_only_never_training():
    examples, review, _ = ex.build_examples(_log())
    assert len(review) == 5
    held, train = ex.split(examples)
    assert not any(x["tool_use_id"].startswith("h") for x in held + train)


def test_timeouts_and_housekeeping_are_left_out():
    entries = _log(5) + [_decision("q", tool="ask_followup_question"), _answer("q"),
                         _decision("to"), _answer("to", "timeout")]
    examples, _, skipped = ex.build_examples(entries)
    ids = {x["tool_use_id"] for x in examples}
    assert "q" not in ids and "to" not in ids
    assert skipped["housekeeping tool"] == 1
    assert skipped["timeout (no human answer)"] == 1


def test_exclude_list_removes_known_mislabels():
    examples, _, skipped = ex.build_examples(_log(), excluded_ids={"t0", "t1"})
    assert len(examples) == 38
    assert skipped["listed in exclude_tool_use_ids.txt"] == 2


def test_split_never_separates_a_question_from_its_answer():
    examples, _, _ = ex.build_examples(_log())
    held, train = ex.split(examples)
    assert len(held) + len(train) == len(examples)
    assert not ({x["tool_use_id"] for x in held} & {x["tool_use_id"] for x in train})


def test_held_out_set_is_stable_when_data_grows():
    held_a, _ = ex.split(ex.build_examples(_log(40))[0])
    held_b, _ = ex.split(ex.build_examples(_log(80))[0])
    ids_a = {x["tool_use_id"] for x in held_a}
    ids_b = {x["tool_use_id"] for x in held_b}
    assert ids_a <= ids_b          # nothing held out before ever moves to training
