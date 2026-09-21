---
name: missing-scoping
description: A cleanup or mutation that targets by pattern/prefix without verifying ownership of what it's touching
path_trigger: "**/*.swift **/*.ts **/*.tsx **/routes/**/*.ts **/api/**/*.ts orchestrator/*.sh substrate/*.sh tools/*.sh"
---

Cleanup that deletes "anything under prefix X" without first confirming the specific object
belongs to the current operation is unscoped. When two operations share a namespace (same
user, same project, same directory), one operation's cleanup can silently destroy another's
live data.

Concrete failure: a billing-rejection handler deletes any upload path matching the project
prefix. Nothing establishes the path is an unregistered upload — it could be a successfully
registered file from a concurrent or prior operation. Replaying a registration after billing
blocks deletes the PDF while leaving its database row intact.

The safe shape requires POSITIVE PROOF OF OWNERSHIP before mutating:
- Verify the specific object's state in the same transaction: `WHERE path = $1 AND status = 'pending_upload' AND owner_id = $2`.
- Never delete by prefix/glob across a shared namespace; always delete by exact identifier
  with an ownership predicate.
- Cleanup and registration must be coordinated: if cleanup runs on failure, it must know
  what state it's allowed to clean (e.g. only rows it created in this transaction).

Flag: DELETE/rm with a LIKE or prefix match; cleanup blocks that run on failure without
checking which specific resource to remove; any "delete everything under X" that doesn't
hold a lock or check state acquired before the operation started.

(Captured 2026-09-12→14: kilabz caught unscoped upload cleanup in FieldVision blueprint
registration — deletion matched project prefix without confirming upload state.)
