<#
.SYNOPSIS
    MasterOrchestratorAgent - runs the SonarFix pipeline.

.DESCRIPTION
    Runs the agents in order, passing state through JSON files in one run
    directory. Each stage is resumable: an agent whose output already exists
    is skipped unless -Force is given, so a long build is never repeated
    because a later stage failed.

    Stages are grouped, and each group is a gate:

      survey   1-3   discovery, ranking, dependency graph   (read-only, fast)
      build    4     compile the ranked candidates          (slow, no network)
      scan     5-6   Sonar analysis and issue download      (needs SonarQube)
      plan     7-8   context and the remediation plan       (no AI spend)
      fix      8     apply recipes, and approved AI groups  (spends only if approved)
      verify   9-10  rebuild the blast radius, check the diff
      publish  11    push and open the pull request         (needs -Confirm)

    Default -Stage plan stops before anything is changed or spent.

.EXAMPLE
    .\Invoke-SonarFix.ps1 -RepoPath C:\src\repo -ProjectKey InfoImage_CK -Stage survey
    Inventory only. Nothing is built, nothing is spent.

.EXAMPLE
    .\Invoke-SonarFix.ps1 -RepoPath C:\src\repo -ProjectKey InfoImage_CK -Stage plan
    Everything up to the remediation plan, including Sonar. No AI calls.

.EXAMPLE
    .\Invoke-SonarFix.ps1 -RepoPath C:\src\repo -ProjectKey InfoImage_CK -Stage fix -ApplyMechanical
    Applies the zero-token recipes only. No AI.

.EXAMPLE
    .\Invoke-SonarFix.ps1 -RepoPath C:\src\repo -ProjectKey InfoImage_CK -Stage publish `
        -ApplyMechanical -ApproveAiGroups 'ai:csharpsquid:S3776' -Confirm
    Applies recipes plus one approved AI group, verifies the build, opens the PR.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RepoPath,
    [Parameter(Mandatory)][string]$ProjectKey,

    [ValidateSet('survey', 'build', 'scan', 'plan', 'fix', 'verify', 'publish')]
    [string]$Stage = 'plan',

    [string]$RunId,
    [string]$StateRoot = (Join-Path (Split-Path -Parent $PSScriptRoot) '.sonarfix\runs'),

    [string]$SonarUrl = 'http://localhost:9000',
    [string]$SonarToken = $env:SONAR_TOKEN,
    [string]$SolutionPath,

    [int]$MaxSolutions = 5,
    [int]$MaxIssues = 200,

    [switch]$ApplyMechanical,
    [string[]]$ApproveAiGroups = @(),
    [string]$Notes = '',
    [switch]$Confirm,
    [switch]$IgnoreBuildGate,
    [switch]$Force
)

. "$PSScriptRoot\_Common.ps1"

$STAGES = @('survey', 'build', 'scan', 'plan', 'fix', 'verify', 'publish')
$target = [array]::IndexOf($STAGES, $Stage)

$runDir = New-RunDir -Root $StateRoot -RunId $RunId
$runName = Split-Path -Leaf $runDir

Write-Host ''
Write-Host '=============================================' -ForegroundColor White
Write-Host ' SonarFix' -ForegroundColor White
Write-Host '=============================================' -ForegroundColor White
Write-Host "  repository : $RepoPath"
Write-Host "  sonar key  : $ProjectKey"
Write-Host "  stage      : $Stage"
Write-Host "  run        : $runName"
Write-Host "  state      : $runDir"

$results = [Collections.ArrayList]::new()

function Invoke-Stage {
    param(
        [string]$Script,
        [string]$OutputName,
        [hashtable]$Arguments,
        [switch]$AlwaysRun
    )
    $existing = Get-AgentOutputPath -RunDir $runDir -Name $OutputName
    if ((Test-Path $existing) -and -not $Force -and -not $AlwaysRun) {
        $prior = (Get-Content -LiteralPath $existing -Raw | ConvertFrom-Json)
        if ($prior.status -ne 'failed') {
            Write-Host ''
            Write-Host "  $OutputName" -ForegroundColor DarkGray
            Write-Host '  (cached; pass -Force to re-run)' -ForegroundColor DarkGray
            [void]$results.Add([pscustomobject]@{ agent = $OutputName; status = 'cached' })
            return 0
        }
    }

    $argv = @('-NoProfile', '-File', (Join-Path $PSScriptRoot $Script))
    foreach ($kv in $Arguments.GetEnumerator()) {
        if ($null -eq $kv.Value) { continue }
        if ($kv.Value -is [switch] -or $kv.Value -is [bool]) {
            if ($kv.Value) { $argv += "-$($kv.Key)" }
        } elseif ($kv.Value -is [array]) {
            if ($kv.Value.Count -gt 0) { $argv += "-$($kv.Key)"; $argv += $kv.Value }
        } else {
            $argv += "-$($kv.Key)"; $argv += "$($kv.Value)"
        }
    }

    & pwsh @argv
    $code = $LASTEXITCODE
    $status = switch ($code) { 0 { 'ok' } 2 { 'partial' } default { 'failed' } }
    [void]$results.Add([pscustomobject]@{ agent = $OutputName; status = $status })
    $code
}

$exitCode = 0
try {
    # --- survey ---------------------------------------------------------------
    if (Invoke-Stage -Script '01-RepoDiscoveryAgent.ps1' -OutputName '01-RepoDiscoveryAgent' `
            -Arguments @{ RepoPath = $RepoPath; RunDir = $runDir } -ne 0) {
        throw 'Discovery failed; nothing downstream can run.'
    }
    if ((Invoke-Stage -Script '02-SolutionRankingAgent.ps1' -OutputName '02-SolutionRankingAgent' `
            -Arguments @{ RunDir = $runDir; TopN = $MaxSolutions }) -gt 1) {
        throw 'Ranking failed.'
    }
    if ((Invoke-Stage -Script '03-DependencyGraphAgent.ps1' -OutputName '03-DependencyGraphAgent' `
            -Arguments @{ RunDir = $runDir }) -gt 1) {
        throw 'Dependency graph failed.'
    }
    if ($target -lt 1) { throw [OperationCanceledException]::new('stage-complete') }

    # --- build ----------------------------------------------------------------
    if ((Invoke-Stage -Script '04-BuildValidationAgent.ps1' -OutputName '04-BuildValidationAgent' `
            -Arguments @{ RunDir = $runDir; MaxSolutions = $MaxSolutions }) -gt 1) {
        throw 'No candidate solution compiled; Sonar cannot analyse C# without a build.'
    }
    if ($target -lt 2) { throw [OperationCanceledException]::new('stage-complete') }

    # --- scan -----------------------------------------------------------------
    if ((Invoke-Stage -Script '05-SonarAgent.ps1' -OutputName '05-SonarAgent' `
            -Arguments @{ RunDir = $runDir; ProjectKey = $ProjectKey; SonarUrl = $SonarUrl
                          SonarToken = $SonarToken; SolutionPath = $SolutionPath }) -gt 1) {
        throw 'Sonar analysis failed.'
    }
    if ((Invoke-Stage -Script '06-IssueCollectionAgent.ps1' -OutputName '06-IssueCollectionAgent' `
            -Arguments @{ RunDir = $runDir; ProjectKey = $ProjectKey; SonarUrl = $SonarUrl
                          SonarToken = $SonarToken }) -gt 1) {
        throw 'Issue collection failed.'
    }
    if ($target -lt 3) { throw [OperationCanceledException]::new('stage-complete') }

    # --- plan (no AI spend) ---------------------------------------------------
    if ((Invoke-Stage -Script '07-ContextCollectionAgent.ps1' -OutputName '07-ContextCollectionAgent' `
            -Arguments @{ RunDir = $runDir; MaxIssues = $MaxIssues }) -gt 1) {
        throw 'Context collection failed.'
    }

    $applying = $target -ge 4
    if ((Invoke-Stage -Script '08-FixGenerationAgent.ps1' -OutputName '08-FixGenerationAgent' -AlwaysRun `
            -Arguments @{ RunDir = $runDir; ProjectKey = $ProjectKey
                          PlanOnly = (-not $applying)
                          ApplyMechanical = ($applying -and $ApplyMechanical)
                          ApproveAiGroups = $(if ($applying) { $ApproveAiGroups } else { @() })
                          Notes = $Notes }) -gt 1) {
        throw 'Fix generation failed.'
    }
    if ($target -lt 5) { throw [OperationCanceledException]::new('stage-complete') }

    # --- verify ---------------------------------------------------------------
    Invoke-Stage -Script '09-BuildVerificationAgent.ps1' -OutputName '09-BuildVerificationAgent' -AlwaysRun `
        -Arguments @{ RunDir = $runDir } | Out-Null
    if ((Invoke-Stage -Script '10-GitAgent.ps1' -OutputName '10-GitAgent' -AlwaysRun `
            -Arguments @{ RunDir = $runDir }) -gt 1) {
        throw 'Git inspection failed.'
    }
    if ($target -lt 6) { throw [OperationCanceledException]::new('stage-complete') }

    # --- publish --------------------------------------------------------------
    if ((Invoke-Stage -Script '11-PRAgent.ps1' -OutputName '11-PRAgent' -AlwaysRun `
            -Arguments @{ RunDir = $runDir; ProjectKey = $ProjectKey
                          Confirm = $Confirm; IgnoreBuildGate = $IgnoreBuildGate }) -gt 1) {
        throw 'Pull request step failed.'
    }
} catch [OperationCanceledException] {
    # Reached the requested stage; not an error.
} catch {
    Write-Host ''
    Write-Host "  pipeline stopped: $($_.Exception.Message)" -ForegroundColor Red
    $exitCode = 1
}

Write-Host ''
Write-Host '---------------------------------------------' -ForegroundColor White
foreach ($r in $results) {
    $colour = @{ ok = 'Green'; cached = 'DarkGray'; partial = 'Yellow'; failed = 'Red' }[$r.status]
    Write-Host ("  {0,-28} {1}" -f $r.agent, $r.status) -ForegroundColor $colour
}
Write-Host "  state: $runDir" -ForegroundColor DarkGray
Write-Host "  resume with: -RunId $runName" -ForegroundColor DarkGray
exit $exitCode
