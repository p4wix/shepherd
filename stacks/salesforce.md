+++
description = "Salesforce (Apex, LWC, metadata) and NetSuite (SuiteScript, SuiteCloud)"
# NetSuite-only projects are not detected: the sf team runs vibe-force, which is Salesforce only.
detect = ["sfdx-project.json"]
team = "sf"
+++
Salesforce and NetSuite project.

PRODUCTION IS READ-ONLY. On any production org or account agents only read: queries
(SOQL, SuiteQL, saved searches, reports), describe and metadata reads, and check-only
operations (`sf project deploy validate`, `suitecloud project:deploy --dryrun`,
`suitecloud project:validate`). Never deploy (quick deploy included), never create,
update or delete records, never change configuration, users or permissions. When a
production change is needed, write out the exact commands and let the user run them.
If you are not sure whether an org is production, treat it as production.

- Use `sf` CLI v2 and always pass `--target-org` explicitly; never the retired `sfdx force:*`.
- Every Apex class ships with a test class; mind governor limits (no SOQL or DML in loops).
- Run Apex tests against sandboxes only. On production the only deploy-related command
  allowed is check-only validation (`sf project deploy validate`).
