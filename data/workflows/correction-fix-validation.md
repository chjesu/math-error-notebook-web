# Correction-state repair (local, 2026-09-03)

Scope approved by the user: preserve first review grades and schedules, update
explicit corrections independently, and recover the three already-graded correct
answers identified in the account. No regrading, authentication change, new error
entry, cloud deployment, or external model routing.

Diagnosis: settled PDF groups replay their original receipt before processing an
explicit correction. Calendar counts also count historical wrong group results,
while pending association discards correction intent.

Implementation plan: reuse versioned per-item submissions and their audit chain;
keep group receipts immutable; derive outstanding correction counts from current
item state; preserve correction intent on association. Exercise account isolation,
retry idempotence, shared reprints, and unchanged learning schedules in tests.

Recovery requires passing tests, a scoped local checkpoint backup, exact owned
PDF identities and current frozen grade candidates. User approval is the current
request “按建议修复”; implementation does not self-authorize broader changes.

Validation: all 318 unittest tests passed. The local MySQL synthetic transaction
check passed, including settled corrections and retry without task advancement;
all synthetic rows were rolled back. A scoped recovery dry run also rolled back:
three existing correct candidates resolved their owned PDF exercises, outstanding
count 4 -> 1, original receipts, review attempts, tasks, error entries, and daily
usage unchanged. No external model calls or regrading.

Recovery applied atomically after a local ignored backup. Replaying each accepted
candidate was idempotent. The restarted Web and Harness endpoints both returned
200. Authenticated browser verification after reload: outstanding count is 1 and
the three corrected exercises are absent from the correction detail. Answered
count remains 13, review count 11, and first-review accuracy 64%.

Runtime recovery report and backup remain outside Git under `.runtime/local-mysql/`.
Only this repair is committed; unrelated worktrees and local validation notes are
preserved. No production deployment or GitHub push is part of this task.
