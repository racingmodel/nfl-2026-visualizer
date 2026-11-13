# Supervised Week 10 mixed archival/pregame recovery

Release `frontend-week10-recovery-bd0913766051f62c`. **Preparation only. No deployment authority is granted by this file.** Existing public URL: https://racingmodel.github.io/nfl-2026-visualizer/

## Scope and exact pins

This new authority is **WEEK10_MIXED_ARCHIVAL_PREGAME_STATIC_ONCE**, not the expired pregame-only Week 10 scope. The original WAS-NYG cutoff stays **13 November 2026 01:14 GMT**. Exactly that fixture may be published late, only with byte-identical pre-cutoff team, TD, Model B/C, source and price records. It is labelled **ARCHIVED PREGAME**; no post-kickoff forecasts, live odds or silent actuals ingestion. The remaining 13 fixtures must still pass fresh independent pregame/individual-cutoff checks.

The new publication deadline is the earliest remaining fixture cutoff, **15 November 2026 14:29 GMT**, never an extension of any original prediction cutoff. If any other fixture closes or becomes live/final/ambiguous, stop: this request cannot silently archive another game. All market observations remain captured references with separately preserved feed/retrieval clocks and unknown bookmaker update time.

431 exact static files; 426 served files after excluding five .nojekyll markers. Frozen original team, player, Model C and negative-binomial data are pinned. The current live parent remains Week 9 `frontend-week9-tidy-29466c12d79894cf` at `99debb21c2fc067fff1e3d4c9b3a0c1aec25c869`. The original accepted HTTP-500 run 43257307848 and suite 117139170263 must remain completed/cancelled with zero jobs. The authenticated server-side CheckRun deployment binding is unchanged.

| Pin | SHA256 |
|---|---|
| Manifest | `6cc743b046f98d7d53b57895faced2edca167f86ee571eef2af973691612ca5e` |
| Review request | `10ee96fe4935fe3d9e6bdc50276b25c61ef4f89b4fb4cb6ed0a2a5183d38d2d6` |
| Controls | `a344a30ddc992c937cd2db210e434e75523505840e24727b8da49b70c5832031` |
| Guard | `0fa37cecb59a14fc257afd9c5f0cd9d0f0ada8a6dfdf054ddda14dcdecb22e41` |
| Workflow | `6f91ba3c64cbc552325744d237d0a8ceae1b375af2d458199bf62959ef0fd59a` |
| Focused tests | `5f5d463e83f8b47efd013fc3c552d5afbcab1a5d56653a4a865976fb84ef80f2` |

## Staged review and merge — requires separate owner authority

1. Review assets [PR #14](https://github.com/racingmodel/nfl-2026-visualizer/pull/14), head `93efb870405d96f8b65660c1040195773dbc48e5`. Its nine paths contain eight changed/new site assets and the new manifest. Merge with a **merge commit**, retain the branch, and verify all 431 assets against the manifest. All workflows remain disabled; a merge must not publish.
2. Retarget the stacked controls PR from `codex/week10-recovery-assets-bd0913766051f62c` to main. Keep its reviewed head unchanged. Its diff must contain only the six reviewed control/document/test paths. Recheck the actual asset merge and manifest, then merge controls with a **merge commit** and retain its branch. No squash/rebase or intervening main commits: the guard binds the exact asset head and two-stage merge lineage.
3. Independently verify actual final main, source branch `codex/week10-recovery-controls-bd0913766051f62c`, disabled workflows, false routine flags, no active run/deployment, fresh schedule, unchanged Week 9 live parent and these exact pins. Do not reuse old approvals 6824322618 or 6829182348 or rerun either old failed/cancelled run.

## Fresh authenticated owner approval — only after the controls merge

Post one new **unedited** comment on the merged controls PR, using exactly one fenced JSON block. Replace the two placeholders below with the actual final merged main SHA and a real UTC expiry. The expiry must be after dispatch, no later than two hours after comment creation, and no later than **15 November 14:29 GMT**. Owner association, authorship, merge/creation/edit clocks, branch, scope, hashes, exact asset parent and actual main are verified through authenticated GitHub APIs. The following is a template, not approval:

```json
{
  "decision": "APPROVED",
  "repository": "racingmodel/nfl-2026-visualizer",
  "destination": "https://racingmodel.github.io/nfl-2026-visualizer/",
  "release_id": "frontend-week10-recovery-bd0913766051f62c",
  "manifest_sha256": "6cc743b046f98d7d53b57895faced2edca167f86ee571eef2af973691612ca5e",
  "request_sha256": "10ee96fe4935fe3d9e6bdc50276b25c61ef4f89b4fb4cb6ed0a2a5183d38d2d6",
  "installation_head": "<VERIFIED_FINAL_MERGED_MAIN_SHA>",
  "scope": "WEEK10_MIXED_ARCHIVAL_PREGAME_STATIC_ONCE",
  "expires_at_utc": "<ACTUAL_UTC_EXPIRY_MAX_TWO_HOURS_AFTER_COMMENT>",
  "unattended": false,
  "publication_mode": "MIXED_ARCHIVE_AND_PREGAME",
  "archived_fixture_ids": [
    "2026_10_WAS_NYG"
  ],
  "publication_deadline_utc": "2026-11-15T14:29:00+00:00"
}
```

Using the OS-backed CLI, post the completed block from a UTF-8 body file via `gh pr comment $controlsPrNumber --repo racingmodel/nfl-2026-visualizer --body-file $approvalFile`. Obtain the actual comment ID and validate it through the installed guard; do not fabricate it.

## One separate deployment authorisation — not granted by merging or commenting

After fresh owner approval passes and the owner separately authorises one dispatch:

```powershell
$installationHead = '<ACTUAL_FINAL_MERGED_MAIN_SHA>'
$approvalCommentId = '<NEW_VERIFIED_OWNER_COMMENT_ID>'
gh workflow enable 399724330 --repo racingmodel/nfl-2026-visualizer
gh workflow run 399724330 --repo racingmodel/nfl-2026-visualizer --ref main --field release_id=frontend-week10-recovery-bd0913766051f62c --field review_request_sha256=10ee96fe4935fe3d9e6bdc50276b25c61ef4f89b4fb4cb6ed0a2a5183d38d2d6 --field installation_head=$installationHead --field approval_comment_id=$approvalCommentId
```

Only workflow 399724330 may be enabled. The review job runs the focused tests, then actual approval/platform/asset/fresh-fixture checks. The publish job repeats checks immediately before Pages deployment, recognising only its authenticated own deployment via CheckRun.deployment. Unrelated active or ambiguous deployments fail closed.

One-time checks use complete **unfiltered** repository/workflow inventories plus direct current/predecessor IDs, not filtered zero counts. Missing/inconsistent inventories fail closed. A second dispatch on the installation SHA or run_attempt > 1 is rejected. If dispatch returns HTTP 500 or any uncertain response, **do not retry**. Disable the manual workflow, reconcile direct/unfiltered run and deployment records, and request a new explicit diagnosis/authority before any replacement.

Monitor the one run, verify the successful Pages deployment and all **426 served asset hashes**, and confirm Week 10 plus the archived WAS-NYG label and correct original TD/odds timestamps. Disable workflow 399724330 again. All other workflows and routine flags remain disabled/false throughout. No model rebuild, promotion, paid odds, alternative hosting or racing-model change is authorised.
