#!/bin/bash
# ledger-backup — nightly pg_dump of the runtime ledger (the factory's durable memory and
# the future self-learning ground truth). Substrate-declared (ai.myndaix.ledger-backup) so
# reconcile installs it and the liveness-canary watches that it actually fires.
# Durability chain: dump here -> Syncthing (send-only folder) -> MacBook ~/MiniMirror ->
# Time Machine. Restore: pg_restore -d runtime <dump>. Every dump is TOC-verified with
# pg_restore --list before rotation — a dump that can't list is a dump that can't restore.
set -euo pipefail
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

DB="${LEDGER_DB:-runtime}"
PG_DUMP="${LEDGER_PG_DUMP:-pg_dump}"
PG_RESTORE="${LEDGER_PG_RESTORE:-pg_restore}"
KEEP_DAYS="${LEDGER_KEEP_DAYS:-14}"
OUT_DIR="${MYNDAIX_HOME:-$HOME/.myndaix}/backups/ledger"
LOG_FILE="${MYNDAIX_HOME:-$HOME/.myndaix}/backups/ledger-backup.log"

mkdir -p "$OUT_DIR"
# liveness-fire: every run — success OR failure — emits >=1 stdout line via the EXIT trap,
# so this job's .out mtime is real execution evidence for the liveness-canary.
tmp=""
on_exit() {
  rc=$?
  rm -f "$tmp"
  printf '[%s] liveness-fire: ledger-backup tick rc=%s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$rc"
}
trap on_exit EXIT INT TERM

log() { printf '[%s] [ledger-backup] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG_FILE"; }

stamp="$(date '+%Y%m%d-%H%M%S')"
dest="$OUT_DIR/ledger-$stamp.dump"

log "BEGIN dump db=$DB dest=$dest"
tmp="$(mktemp "$OUT_DIR/.ledger-partial.XXXXXX")"
if ! "$PG_DUMP" -Fc --no-password -f "$tmp" "$DB" 2>> "$LOG_FILE"; then
  log "FAIL pg_dump rc=$? — no dump written"
  exit 1
fi

# Verify the archive is restorable-shaped before accepting it.
if ! "$PG_RESTORE" --list "$tmp" > /dev/null 2>> "$LOG_FILE"; then
  log "FAIL dump verification (pg_restore --list) — discarding partial"
  exit 1
fi

mv "$tmp" "$dest"; tmp=""
size="$(du -h "$dest" | cut -f1 | tr -d ' ')"
log "OK dump $dest ($size)"

# Rotate: keep KEEP_DAYS most-recent daily dumps (lexicographic == chronological here).
# The Syncthing mirror on the MacBook keeps its own 30d versioning of anything we delete.
count=0
while IFS= read -r f; do
  count=$((count + 1))
  if [ "$count" -gt "$KEEP_DAYS" ]; then
    rm -f "$f"
    log "rotated out $(basename "$f")"
  fi
done < <(ls -1 "$OUT_DIR"/ledger-*.dump 2>/dev/null | sort -r)

log "END ok (retained $(( count < KEEP_DAYS ? count : KEEP_DAYS )) dumps)"
