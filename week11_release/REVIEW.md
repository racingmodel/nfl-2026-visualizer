# Week 11 supervised Pages review

The draft contains a Week 11 overlay and the exact final inventory for the existing NFL GitHub Pages destination. The package is assembled from the verified Week 10 parent plus the 15 reviewed overlay files. All 425 unchanged parent paths and the older forecast payloads remain bound.

The original Week 10 release and verifier remain intact. The draft changes only the existing manually dispatched publication workflow, adds a Week 11 verifier and tests, and adds the Week 11 release package. Inspection, testing and a merge do not grant deployment authority.

## Required owner action

1. Review the draft and confirm the branch tip and exact manifest. Use a merge commit against the baseline main SHA in the review request. The guard rejects a different base or an edited owner comment.
2. Check that Pages still uses workflow publishing, the five workflows are disabled, repository routine publication flags are all false and no competing runs or deployments are active. Check the fresh NFL fixture schedule. Pregame authority closes on **20 November 2026 at 01:14 UTC**.
3. After merge, generate the commit-bound owner approval in the local NFL2026 project with the following command. It rereads the installed hashes and prints the exact unedited JSON block for the merged PR plus a one-time command.

    .\RUN_NFL_WEEKLY.ps1 -Mode OwnerApproval -PullRequest <merged-pr-number>

4. Post that block as a new comment from the authenticated repository owner. Obtain separate authorisation for one deployment before enabling workflow **399724330**. Dispatch once on main with the output from the local command and the new numeric owner comment ID.
5. Verify the exact Actions run and Pages deployment, all 435 publicly served asset hashes, and Week 11 as the default. Disable the manual workflow again. A failed review, transport ambiguity, expired approval or closed fixture consumes or blocks this path. Stop and reconcile the direct run, jobs and deployment records before requesting fresh authority.

The existing server-side CheckRun/deployment binding and the approval, commit, active-run, one-attempt, fixture-clock, parent and post-deployment hash checks apply. The publish job depends on a successful non-deployment review; review itself has read-only publication permissions. Windows CRLF and Unix LF are accepted inside the single unedited approval block.
