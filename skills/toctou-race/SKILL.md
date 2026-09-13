---
name: toctou-race
description: Check-then-use races on fs/db/remote state keep recurring
path_trigger: orchestrator/*.sh src/runtime/*.py substrate/*.sh substrate/plists/*.json
---

A check followed by a separate act on fs/db/remote state is a window, not a guarantee — this
repo's reviews keep re-finding the same class: a staleness check reading lock mtime before the
holder writes its pid; count-then-claim capacity gates (a MAX_OPEN read that admits two writers);
existence checks before a write that a symlink swap or concurrent tick invalidates; a TTL sweep
closing a PR a human merged between the read and the close.

Preferred pattern — make the check and the act ONE atomic step, or re-verify at the act:
- DB: CAS in the WHERE clause (`UPDATE ... WHERE state='ready'`), `FOR UPDATE SKIP LOCKED`,
  advisory locks — never SELECT-then-UPDATE across statements.
- Filesystem: the create IS the check — atomic `mkdir` as the lock, `O_EXCL|O_NOFOLLOW` opens,
  dir-fd walks for ancestors, atomic `mv` for publishes. Never stat-then-write.
- Remote effectors (gh/GitHub, another host): re-read LIVE state immediately before the
  destructive act, treat unknown/ambiguous as DEFER, and confirm the mutation's result before
  recording it — the remote can change between any two of your steps and a lock here cannot
  serialize it.

Flag any change matching the trigger that checks state in one step and acts on it in another
without an atomic primitive or an at-the-act re-verify.

(Promoted from recurring reviewer findings [toctou-race]. Provenance: origin_repo=myndaix-runtime;
finding_ids=64e31049a07f2a61db06cec5553b4595490b7d76, ccd5e9585a37ccc700788018d100dcd282eca334,
bd5424970bac16cab48df3b1103028d805880f49, bb3ffeb1c19ed1da5c1b8bd8c64711f8d47a4a75.)
