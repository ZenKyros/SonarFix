<#
.SYNOPSIS
    Fix the single highest-priority SonarQube issue and open a pull request.

.DESCRIPTION
    One issue per run, deliberately. Large remediation batches are hard to
    review and hard to roll back; this keeps every pull request to a single
    finding a reviewer can judge in a minute.

    Reuses the survey the other agents already produced:

      01-RepoDiscoveryAgent   which project owns the file
      02-SolutionRankingAgent whether that code matters
      03-DependencyGraphAgent how many projects a change there would affect

    Priority is computed, not guessed. Security beats bugs, bugs beat smells,
    severity breaks ties, a deterministic recipe is preferred because it costs
    nothing, and touching a widely-consumed assembly is penalised because the
    blast radius makes it risky to change unattended.

    AI is used only when no recipe covers the rule, and only for that one
    issue.

.PARAMETER RunDir
    A run directory containing the three survey JSON files. Produce one with:
        .\Invoke-SonarFix.ps1 -RepoPath <repo> -ProjectKey <key> -Stage survey

.PARAMETER Apply
    Actually change code. Without it the agent only reports what it would do.

.PARAMETER CreatePR
    Push the branch and open the pull request. Implies -Apply.

.EXAMPLE
    .\Fix-OneIssue.ps1 -RunDir .sonarfix\runs\run-1 -ProjectKey InfoImage_CK
    Shows the ranked queue and the one issue it would fix. Changes nothing.

.EXAMPLE
    .\Fix-OneIssue.ps1 -RunDir .sonarfix\runs\run-1 -ProjectKey InfoImage_CK -Apply
    Fixes the top issue on a branch. Does not push.

.EXAMPLE
    .\Fix-OneIssue.ps1 -RunDir .sonarfix\runs\run-1 -ProjectKey InfoImage_CK -CreatePR
    Fixes it, verifies the build, pushes, and opens the pull request.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [Parameter(Mandatory)][string]$ProjectKey,

    [string]$SonarUrl = 'http://localhost:9000',
    [string]$SonarToken = $env:SONAR_TOKEN,

    [string]$IssueKey,
    [switch]$Apply,
    [switch]$CreatePR,
    [switch]$SkipVerify,
    [switch]$RefreshIssues,
    [int]$ShowTop = 10,
    [string]$Notes = ''
)

. "$PSScriptRoot\_Common.ps1"
if ($CreatePR) { $Apply = $true }

$agent = Start-Agent -Name 'Fix-OneIssue' -RunDir $RunDir -Inputs @{
    projectKey = $ProjectKey; issueKey = $IssueKey
    apply = [bool]$Apply; createPR = [bool]$CreatePR
}

# Weights: what makes one finding more worth fixing than another.
$TYPE_WEIGHT = @{ VULNERABILITY = 100; SECURITY_HOTSPOT = 80; BUG = 50; CODE_SMELL = 0 }
$SEVERITY_WEIGHT = @{ BLOCKER = 50; CRITICAL = 35; MAJOR = 20; MINOR = 8; INFO = 2 }

try {
    # --- survey ---------------------------------------------------------------
    $discovery = Read-AgentOutput -RunDir $RunDir -Name '01-RepoDiscoveryAgent'
    $graph = Read-AgentOutput -RunDir $RunDir -Name '03-DependencyGraphAgent'
    $ranking = Read-AgentOutput -RunDir $RunDir -Name '02-SolutionRankingAgent' -Optional
    $repo = $discovery.repoPath

    # --- issues ---------------------------------------------------------------
    $issuesPath = Join-Path $RunDir 'issues-raw.json'
    if ($RefreshIssues -or -not (Test-Path $issuesPath)) {
        if (-not $SonarToken) {
            throw @"
No SonarQube token yet.

When you have one:
  `$env:SONAR_TOKEN = '<token from $SonarUrl/account/security>'
  .\Fix-OneIssue.ps1 -RunDir '$RunDir' -ProjectKey '$ProjectKey'

Everything else is ready: the repository survey and dependency graph are in
$RunDir, so the first run will go straight to ranking and fixing.
"@
        }
        Write-Host '  fetching issues from SonarQube'
        & pwsh -NoProfile -File (Join-Path $PSScriptRoot '06-IssueCollectionAgent.ps1') `
            -RunDir $RunDir -ProjectKey $ProjectKey -SonarUrl $SonarUrl -SonarToken $SonarToken | Out-Null
        if ($LASTEXITCODE -gt 1) { throw 'Could not fetch issues from SonarQube.' }
    } else {
        Write-Host '  using cached issues (pass -RefreshIssues to re-fetch)'
    }
    $issues = @((Get-Content -LiteralPath $issuesPath -Raw -Encoding UTF8 | ConvertFrom-Json).issues)
    if ($issues.Count -eq 0) { throw "No open issues for '$ProjectKey'." }
    Write-Host "  $($issues.Count) open issue(s)"

    # --- lookups --------------------------------------------------------------
    $projectDirs = @(
        foreach ($p in $discovery.projects) {
            [pscustomobject]@{ dir = (Split-Path -Parent $p.path) -replace '\\', '/'; project = $p }
        }
    ) | Sort-Object { $_.dir.Length } -Descending

    $graphByPath = @{}
    foreach ($n in $graph.nodes) { $graphByPath[$n.path] = $n }

    $rankedPaths = @{}
    if ($ranking) { foreach ($r in $ranking.allRanked) { $rankedPaths[$r.path] = $r } }

    # Rules a recipe handles deterministically, so no tokens are spent.
    $MECHANICAL_RULES = @(
        'csharpsquid:S1128', 'csharpsquid:S3445', 'csharpsquid:S125',
        'java:S1128', 'java:S125', 'javascript:S1128', 'typescript:S1128',
        'javascript:S125', 'typescript:S125', 'python:S125',
        'python:S5754', 'python:S8572', 'powershelldre:S8642'
    )

    # --- rank -----------------------------------------------------------------
    $candidates = foreach ($issue in $issues) {
        $rel = $issue.component
        if ($rel -match '^[^:]+:(.+)$') { $rel = $Matches[1] }

        # Vendored, generated and build output are scan-configuration problems,
        # not code to change.
        if (Test-ExcludedPath -Path $rel) { continue }
        if ($rel -match '(?i)\.designer\.cs$|\.g\.cs$|/(vendor|third_?party|packages)/') { continue }

        $full = Join-Path $repo ($rel -replace '/', '\')
        if (-not (Test-Path -LiteralPath $full)) { continue }

        $owner = @($projectDirs | Where-Object { $rel.StartsWith($_.dir + '/', [StringComparison]::OrdinalIgnoreCase) } |
            Select-Object -First 1)
        $ownerProject = if ($owner.Count -gt 0) { $owner[0].project } else { $null }

        $impacted = 0
        if ($ownerProject -and $graphByPath.ContainsKey($ownerProject.path)) {
            $impacted = [int]$graphByPath[$ownerProject.path].impactedCount
        }

        $isMechanical = $MECHANICAL_RULES -contains $issue.rule
        $score = 0.0
        $score += $(if ($TYPE_WEIGHT.ContainsKey($issue.type)) { $TYPE_WEIGHT[$issue.type] } else { 0 })
        $score += $(if ($SEVERITY_WEIGHT.ContainsKey($issue.severity)) { $SEVERITY_WEIGHT[$issue.severity] } else { 5 })
        # A recipe is safe and free, so prefer it when priority is otherwise equal.
        if ($isMechanical) { $score += 25 }
        # Changing a widely-consumed assembly is risky to do unattended.
        $score -= [Math]::Min($impacted / 20.0, 25.0)
        # Test code matters less than shipped code.
        if ($ownerProject -and $ownerProject.isTest) { $score -= 30 }

        [pscustomobject][ordered]@{
            issueKey    = $issue.key
            rule        = $issue.rule
            type        = $issue.type
            severity    = $issue.severity
            message     = $issue.message
            file        = $rel
            line        = $issue.line
            project     = if ($ownerProject) { $ownerProject.path } else { $null }
            projectName = if ($ownerProject) { $ownerProject.name } else { '(unknown)' }
            impacted    = $impacted
            mechanical  = $isMechanical
            score       = [Math]::Round($score, 1)
        }
    }
    $queue = @($candidates | Sort-Object -Property score -Descending)
    if ($queue.Count -eq 0) { throw 'Every open issue is in vendored, generated or missing code.' }

    Write-Host ''
    Write-Host "  Priority queue (top $ShowTop of $($queue.Count))" -ForegroundColor White
    Write-Host ('  ' + '-' * 92) -ForegroundColor DarkGray
    Write-Host ('  {0,6}  {1,-17} {2,-9} {3,-7} {4}' -f 'SCORE', 'TYPE', 'SEVERITY', 'FIX', 'LOCATION') -ForegroundColor DarkGray
    foreach ($c in ($queue | Select-Object -First $ShowTop)) {
        $how = if ($c.mechanical) { 'recipe' } else { 'ai' }
        $colour = if ($c.type -in 'VULNERABILITY', 'SECURITY_HOTSPOT') { 'Red' }
                  elseif ($c.mechanical) { 'Green' } else { 'Yellow' }
        # Long legacy paths dominate the line; the tail is the useful part.
        $where = if ($c.file.Length -gt 58) { '...' + $c.file.Substring($c.file.Length - 55) } else { $c.file }
        Write-Host ('  {0,6}  {1,-17} {2,-9} {3,-7} {4}:{5}' -f
            $c.score, $c.type, $c.severity, $how, $where, $c.line) -ForegroundColor $colour
    }

    # --- select ---------------------------------------------------------------
    $chosen = if ($IssueKey) {
        $match = @($queue | Where-Object { $_.issueKey -eq $IssueKey })
        if ($match.Count -eq 0) { throw "Issue '$IssueKey' is not in the actionable queue." }
        $match[0]
    } else { $queue[0] }

    Write-Host ''
    Write-Host '  Selected' -ForegroundColor White
    Write-Host "    $($chosen.message)"
    Write-Host "    rule      $($chosen.rule)  ($($chosen.type)/$($chosen.severity))"
    Write-Host "    location  $($chosen.file):$($chosen.line)"
    Write-Host "    project   $($chosen.projectName)  [$($chosen.impacted) dependent project(s)]"
    Write-Host "    method    $(if ($chosen.mechanical) { 'deterministic recipe - no AI, no tokens' } else { 'one AI session for this issue only' })" `
        -ForegroundColor $(if ($chosen.mechanical) { 'Green' } else { 'Yellow' })

    $data = [ordered]@{
        projectKey  = $ProjectKey
        queueLength = $queue.Count
        queue       = @($queue | Select-Object -First $ShowTop)
        selected    = $chosen
        applied     = $false
        branch      = $null
        prUrl       = $null
    }

    if (-not $discovery.git.isRepository) {
        # Only blocks changes: the queue above is still worth seeing without a clone.
        Add-AgentWarning -Agent $agent -Message "$repo is not a git repository, so no fix can be committed. Clone it with git to go further."
        $data.blocked = 'not a git repository'
        exit (Complete-Agent -Agent $agent -Data $data)
    }

    if (-not $Apply) {
        Write-Host ''
        Write-Host '  report only - nothing changed' -ForegroundColor Cyan
        Write-Host '  add -Apply to fix it, or -CreatePR to fix, verify and raise the pull request' -ForegroundColor DarkGray
        exit (Complete-Agent -Agent $agent -Data $data)
    }

    # --- fix ------------------------------------------------------------------
    Write-Host ''
    Write-Host '  fixing'
    $fix = Invoke-SonarFixPython -Arguments @('fix-one', '--issue', $chosen.issueKey, '--notes', $Notes)
    if (-not $fix.ok) { throw "Fix failed: $($fix.error)" }
    if ($fix.status -eq 'no_changes') {
        Add-AgentWarning -Agent $agent -Message 'The fix produced no change; the code may already be correct.'
        exit (Complete-Agent -Agent $agent -Data $data)
    }

    Write-Host "    branch $($fix.branch)  commit $($fix.commit)" -ForegroundColor Green
    if ($fix.summary) { Write-Host "    $($fix.summary)" }
    $data.applied = $true
    $data.branch = $fix.branch
    $data.baseBranch = $fix.baseBranch
    $data.usedAi = $fix.usedAi
    $data.commit = $fix.commit
    $data.batchId = $fix.batchId

    # --- verify ---------------------------------------------------------------
    # Record the fix so 09/10/11 can read it through the normal contract.
    @{
        agent = '08-FixGenerationAgent'; version = '1.0'; status = 'ok'
        startedAt = (Get-Date).ToUniversalTime().ToString('o')
        finishedAt = (Get-Date).ToUniversalTime().ToString('o')
        durationSec = 0; inputs = @{ singleIssue = $chosen.issueKey }; warnings = @(); errors = @()
        data = @{
            applied = $true; batchId = $fix.batchId; branch = $fix.branch
            baseBranch = $fix.baseBranch; projectKey = $ProjectKey
        }
    } | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath (Join-Path $RunDir '08-FixGenerationAgent.json') -Encoding UTF8

    if (-not $SkipVerify) {
        Write-Host ''
        Write-Host '  verifying the build'
        & pwsh -NoProfile -File (Join-Path $PSScriptRoot '09-BuildVerificationAgent.ps1') -RunDir $RunDir | Out-Null
        $verify = Read-AgentOutput -RunDir $RunDir -Name '09-BuildVerificationAgent' -Optional
        $data.buildPassed = $(if ($verify -and $verify.PSObject.Properties.Name -contains 'passesGate') { $verify.passesGate } else { $null })
        if ($data.buildPassed -eq $false) {
            Add-AgentError -Agent $agent -Message 'The change does not compile; the branch is kept for inspection but will not be published.'
        }
    }

    & pwsh -NoProfile -File (Join-Path $PSScriptRoot '10-GitAgent.ps1') -RunDir $RunDir | Out-Null

    if (-not $CreatePR) {
        Write-Host ''
        Write-Host "  fixed on $($fix.branch) - not pushed" -ForegroundColor Cyan
        Write-Host "  review with: git -C `"$repo`" diff $($fix.baseBranch)...$($fix.branch)" -ForegroundColor DarkGray
        Write-Host '  add -CreatePR to push and open the pull request' -ForegroundColor DarkGray
        exit (Complete-Agent -Agent $agent -Data $data)
    }

    # --- publish --------------------------------------------------------------
    Write-Host ''
    & pwsh -NoProfile -File (Join-Path $PSScriptRoot '11-PRAgent.ps1') `
        -RunDir $RunDir -ProjectKey $ProjectKey -Confirm | Out-Null
    $pr = Read-AgentOutput -RunDir $RunDir -Name '11-PRAgent' -Optional
    if ($pr -and $pr.prUrl) {
        $data.prUrl = $pr.prUrl
        Write-Host "  pull request: $($pr.prUrl)" -ForegroundColor Green
        Write-Host '  one issue, one reviewer decision, nothing merged automatically' -ForegroundColor DarkGray
    } else {
        Add-AgentError -Agent $agent -Message 'The pull request was not created; see 11-PRAgent.json.'
    }

    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
