---
name: silent-error-suppression
description: An operation that partially fails but reports success or produces no observable signal
path_trigger: "**/*.swift **/*.ts **/*.tsx **/routes/**/*.ts **/api/**/*.ts orchestrator/*.sh substrate/*.sh tools/*.sh"
---

Silent failure is when code reaches a success path despite not doing what the caller
intended, and the caller has no way to know. It compounds: downstream code trusts the
"success" and builds on a corrupt state.

Two shapes appear repeatedly:

**Partial execution that looks complete.** A metadata-stripping call uses a flag
(`kCGImageMetadataShouldExcludeGPS`) documented to preserve proprietary location data in
MakerNotes and custom XMP fields. The flag does MOST of the job; the residual is silent.
The caller logs "GPS stripped" and the data ships with location intact. Safe shape: verify
the output artifact, not just the call — after stripping, read back metadata and assert the
sensitive fields are absent; fail loudly if not.

**Error path that falls through to success.** `try { … } catch { }` (empty catch),
`result ?? defaultValue` where the default is indistinguishable from a real result, or a
`|| true` on a security-critical check all let failures masquerade as success. Safe shape:
surface the error at the boundary where it can be handled — log with enough context to
diagnose, return a typed error, or re-throw; never swallow.

Flag: empty catch blocks; `?? fallback` on paths that should be fatal; `|| true` on
anything that gates access or data integrity; calls whose return value is discarded when
the return value is the only signal that the operation succeeded; "success" logs before
the result is verified.

(Captured 2026-09-10→14: kilabz caught GPS metadata residual in FieldVision photo upload;
oracle flagged swallowed sync errors across feat-sync-phase1.)
