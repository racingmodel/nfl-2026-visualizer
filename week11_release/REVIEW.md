# Week 11 review-only recovery

This draft keeps the existing Week 11 release and all 440 site assets byte-for-byte. The first kickoff remains 20 November 2026 01:15 UTC, with pregame release authority ending at 01:14 UTC.

GitHub created run 44578183871 at 17:31:58 UTC. The workflow record was updated at 17:31:59 and is manually disabled. The review job started at 17:32:02 and failed on the active-workflow-state check at 17:32:16. Its Pages upload and publish job were skipped. The failed run produced zero artifacts and zero deployments. The workflow API records the final state and update time. It does not supply the actor or PowerShell command for that change.

The consumed approval and request remain closed. A new request grants one attempt only after the reviewed recovery merge and a new owner approval. Review and pre-deploy re-fetch the exact prior run, check suite, jobs, original controls, zero artifacts and absence of deployment. Any prior attempt other than the one reviewed here blocks recovery.

## Owner procedure after separate merge review

1. Merge only by merge commit against the stated prior installation. In the local NFL2026 project, run RUN_NFL_WEEKLY.ps1 with -Mode OwnerApproval and the merged recovery PR number. Post the resulting fresh unedited owner block. Its expiry may not exceed the original pregame cutoff.
2. With separate dispatch authorisation, run RUN_NFL_WEEKLY.ps1 with -Mode Publish, -ApprovalCommentId set to the new comment ID and -AuthorizeOneDispatch. Do not wrap the command in an immediate workflow disable.
3. If the helper reports uncertainty or a stuck run, use -Mode ResumePublication with the same journal. It only observes the existing attempt and finishes verification/disable after a confirmed terminal state. It never resends the dispatch. An enable-state error before any local dispatch, competing activity, or a changed main needs manual reconciliation. The original 01:14 UTC cutoff still applies during review and pre-deploy.

The helper independently checks successful review and publish jobs, the exact Pages deployment, and the served asset hashes before confirming disable. On a failed terminal run it reports failure and disables the workflow. When the outcome cannot be confirmed, it leaves state alone and reports the next manual check.
