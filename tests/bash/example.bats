#!/usr/bin/env bats
# example.bats — TEMPLATE + a real regression. Copy this shape to unit-test any bash you write.
# bats gives you: `run <cmd>` (captures $status + $output), setup()/teardown(), TAP output, CI-friendly
# exit codes. Test a SOURCED function, or a script's observable behavior via `run`.
# Run: `bats tests/bash/`

# --- the thing under test: a tiny pure helper (in real use, `source` your script's functions) ---
norm10() { printf '%s\n' "$((10#$1))"; }   # the CORRECT base-10 normalization (rules/bash-scripts.md)

# --- REGRESSION: the leading-zero octal trap that bit us 3x (2026-07-03) ---
@test "octal trap: 10# normalization reads leading-zero values as base-10, not octal" {
  run norm10 "08"
  [ "$status" -eq 0 ]        # bare \$((08)) would CRASH ('value too great for base'); 10# does not
  [ "$output" -eq 8 ]
  run norm10 "010"
  [ "$output" -eq 10 ]       # bare \$((010)) would silently be 8 (octal). 10# keeps it 10.
}

# --- TEMPLATE: assert a command's exit code AND output ---
@test "template — exit code + stdout of a command" {
  run bash -c 'printf "hello %s" world'
  [ "$status" -eq 0 ]
  [ "$output" = "hello world" ]
}

# --- TEMPLATE: assert a failure path (nonzero exit) is handled, not swallowed ---
@test "template — a failing command reports nonzero" {
  run bash -c 'exit 3'
  [ "$status" -eq 3 ]
}

# --- TEMPLATE: setup/teardown with a temp workspace (auto-cleaned) ---
setup_file() { export BATS_TMP="$(mktemp -d -t bats-example)"; }
teardown_file() { [ -n "${BATS_TMP:-}" ] && [ -d "$BATS_TMP" ] && find "$BATS_TMP" -mindepth 0 -delete 2>/dev/null || true; }

@test "template — write + read within an isolated temp dir" {
  printf 'data' > "$BATS_TMP/f"
  run cat "$BATS_TMP/f"
  [ "$output" = "data" ]
}
