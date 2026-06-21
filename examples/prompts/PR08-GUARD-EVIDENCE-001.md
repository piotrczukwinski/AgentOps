Implement the PR-08 evidence retention non-regression guard.

Context:
- Do not reduce data collection.
- Do not reduce source coverage.
- Do not reduce browser automation hardening.
- Do not reduce evidence retention.
- This is a guard/test/docs task, not a runtime behavior change.

Expected outputs:
- `scripts/verify_pr08_evidence_retention.py`
- `tests/test_pr08_evidence_retention_guards.py`
- `docs/audits/2026-06-15-pr08v-evidence-retention-guard.md`
- `agent-work/02-execution/reports/PR08-GUARD-EVIDENCE-001-review.md`

Keep the implementation narrow. Do not touch runtime crawler/search/network-automation/enrichment code.
