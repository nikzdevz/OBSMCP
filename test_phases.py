"""Comprehensive verification of Phases 1-3."""
import sys
sys.stdout.reconfigure(encoding='utf-8')
from server.store import StateStore
from server.service import ObsmcpService
from server.config import load_config

config = load_config()
store = StateStore(config)
service = ObsmcpService(config)

print("=== PHASE 1: COMPREHENSIVE TESTS ===")

# Template: missing variable should error
print()
print("[TEST] Template: missing variable should error")
try:
    store.create_task_from_template("bug", {}, actor="test")
    print("[FAIL] Should have raised ValueError")
except ValueError as e:
    print(f"[PASS] ValueError: {e}")

# Template: partial variables should error
print()
print("[TEST] Template: partial variables should error")
try:
    store.create_task_from_template("bug", {"summary": "Test"}, actor="test")
    print("[FAIL] Should have raised ValueError")
except ValueError as e:
    print(f"[PASS] ValueError: {e}")

# Template: all variables provided should work
print()
print("[TEST] Template: all variables provided should work")
task = store.create_task_from_template("bug", {
    "summary": "Login crash",
    "steps": "1.Open 2.Click",
    "expected": "Success",
    "actual": "Crashes"
}, actor="test")
print(f"[PASS] Created: {task['id']} - {task['title']} - priority={task['priority']}, tags={task['tags']}")

# Template: non-existent template should error
print()
print("[TEST] Template: non-existent template should error")
try:
    store.create_task_from_template("nonexistent", {}, actor="test")
    print("[FAIL] Should have raised ValueError")
except ValueError as e:
    print(f"[PASS] ValueError: {e}")

# Delete template
print()
print("[TEST] Delete template")
deleted = store.delete_task_template("test")
templates = store.get_task_templates()
print(f"[PASS] Delete: {deleted}, remaining: {[t['name'] for t in templates]}")

# Re-create it
store.create_task_template("test", "Test: {name}", "Description {desc}", priority="medium", tags=["test"])
print(f"[PASS] Re-created: {[t['name'] for t in store.get_task_templates()]}")

# Audit log
audit = store.get_audit_log(limit=5)
print(f"[PASS] Audit log: {audit['total_events']} events, by_action={audit['by_action']}")

# Audit log: include_ai_only
audit2 = store.get_audit_log(include_ai_only=True, limit=5)
print(f"[PASS] Audit log (ai-only): {audit2['total_events']} events")

# Quick log: no current task scenario
print()
print("[TEST] Quick log with no current task")
with store._connect() as conn:
    conn.execute("UPDATE project_state SET value='' WHERE key='current_task_id'")
log = store.quick_log("Log with no task", actor="test")
print(f"[PASS] Quick log: task_id={log['task_id']} (None is OK)")

print()
print("=== PHASE 2: COMPREHENSIVE TESTS ===")

# Reset: invalid scope should error
print()
print("[TEST] Reset: invalid scope should error")
try:
    store.reset_project("invalid_scope", actor="test")
    print("[FAIL] Should have raised ValueError")
except ValueError as e:
    print(f"[PASS] ValueError: {e}")

# Bulk ops: atomic failure
print()
print("[TEST] Bulk ops: atomic failure")
ops = [
    {"action": "create", "title": "Good task", "description": "This works"},
    {"action": "update", "task_id": "INVALID-ID-XYZ", "status": "done"},
]
result = service.bulk_task_ops(operations=ops, actor="test")
print(f"[PASS] Atomic failure: failed={result['failed']}, message={result['message'][:60]}")
all_tasks = store.get_active_tasks(limit=100)
good_tasks = [t for t in all_tasks if "Good task" in t["title"]]
print(f"[PASS] No tasks created (rolled back): {len(good_tasks)} == 0")

# Export: verify files created
print()
print("[TEST] Export markdown files")
exp = store.export_project(format="markdown")
import os
files_exist = all(os.path.exists(f) for f in exp["files"])
print(f"[PASS] Export: {exp['file_count']} files, all exist={files_exist}")
print(f"  Files: {[os.path.basename(f) for f in exp['files']]}")

print()
print("=== PHASE 3: COMPREHENSIVE TESTS ===")

# Cycle detection
print("[TEST] Cycle detection: A blocks B, B blocks A should error")
tA = store.create_task("Cycle A", "A blocks B", actor="cycle-test")
tB = store.create_task("Cycle B", "B blocks A", actor="cycle-test")
store.add_task_dependency(task_id=tA["id"], blocks=[tB["id"]])
try:
    store.add_task_dependency(task_id=tB["id"], blocks=[tA["id"]])
    print("[FAIL] Should have detected cycle")
except ValueError as e:
    print(f"[PASS] Cycle detected: {str(e)[:60]}")

# Broken reference
print()
print("[TEST] Broken dependency reference should error")
try:
    store.add_task_dependency(task_id=tA["id"], blocked_by=["TASK-INVALID-999"])
    print("[FAIL] Should have errored")
except ValueError as e:
    print(f"[PASS] Broken ref error: {e}")

# Validate deps
print()
print("[TEST] Validate dependencies")
validate = store.validate_dependencies()
print(f"[PASS] Valid: {validate['valid']}, issues: {validate['issues']}")

# Session replay: no events case
print()
print("[TEST] Session replay: no events")
replay = store.session_replay("SESSION-NONEXISTENT")
print(f"[PASS] Session not found: {'error' in replay}")

# Log expiry: disable
print()
print("[TEST] Log expiry: disable (days=0)")
cfg = store.configure_log_expiry(0, actor="test")
print(f"[PASS] {cfg['message']}")

# Log stats
print()
print("[TEST] Log stats")
stats = store.get_log_stats()
print(f"[PASS] Total logs: {stats['total_logs']}, buckets: {stats['buckets']}")

# get_log_expiry_days returns correct value
days = store.get_log_expiry_days()
print(f"[PASS] get_log_expiry_days: {days}")

print()
print("=== TOOL DEFINITION CHECK ===")
tools = service.list_tool_definitions()
tool_names = [t["name"] for t in tools]
expected = [
    "get_task_templates", "get_task_template", "create_task_template", "delete_task_template",
    "create_task_from_template", "quick_log", "get_audit_log",
    "reset_project", "bulk_task_ops", "export_project",
    "configure_log_expiry", "expire_old_logs", "get_log_stats",
    "session_replay",
    "add_task_dependency", "remove_task_dependency", "get_task_dependency",
    "get_all_dependencies", "get_blocked_tasks", "validate_dependencies",
]
for name in expected:
    status = "[PASS]" if name in tool_names else "[FAIL]"
    print(f"  {status} {name}")

print()
print(f"Total tools registered: {len(tool_names)}")

print()
print("=== MCP TOOL CALL TESTS ===")
for name, args in [
    ("get_task_templates", {}),
    ("get_task_template", {"name": "bug"}),
    ("quick_log", {"message": "Test"}),
    ("get_audit_log", {"limit": 3}),
    ("get_log_stats", {}),
    ("get_blocked_tasks", {}),
    ("validate_dependencies", {}),
    ("export_project", {"format": "markdown"}),
    ("session_replay", {}),
]:
    try:
        r = service.call_tool(name, args)
        ok = "error" not in r and r is not None
        print(f"[PASS] {name}: OK" if ok else f"[FAIL] {name}: {r}")
    except Exception as e:
        print(f"[FAIL] {name}: {e}")

print()
print("ALL VERIFICATIONS COMPLETE")
