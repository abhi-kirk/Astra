#!/usr/bin/env bash
# PostToolUse hook: after Claude edits a Python file under src/ or tests/, run
# ruff (both) + pyright (src only — tests use MagicMock chains pyright can't type)
# and surface any diagnostics back to Claude via exit code 2 (feedback, not a
# blocking failure — the edit already applied). Non-Python / out-of-scope: no-op.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0   # repo root (script lives in .claude/hooks/)

f=$(jq -r '.tool_input.file_path // empty' 2>/dev/null)
[ -z "$f" ] && exit 0
case "$f" in *.py) ;; *) exit 0 ;; esac
case "$f" in */src/*|src/*|*/tests/*|tests/*) ;; *) exit 0 ;; esac

out=""
ruff_out=$(.venv/bin/ruff check "$f" 2>&1); ruff_rc=$?
[ $ruff_rc -ne 0 ] && out+="=== ruff ===
$ruff_out
"
case "$f" in
  */src/*|src/*)
    pyright_out=$(.venv/bin/pyright "$f" 2>&1); pyright_rc=$?
    [ $pyright_rc -ne 0 ] && out+="=== pyright ===
$pyright_out
"
  ;;
esac

if [ -n "$out" ]; then
  printf 'Static-check issues in %s — fix before continuing:\n%s\n' "$f" "$out" >&2
  exit 2
fi
exit 0
