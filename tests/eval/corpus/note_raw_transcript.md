DataSync Migration Planning Meeting
Date: 2026-06-10
Attendees: Alice Chen, Bob Martinez, Priya Nair, Tom Schulz

---

Quick sync before sprint planning. Main focus: nail down the approach for the DataSync schema migration ahead of Q3. Priya kicked things off by asking whether we go for a big-bang cutover or something more incremental.

Tom walked through what happened to the analytics team last year during their migration — they had about 45 minutes of downtime with the big-bang approach. Nobody wants that again, especially given that DataSync is customer-facing and we have SLA commitments. Alice said she'd already been looking at blue-green deployment as an alternative and had a rough outline ready.

Alice presented the blue-green deployment strategy. Idea: bring up a parallel "green" environment running the new schema, migrate traffic gradually, keep "blue" as live fallback until we're confident. Main benefit is zero-downtime cutover and easy rollback if something goes sideways. Bob asked about the state sync problem — what happens to writes that land on blue during the transition? Alice acknowledged it's a real issue and said we'd need a short dual-write window, probably 15-30 minutes, with reconciliation logic built into the migration script. Team agreed this is manageable.

Team decided on blue-green deployment strategy. No objections.

On tooling: Alice confirmed we'll use pgmigrate for the incremental schema changes. It handles versioned migration files, supports dry runs, and integrates with our existing CI pipeline. Bob had used it on a previous project and vouched for it.

Ownership split:
- Alice Chen owns the migration script development end to end, including the dual-write reconciliation logic and the pgmigrate configuration
- Bob Martinez takes rollback testing — he'll define the rollback scenarios, write the test scripts, and sign off before we promote to production
- Priya will handle comms with downstream teams that depend on the DataSync schema
- Tom is on infra — spinning up the green environment and monitoring during cutover

Q3 deadline came up. Target is end of September. Alice flagged that the schema changes touch several tables with foreign key constraints, and those need re-validation after the migration runs. It's not a blocker but it's a known risk — if any FK constraint fails post-migration, we'd need a fast fix path. She'll document that in the runbook.

Priya asked about data integrity checks mid-migration. Alice said pgmigrate has a verify step we can hook into, and she'll add custom assertions for the FK constraints specifically. Bob will include FK re-validation in his rollback test suite as a separate scenario.

One more thing flagged by Tom: the green environment will need production-like data volume for the migration dry run. He'll coordinate with the data team to get an anonymized snapshot. Timeline on that is next week.

Next steps before the sprint review:
- Alice to draft migration script skeleton and share in the shared drive by end of week
- Bob to set up rollback test environment and document initial scenarios
- Tom to request the anonymized data snapshot from data engineering
- Priya to send a heads-up email to downstream DataSync consumers

No blockers reported. Meeting wrapped in 40 minutes.
