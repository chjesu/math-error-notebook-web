# Local candidate completion report — 2026-09-01

## Scope and decision

- Worktree: `C:\Users\Administrator\Documents\Codex\2026-09-01\orchestrate-x20\work\math-error-notebook-web-local-candidate`
- Branch: `orchestrate/local-candidate-20260901`
- Base candidate commit: `190f066c6112a96bf3b1fb8df0f91b5827c03bb5` (44 commits ahead of `origin/main` when the Orchestrate run began)
- Finish line reached: independently verified localhost + MySQL personal learning-loop candidate.
- Finish line not reached: production/cloud completion. This report does not claim deployment, push, merge, or production approval.
- Integration status: merge to the primary branch, push, and deployment remain pending explicit authorization.
- External processing: no external model or supplier was used.

## Reproducible checks

Run from the worktree root.

| Command | Result |
|---|---|
| `python -X utf8 -B -m unittest discover -s tests -p "test_*.py"` | PASS: 289 tests |
| `python -X utf8 -B -m unittest tests.test_web_app_e2e.NotebookE2ETests.test_harness_attachment_bridge_freezes_grades_and_writes_authoritative_receipts` | PASS: 1 test; includes frozen replay and in-process claim-release cleanup |
| `python -X utf8 -B scripts/codex_task_router.py route --task web-security-review --json` | exit 0; valid JSON routes to `gpt-5.6-sol`, high reasoning |
| `python -X utf8 -B -m scripts.codex_task_router route --task web-security-review --json` | exit 0; same valid routing JSON |
| `python -X utf8 -B scripts/local_env.py status` | `status=ok`; MySQL, migrations, schema, secrets, client configs, runtime and Python dependencies all true |
| `python -X utf8 -B scripts/local_env.py smoke` | PASS against real local MySQL: OTP request/verify/login/replay/concurrency, Secure cookie, 102,666-byte PDF, export and deletion; generated test records cleaned |
| `node --check web/app.js` | exit 0 |
| `python -m pip check` | `No broken requirements found.` |
| `npm ls --all --depth=0` | exit 0; independent worktree dependencies resolved |
| `python -X utf8 -B -c "import json; p=json.load(open('openapi/web-v1.json',encoding='utf-8'))['paths']; print(len(p)); print([x for x in p if x.startswith('/v1/admin')])"` | `45` and `[]` |
| `git diff --check` | PASS apart from line-ending conversion warnings |

The PDF review-link MySQL/HTTP probe also passed current-account isolation, cross-account 404, stale `input_version` 409, success 200 with `review_waiting`, repeat conflict, and no duplicate error/review writes. Its synthetic rows were rolled back.

## Browser and HTTP evidence

- In-app browser: desktop 1440×900 and mobile 390×844 both had zero horizontal overflow; current student navigation was present.
- The browser client blocked direct `/admin` navigation twice, so no browser-rendered 404 was captured. HTTP fallback proved both `/admin` and `/v1/admin/overview` return 404.
- A browser confirmation click was not exercised because no pending candidate existed. Fresh HTTP/service coverage exercised the confirmation boundary instead.
- Operations admin UI/API was deliberately removed on 2026-08-31. `0012_operations_admin.sql` remains historical forward-migration evidence only; `0013_model_usage_sessions.sql` is current.

## Security review

- Independent local candidate re-review: PASS, with no unresolved Critical or High findings.
- Confirmed High fixed: completed attachment replay previously trusted changed model payload fields. Replays now use persisted authoritative candidate/receipt fields; changed question, answer, or item count fails closed, and replay creates no extra candidate, error, review, or usage writes.
- Verification: targeted 24-test security set and full 289-test suite passed. The focused attachment replay/claim-release test above also passed after the low cleanup change became visible in the worktree.
- Accepted Low limitation: candidate replay recovery scans the latest 100 attempts. An older frozen attachment can return 409; this is a safe fail-closed ceiling.
- Production concern: in-process exclusion is not distributed across multiple workers. A production worker/session architecture must provide distributed exclusion and recovery before horizontal production scale.

## Remaining production blockers

- Production async OCR/AI workers and PDF/DOCX automatic intake.
- Fixed mathematics benchmark and manual review pack.
- Production application factory, worker, and external session store.
- OSS, RDS, KMS, real SMS, Turnstile, fixed egress and production network controls.
- Observability, load, backup/restore and disaster-recovery evidence.
- Production-equivalent E2E and independent production security sign-off.
- Human cloud deployment approval.

These blockers are intentional. The local candidate must not be described as production complete or deployed until every applicable gate is closed.
