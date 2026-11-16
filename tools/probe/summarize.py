#!/usr/bin/env python3
"""
Reads probe_log.jsonl and prints a summary of what was observed.
Run after using Bob normally for a day.

Usage:
    python3 ~/supervisor_probe/summarize.py
"""

import json
from collections import Counter, defaultdict
from pathlib import Path

LOG_FILE = Path.home() / "supervisor_probe" / "probe_log.jsonl"


def main() -> None:
    if not LOG_FILE.exists():
        print("No probe log found. Run Bob with the probe hooks installed first.")
        return

    records = []
    with LOG_FILE.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # skip malformed lines (partial writes, encoding artefacts)

    print(f"\n{'='*60}")
    print(f"PROBE SUMMARY  —  {len(records)} hook events logged")
    print(f"{'='*60}\n")

    # Count by hook event name.
    event_counts: Counter = Counter()
    tool_names: Counter = Counter()
    tool_input_shapes: dict = defaultdict(set)
    parse_errors = 0

    for r in records:
        payload = r.get("payload", {})

        if "parse_error" in payload:
            parse_errors += 1
            continue

        event = payload.get("hook_event_name", "UNKNOWN")
        event_counts[event] += 1

        if event in ("PreToolUse", "PostToolUse"):
            name = payload.get("tool_name", "UNKNOWN")
            tool_names[name] += 1
            # Record top-level keys of tool_input to understand shape.
            tool_input = payload.get("tool_input", {})
            if isinstance(tool_input, dict):
                tool_input_shapes[name].add(tuple(sorted(tool_input.keys())))

    # --- Event counts ---
    print("Hook events fired:")
    for event, count in sorted(event_counts.items()):
        print(f"  {event:<25} {count:>4}x")

    if parse_errors:
        print(f"\n  PARSE ERRORS: {parse_errors}")

    # --- Tool names seen in PreToolUse ---
    pre_tools = {
        r["payload"]["tool_name"]
        for r in records
        if r.get("payload", {}).get("hook_event_name") == "PreToolUse"
        and "tool_name" in r.get("payload", {})
    }
    post_tools = {
        r["payload"]["tool_name"]
        for r in records
        if r.get("payload", {}).get("hook_event_name") == "PostToolUse"
        and "tool_name" in r.get("payload", {})
    }

    print(f"\nTool names seen in PreToolUse ({len(pre_tools)}):")
    for t in sorted(pre_tools):
        print(f"  {t}")

    # --- Tools in PreToolUse but NOT in PostToolUse ---
    # These were either blocked by Bob's own prompt, or something else stopped them.
    only_pre = pre_tools - post_tools
    if only_pre:
        print(f"\nIn PreToolUse but NOT PostToolUse (blocked or cancelled?):")
        for t in sorted(only_pre):
            print(f"  {t}")
    else:
        print("\nAll PreToolUse tools also appeared in PostToolUse.")

    # --- tool_input shapes per tool ---
    print("\ntool_input field shapes per tool:")
    for tool, shapes in sorted(tool_input_shapes.items()):
        for shape in shapes:
            print(f"  {tool}: {list(shape)}")

    # --- Check for MCP tool calls ---
    # MCP tool names typically contain '/' or are not in the standard Bob tool list.
    standard_tools = {
        "read_file", "write_file", "apply_diff", "search_and_replace",
        "insert_content", "execute_command", "list_files", "glob", "grep",
        "GetSymbolsOverview", "FindSymbol", "FindReferencingSymbols",
        "ask_followup_question", "create_html_artifact", "create_chart",
        "update_todo_list", "switch_mode", "start_subtask", "start_workflow",
        "spawn_subagent", "use_skill", "office_read", "office_edit",
        "list_ibm_doc_libraries", "search_ibm_docs",
    }
    mcp_candidates = pre_tools - standard_tools
    if mcp_candidates:
        print(f"\nPossible MCP tool calls in PreToolUse (not in standard list):")
        for t in sorted(mcp_candidates):
            print(f"  {t}  <-- CONFIRM: is this an MCP tool?")
    else:
        print("\nNo MCP tool calls detected in PreToolUse.")
        print("  (Either no MCP tools were used, or PreToolUse does not fire for them.)")

    print(f"\n{'='*60}")
    print("Open Items to check manually from probe_log.jsonl:")
    print("  1. Does PreToolUse fire BEFORE Bob's own approval prompt?")
    print("     Look for PreToolUse entries followed immediately by a PostToolUse")
    print("     gap — a gap may mean Bob's own prompt fired in between.")
    print("  2. Does the hook hold open > 10s without Bob misbehaving?")
    print("     (Increase timeout in probe_settings.json to 15s and retest.)")
    print("  3. Check the Bob log for any 'blocked' entries to verify stderr")
    print("     reason text reaches the model context.")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
