# Audit instructions — 2026-04-14

> **Frozen working note.** This is the original, unedited brief that started the
> fleet-wide repo audit on 2026-04-14. It is a record of intent, not a process
> document, and it is not maintained.
>
> What came out of it: `audit/phase1-scan.py`, `audit/phase1-results.json` and
> `audit/master-table.html`. The method was written up the same day as
> [2026-04-14-validation-plan.md](2026-04-14-validation-plan.md).

---

Before we validate or migrate from gen2 to gen3 first we need to make sure the repos work and by that I mean we should emulate the user/student and do the repo for them. From beginning to end following the documentation. we need to spot inconsistencies and things that are wrong or changed. We want to curate all the repos and make sure that they  actually work.


For each repo

We need to improve the integration.sh (integration tests of the repos) to make sure that they work, as an example see the live-debugger-bug-hunting repo. It has more mature tests and assertions that make sure that the repo works.

Improvements for the core functionality:


App exposure using ingress. The printGreeting shows 