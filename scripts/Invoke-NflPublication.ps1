# Supervised single-dispatch operator. Windows PowerShell 5.1.
function Get-NflPublicationRun {
    param($Ticket, [scriptblock]$Read)
    $Repo = 'racingmodel/nfl-2026-visualizer'
    $All = & $Read "repos/$Repo/actions/runs?per_page=100"
    $Manual = & $Read "repos/$Repo/actions/workflows/399724330/runs?per_page=100"
    if ($All.total_count -ne @($All.workflow_runs).Count -or
        $Manual.total_count -ne @($Manual.workflow_runs).Count) { throw 'Run inventory incomplete.' }
    $New = @($Manual.workflow_runs | Where-Object { $_.id -notin $Ticket.baseline_ids })
    $OtherView = @($All.workflow_runs | Where-Object {
        $_.workflow_id -eq 399724330 -and $_.id -notin $Ticket.baseline_ids
    })
    if ($New.Count -gt 1 -or $OtherView.Count -gt 1) { throw 'Multiple new manual runs.' }
    if ($New.Count -ne 1 -or $OtherView.Count -ne 1 -or $OtherView[0].id -ne $New[0].id) { return }
    $Id = $New[0].id
    if ($Ticket.run_id -and "$($Ticket.run_id)" -ne "$Id") { throw 'Dispatch response and run inventories disagree.' }
    $Run = & $Read "repos/$Repo/actions/runs/$Id"
    if ($Run.id -ne $Id -or $Run.workflow_id -ne 399724330 -or $Run.event -ne 'workflow_dispatch' -or
        $Run.head_sha -cne $Ticket.head -or $Run.head_branch -cne 'main' -or $Run.run_attempt -ne 1 -or
        $Run.actor.login -cne 'racingmodel' -or $Run.triggering_actor.login -cne 'racingmodel' -or
        $Run.repository.full_name -cne $Repo -or [DateTimeOffset]$Run.created_at -lt [DateTimeOffset]$Ticket.not_before_utc) {
        throw 'New run does not match the approved first dispatch.'
    }
    return $Run
}

function Invoke-NflOnePublication {
    param($Ticket, [switch]$Resume, [scriptblock]$Read, [scriptblock]$Enable,
        [scriptblock]$Dispatch, [scriptblock]$Disable, [scriptblock]$PreflightEnabled,
        [scriptblock]$Verify, [scriptblock]$Save, [scriptblock]$Wait, [int]$PollLimit = 120)

    if ($Resume) {
        if ($Ticket.phase -notin @('dispatch_attempted','monitoring','terminal','disable_pending')) {
            throw 'This journal has no unresolved dispatch. Check GitHub manually; do not dispatch again.'
        }
    } else {
        if ($Ticket.phase -ne 'reserved') { throw 'This request already has a local attempt.' }
        $Ticket.phase = 'enable_requested'
        & $Save $Ticket
        & $Enable
        try {
            $Fresh = & $PreflightEnabled
            if ($Fresh.head -cne $Ticket.head -or $Fresh.request_sha -cne $Ticket.request_sha -or
                $Fresh.comment_id -cne $Ticket.comment_id -or
                (@($Fresh.baseline_ids | Sort-Object) -join ',') -ne (@($Ticket.baseline_ids | Sort-Object) -join ',')) {
                throw 'Approval or run inventory changed after enablement.'
            }
            if ([DateTimeOffset]::UtcNow -ge [DateTimeOffset]$Fresh.expires_at_utc) { throw 'Owner approval expired.' }
        } catch {
            throw "Preflight stopped before dispatch. Check workflow state and active runs before manually disabling. $($_.Exception.Message)"
        }
        $Ticket.not_before_utc = [DateTimeOffset]::UtcNow.AddSeconds(-5).ToString('o')
        $Ticket.phase = 'dispatch_attempted'
        & $Save $Ticket
        try {
            $Reply = & $Dispatch $Ticket
            if ($Reply -and "$($Reply.workflow_run_id)" -match '^[1-9][0-9]*$') {
                $Ticket.run_id = "$($Reply.workflow_run_id)"
                & $Save $Ticket
            }
        } catch {
            Write-Warning 'Dispatch response uncertain. Reconciling GitHub run records without a retry.'
        }
    }

    $Run = $null
    $Last = 'No matching terminal run has been observed.'
    for ($Poll = 0; $Poll -lt $PollLimit; $Poll++) {
        try { $Run = Get-NflPublicationRun -Ticket $Ticket -Read $Read }
        catch { $Run = $null; $Last = $_.Exception.Message }
        if ($Run) {
            $Ticket.run_id = "$($Run.id)"
            if ($Run.status -eq 'completed' -and $Run.conclusion) { break }
            $Ticket.phase = 'monitoring'
            & $Save $Ticket
            $Last = "Run $($Run.id) is $($Run.status)."
        }
        if ($Poll + 1 -lt $PollLimit) { & $Wait }
    }
    if (-not $Run -or $Run.status -ne 'completed' -or -not $Run.conclusion) {
        throw "$Last Workflow state left untouched. Resume with the existing journal. Do not disable or dispatch while the outcome is uncertain."
    }

    $Ticket.phase = 'terminal'
    $Ticket.conclusion = $Run.conclusion
    & $Save $Ticket
    try {
        if ($Run.conclusion -ne 'success') { throw "Run $($Run.id) ended $($Run.conclusion). This approval is consumed." }
        & $Verify $Run
    } finally {
        $Ticket.phase = 'disable_pending'
        & $Save $Ticket
        try {
            & $Disable
            $Ticket.phase = 'finished'
            & $Save $Ticket
        } catch {
            throw "Run $($Run.id) is terminal, but disabling was not verified. Resume or manually confirm workflow 399724330 is disabled. $($_.Exception.Message)"
        }
    }
    Write-Output "Run $($Run.id) verified. Workflow 399724330 is disabled."
}

function Save-NflPublicationJournal {
    param([string]$Path,$Ticket)
    $Tmp = $Path + '.tmp'
    [IO.File]::WriteAllText($Tmp,($Ticket | ConvertTo-Json -Depth 8),(New-Object Text.UTF8Encoding($false)))
    if ([IO.File]::Exists($Path)) {
        [IO.File]::Replace($Tmp,$Path,($Path + '.previous'))
    } else { [IO.File]::Move($Tmp,$Path) }
}

function Start-NflPublication {
    param([string]$Checkout, [string]$JournalDirectory, [string]$ApprovalCommentId,
          [switch]$AuthorizeOneDispatch, [switch]$Resume)
    if (-not $Resume -and (-not $AuthorizeOneDispatch -or $ApprovalCommentId -notmatch '^[1-9][0-9]*$')) {
        throw 'A fresh owner-comment ID and explicit one-dispatch authorisation are required.'
    }
    $Repo = 'racingmodel/nfl-2026-visualizer'
    $Workflow = '399724330'
    $requestPath = Join-Path $Checkout 'week11_release/review_request.json'
    $requestSha = (Get-FileHash -LiteralPath $requestPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $JournalPath = Join-Path $JournalDirectory ($requestSha + '.json')
    $LockPath = [IO.Path]::ChangeExtension($JournalPath, '.lock')
    $Muted = @{}
    foreach ($Name in @('GH_TOKEN','GITHUB_TOKEN','GH_ENTERPRISE_TOKEN','GITHUB_ENTERPRISE_TOKEN')) {
        if (Test-Path "Env:$Name") {
            $Muted[$Name] = [Environment]::GetEnvironmentVariable($Name,'Process')
            [Environment]::SetEnvironmentVariable($Name,$null,'Process')
        }
    }
    $Lease = $null
    try {
        [void][IO.Directory]::CreateDirectory($JournalDirectory)
        $Lease = [IO.File]::Open($LockPath,[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::None)
        $read = {
            param($Path)
            $Raw = & gh api --method GET $Path
            if ($LASTEXITCODE -ne 0) { throw "GitHub read failed for $Path." }
            if ($Raw) { foreach ($Item in (ConvertFrom-Json -InputObject ($Raw -join [Environment]::NewLine))) { $Item } }
        }
        if ((& $read 'user').login -cne 'racingmodel') { throw 'Use the saved repository owner login.' }
        $save = { param($State) Save-NflPublicationJournal -Path $JournalPath -Ticket $State }
        $preflight = {
            param([switch]$Enabled)
            $Options = @('operator-preflight','--approval-id',$ApprovalCommentId)
            if ($Enabled) { $Options += '--workflow-enabled' }
            $Raw = & python -B -X utf8 scripts/verify_week11_publication.py @Options
            if ($LASTEXITCODE -ne 0 -or -not $Raw) { throw 'Read-only publication preflight failed.' }
            ConvertFrom-Json -InputObject ($Raw -join [Environment]::NewLine)
        }
        Push-Location $Checkout
        try {
            if ($Resume) {
                if (-not (Test-Path -LiteralPath $JournalPath)) { throw 'No existing attempt journal to resume.' }
                $Ticket = Get-Content -LiteralPath $JournalPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($Ticket.request_sha -cne $requestSha) { throw 'Journal belongs to another request.' }
            } else {
                if (Test-Path -LiteralPath $JournalPath) { throw 'A journal already exists. Use ResumePublication to reconcile this attempt.' }
                $Ticket = & $preflight
                if ($Ticket.request_sha -cne $requestSha) { throw 'Local recovery request differs.' }
                $Ticket | Add-Member phase 'reserved'
                $Ticket | Add-Member not_before_utc ''
                $Ticket | Add-Member run_id ''
                $Ticket | Add-Member conclusion ''
                & $save $Ticket
            }
            $Handlers = @{
                Ticket = $Ticket
                Resume = $Resume
                Read = $read
                Save = $save
                PreflightEnabled = { & $preflight -Enabled }
                Wait = { Start-Sleep -Seconds 5 }
                Enable = {
                    & gh api --method PUT "repos/$Repo/actions/workflows/$Workflow/enable" | Out-Null
                    if ($LASTEXITCODE -ne 0) { throw 'Enablement unverified. No dispatch attempted. Check workflow state manually.' }
                }
                Dispatch = {
                    param($Grant)
                    $Body = @{
                        ref = 'main'
                        inputs = @{
                            release_id = [string]$Grant.release_id
                            review_request_sha256 = [string]$Grant.request_sha
                            installation_head = [string]$Grant.head
                            approval_comment_id = [string]$Grant.comment_id
                        }
                    } | ConvertTo-Json -Depth 4
                    $Url = "repos/$Repo/actions/workflows/$Workflow/dispatches"
                    $Reply = $Body | & gh api --method POST -H 'X-GitHub-Api-Version: 2026-03-10' $Url --input -
                    if ($LASTEXITCODE -ne 0) { throw 'Dispatch response unavailable.' }
                    if ($Reply) { ConvertFrom-Json -InputObject ($Reply -join [Environment]::NewLine) }
                }
                Verify = {
                    param($Run)
                    $Jobs = & $read "repos/$Repo/actions/runs/$($Run.id)/attempts/1/jobs?per_page=100"
                    $Valid = @($Jobs.jobs | Where-Object {
                        $_.name -in @('review','publish') -and $_.status -eq 'completed' -and $_.conclusion -eq 'success' -and
                        $_.head_sha -ceq $Run.head_sha -and $_.run_id -eq $Run.id -and $_.run_attempt -eq 1
                    })
                    if ($Jobs.total_count -ne 2 -or @($Jobs.jobs).Count -ne 2 -or $Valid.Count -ne 2 -or
                        @($Valid.name | Sort-Object -Unique).Count -ne 2) { throw 'Review and publish jobs did not verify.' }
                    $Deployments = @(& $read "repos/$Repo/deployments?environment=github-pages&per_page=100")
                    $Matching = @($Deployments | Where-Object { $_.sha -ceq $Run.head_sha })
                    if ($Deployments.Count -ge 100 -or $Matching.Count -ne 1 -or
                        [DateTimeOffset]$Matching[0].created_at -lt [DateTimeOffset]$Run.created_at) {
                        throw 'Pages deployment identity was not unique.'
                    }
                    $Status = @(& $read "repos/$Repo/deployments/$($Matching[0].id)/statuses?per_page=1")
                    if ($Status.Count -ne 1 -or $Status[0].state -ne 'success') { throw 'Pages deployment did not verify.' }
                    $PriorRun = $env:GITHUB_RUN_ID
                    $env:GITHUB_RUN_ID = "$($Run.id)"
                    try {
                        $Receipt = Join-Path $JournalDirectory ("live-$($Run.id)-" + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffffffZ') + '.json')
                        & python -B -X utf8 scripts/verify_week11_publication.py live --out $Receipt
                        if ($LASTEXITCODE -ne 0) { throw 'Served asset verification failed.' }
                    } finally { [Environment]::SetEnvironmentVariable('GITHUB_RUN_ID',$PriorRun,'Process') }
                }
                Disable = {
                    $Main = & $read "repos/$Repo/git/ref/heads/main"
                    $Runs = & $read "repos/$Repo/actions/workflows/$Workflow/runs?per_page=100"
                    if ($Main.object.sha -cne $Ticket.head -or $Runs.total_count -ne @($Runs.workflow_runs).Count -or
                        @($Runs.workflow_runs | Where-Object status -ne 'completed').Count) {
                        throw 'Main or workflow activity changed. Manual reconciliation required.'
                    }
                    & gh api --method PUT "repos/$Repo/actions/workflows/$Workflow/disable" | Out-Null
                    if ($LASTEXITCODE -ne 0) { throw 'Disable response unavailable.' }
                    $States = & $read "repos/$Repo/actions/workflows?per_page=100"
                    if ($States.total_count -ne 5 -or @($States.workflows).Count -ne 5 -or
                        @($States.workflows | Where-Object state -ne 'disabled_manually').Count) {
                        throw 'Disabled workflow states did not verify.'
                    }
                }
            }
            Invoke-NflOnePublication @Handlers
        } finally { Pop-Location }
    } finally {
        if ($Lease) { $Lease.Dispose() }
        foreach ($Name in $Muted.Keys) { [Environment]::SetEnvironmentVariable($Name,$Muted[$Name],'Process') }
    }
}
