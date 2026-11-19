$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '../scripts/Invoke-NflPublication.ps1')

function Assert-Nfl($Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
}
function Test-Scenario([string]$Case, [bool]$Resume = $false, $Ticket = $null) {
    $Repo = 'racingmodel/nfl-2026-visualizer'
    $Box = @{ events = New-Object 'System.Collections.Generic.List[string]'; poll = -1; enabled = $Resume; posts = 0; stage = '' }
    if (-not $Ticket) {
        $Ticket = [pscustomobject]@{
            phase='reserved'; run_id=''; head='a'*40; comment_id='888'; request_sha='b'*64
            baseline_ids=@(44578183871); not_before_utc=''; conclusion=''
            expires_at_utc=[DateTimeOffset]::UtcNow.AddHours(1).ToString('o')
        }
    }
    if ($Case -eq 'expired') { $Ticket.expires_at_utc = [DateTimeOffset]::UtcNow.AddMinutes(-1).ToString('o') }
    $Handlers = @{
        Ticket = $Ticket
        Resume = $Resume
        PollLimit = 4
        Save = { param($Ticket) $Box.stage = $Ticket.phase }
        Enable = { $Box.enabled = $true; $Box.events.Add('enable') }
        PreflightEnabled = {
            Assert-Nfl $Box.enabled 'Preflight while disabled'
            $Box.events.Add('approval')
            if ($Case -eq 'bad-approval') { throw 'Stale approval' }
            $Ticket
        }
        Dispatch = {
            param($Ticket)
            Assert-Nfl ($Box.enabled -and $Box.stage -eq 'dispatch_attempted') 'Dispatch before approval journal'
            $Box.posts++
            $Box.events.Add('dispatch')
            if ($Case -in @('invisible','accepted-500')) { throw 'HTTP 500' }
            if ($Case -eq 'wrong-id') { return @{workflow_run_id=123} }
            @{workflow_run_id=456}
        }
        Read = {
            param($Path)
            Assert-Nfl $Box.enabled 'Workflow disabled prematurely'
            if ($Path -eq "repos/$Repo/actions/runs?per_page=100") { $Box.poll++ }
            if ($Path -match 'runs\?') {
                $Rows = @([pscustomobject]@{id=44578183871;workflow_id=399724330})
                if ($Case -ne 'invisible' -and ($Case -ne 'accepted-500' -or $Box.poll -gt 0)) {
                    $Rows += [pscustomobject]@{id=456;workflow_id=399724330}
                }
                return @{total_count=$Rows.Count;workflow_runs=$Rows}
            }
            $State = 'completed'
            if (-not $Resume) {
                if ($Box.poll -eq 0 -or $Case -eq 'stuck') { $State = 'queued' }
                elseif ($Box.poll -eq 1) { $State = 'in_progress'; $Box.events.Add('review') }
            }
            $Conclusion = if ($State -ne 'completed') { $null } elseif ($Case -eq 'failed-review') { 'failure' } else { 'success' }
            if ($Conclusion -eq 'failure') { $Box.events.Add('review_failed') }
            if ($Conclusion -eq 'success') { $Box.events.Add('publish') }
            $Box.terminal = ($State -eq 'completed')
            [pscustomobject]@{
                id=456;workflow_id=399724330;event='workflow_dispatch';head_sha=$Ticket.head;head_branch='main';run_attempt=1
                actor=@{login='racingmodel'};triggering_actor=@{login='racingmodel'};repository=@{full_name=$Repo}
                created_at=[DateTimeOffset]::UtcNow.ToString('o'); status=$State;conclusion=$Conclusion
            }
        }
        Wait = {}
        Verify = {
            param($Run)
            Assert-Nfl ($Box.enabled -and $Run.status -eq 'completed') 'Verification out of order'
            $Box.events.Add('verify')
            if ($Case -eq 'bad-assets') { throw 'Served asset mismatch' }
        }
        Disable = {
            Assert-Nfl $Box.terminal 'Disable before terminal'
            $Box.events.Add('disable')
            $Box.enabled = $false
            if ($Case -eq 'disable-failure') { throw 'Disable response uncertain' }
        }
    }
    $Problem = ''
    try { Invoke-NflOnePublication @Handlers -WarningAction SilentlyContinue | Out-Null }
    catch { $Problem = $_.Exception.Message }
    return @{ticket=$Ticket; events=($Box.events -join ','); enabled=$Box.enabled; posts=$Box.posts; problem=$Problem}
}
$a = Test-Scenario 'success'
Assert-Nfl ($a.events -eq 'enable,approval,dispatch,review,publish,verify,disable' -and $a.posts -eq 1 -and -not $a.problem -and -not $a.enabled) "Success lifecycle differs: $($a.events) $($a.problem)"
$a = Test-Scenario 'failed-review'
Assert-Nfl ($a.events -eq 'enable,approval,dispatch,review,review_failed,disable' -and $a.posts -eq 1 -and $a.problem -match 'consumed') 'Failed review did not disable exactly after the terminal run.'
$a = Test-Scenario 'accepted-500'
Assert-Nfl ($a.posts -eq 1 -and -not $a.problem -and $a.events -match 'publish,verify,disable$') 'Accepted ambiguous dispatch was retried or missed.'
foreach ($Case in @('invisible','stuck','wrong-id')) {
    $a = Test-Scenario $Case
    Assert-Nfl ($a.posts -eq 1 -and $a.enabled -and $a.events -notmatch 'disable' -and $a.problem -match 'Resume') "Uncertain dispatch changed workflow state for $Case"
    if ($Case -ne 'wrong-id') {
        $b = Test-Scenario 'success' $true $a.ticket
        Assert-Nfl ($b.posts -eq 0 -and $b.events -eq 'publish,verify,disable' -and -not $b.problem) 'Resume introduced a dispatch or did not disable.'
    }
}
foreach ($Case in @('bad-approval','expired')) {
    $a = Test-Scenario $Case
    Assert-Nfl ($a.posts -eq 0 -and $a.problem -match 'stopped before dispatch') 'Invalid or expired approval reached dispatch.'
}
$a = Test-Scenario 'bad-assets'
Assert-Nfl ($a.posts -eq 1 -and $a.events -match 'verify,disable$' -and $a.problem -match 'asset mismatch') 'Asset verification failure was hidden.'
$a = Test-Scenario 'disable-failure'
Assert-Nfl ($a.posts -eq 1 -and $a.ticket.phase -eq 'disable_pending' -and $a.problem -match 'not verified') 'Ambiguous disable was reported as complete.'
$b = Test-Scenario 'success' $true $a.ticket
Assert-Nfl ($b.posts -eq 0 -and $b.events -eq 'publish,verify,disable') 'Disable recovery retried the dispatch.'
$tmp = [IO.Path]::GetTempFileName()
try {
    foreach ($phase in @('reserved','dispatch_attempted','monitoring','finished')) {
        Save-NflPublicationJournal -Path $tmp -Ticket @{phase=$phase}
        $snapshot = Get-Content -LiteralPath $tmp -Raw -Encoding UTF8 | ConvertFrom-Json
        Assert-Nfl ($snapshot.phase -ceq $phase) 'Atomic journal replacement failed.'
    }
} finally {
    foreach ($path in @($tmp,$tmp+'.previous',$tmp+'.tmp')) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }
}
Write-Output "PASS on PowerShell $($PSVersionTable.PSVersion) - normal lifecycle, failed review, approved request expiry, HTTP 500, invisible/stuck runs, wrong run ID, resume, asset failure and uncertain disable."
