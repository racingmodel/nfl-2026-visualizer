# Week 10 reviewed pre-publication recovery — not deployment authority

Static release: `frontend-week10-recovery-bd0913766051f62c` (unchanged).
Manifest SHA256: `6cc743b046f98d7d53b57895faced2edca167f86ee571eef2af973691612ca5e` (unchanged).
New review-request SHA256: `b5a1ed17b2a2712d17a6d32566538ed9e106e7b6b077b0968e7738b884d3a67c`.
New controls SHA256: `73f7416a0451af567e0fe254c5dc06e5f1ac7cc6c7d5010fa7aae6d7fbb6a1dd`.
Recovery ID: `week10-review-only-recovery-20261113-v1`.

## Why a new control request is required

Runs 43409067582 and 43416065208 both failed in review. Both Pages-artifact
uploads and publish jobs were skipped; neither created artifacts or a deployment.
The first consumed the installed one-time authority. Correcting the owner comment
did not authorise a second attempt. Do not rerun either run or reuse either comment.

This meaningful guard correction requires separate PR review and a merge commit
directly above `920b3fd2d684a68c777e9b804c9a84a665c75ccd`, retaining its branch.
It does not create a new static release or change any of the 431 site assets.
The prior asset/control merges and the exact two failed attempts remain pinned.
The guard re-fetches their jobs, artifacts and deployment absence before review
and again before deployment. Every other later attempt blocks, even on another SHA.
The only accepted self-deployment remains the authenticated CheckRun deployment.

## Owner procedure after reviewing and merging this correction

1. Verify the actual merge SHA, unchanged release/manifest/request/controls,
   disabled routine workflows, no active runs/deployments, Week 9 live parent,
   and a fresh independent schedule. Do not merge if main or this reviewed head moved.
2. Post ONE new, unedited approval comment on this correction's **merged PR**,
   not PR #15. Copy the block below, replace both placeholders with verified values,
   and use an expiry no more than two hours after posting. Windows CRLF and LF
   line endings are accepted; extra prose, multiple blocks and lone CRs are rejected.
3. Obtain separate explicit permission for one new dispatch. This draft PR/request
   alone is not owner publication approval or permission to enable a workflow.
4. Only after permission and every preflight passes, enable only 399724330 and
   dispatch once using the commands below. Never rerun or repeat a failed dispatch.
5. Inspect the exact new run, its jobs and Pages deployment. Verify all 426 served
   asset hashes against the 431-file manifest (five .nojekyll files are not served),
   Week 10 display, original source timestamps and archived WAS @ NYG labels.
   Disable 399724330 again, including on failure; all five must finish disabled.

**Hard deadline: 15 November 2026, 14:29 GMT/UTC.** This is unchanged from the
reviewed mixed archival/pregame release. WAS @ NYG stays archived with its original
13 November 01:14 cutoff. All other 13 fixtures keep their individual cutoffs.
An earlier LIVE, FINAL, changed or ambiguous remaining fixture also blocks.
Finish both verification and publication safely before the deadline; do not
extend it or grant a new archival exemption through this request.

## Approval template — incomplete and not authority until verified and posted

```json
{
  "decision": "APPROVED",
  "repository": "racingmodel/nfl-2026-visualizer",
  "destination": "https://racingmodel.github.io/nfl-2026-visualizer/",
  "release_id": "frontend-week10-recovery-bd0913766051f62c",
  "manifest_sha256": "6cc743b046f98d7d53b57895faced2edca167f86ee571eef2af973691612ca5e",
  "request_sha256": "b5a1ed17b2a2712d17a6d32566538ed9e106e7b6b077b0968e7738b884d3a67c",
  "installation_head": "REPLACE_WITH_ACTUAL_REVIEWED_MERGE_SHA",
  "scope": "WEEK10_MIXED_ARCHIVAL_REVIEW_RECOVERY_STATIC_ONCE",
  "recovery_id": "week10-review-only-recovery-20261113-v1",
  "supersedes_request_sha256": "10ee96fe4935fe3d9e6bdc50276b25c61ef4f89b4fb4cb6ed0a2a5183d38d2d6",
  "superseded_run_ids": [
    43409067582,
    43416065208
  ],
  "publication_mode": "MIXED_ARCHIVE_AND_PREGAME",
  "archived_fixture_ids": [
    "2026_10_WAS_NYG"
  ],
  "publication_deadline_utc": "2026-11-15T14:29:00+00:00",
  "expires_at_utc": "REPLACE_WITH_UTC_EXPIRY_NO_MORE_THAN_TWO_HOURS_AFTER_POSTING_AND_BEFORE_DEADLINE",
  "unattended": false
}
```

## Future PowerShell commands — DO NOT EXECUTE during preparation

Use the OS-stored GitHub account wrapper; no token export is needed.
Replace the two placeholders with the verified merged SHA and fresh owner comment ID.

```powershell
$python = 'C:/Users/scott/AppData/Local/Programs/Python/Python312/python.exe'
$merged = 'REPLACE_WITH_ACTUAL_REVIEWED_MERGE_SHA'
$comment = 'REPLACE_WITH_NEW_OWNER_COMMENT_ID'
& $python -B scripts/gh_stored_account.py workflow enable 399724330 --repo racingmodel/nfl-2026-visualizer
& $python -B scripts/gh_stored_account.py workflow run 399724330 --repo racingmodel/nfl-2026-visualizer --ref main -f release_id=frontend-week10-recovery-bd0913766051f62c -f review_request_sha256=b5a1ed17b2a2712d17a6d32566538ed9e106e7b6b077b0968e7738b884d3a67c -f installation_head=$merged -f approval_comment_id=$comment
# Inspect the accepted run directly. Do not retry on an HTTP error or missing history entry.
& $python -B scripts/gh_stored_account.py run view NEW_RUN_ID --repo racingmodel/nfl-2026-visualizer --json status,conclusion,jobs,headSha
# After completion OR failure, disable the manual workflow again.
& $python -B scripts/gh_stored_account.py workflow disable 399724330 --repo racingmodel/nfl-2026-visualizer
```

If another review fails, this request supplies no automatic retry permission.
Stop, preserve evidence and obtain a separately reviewed recovery if still safe.
