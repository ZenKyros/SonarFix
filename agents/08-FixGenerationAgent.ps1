<#
    FixGenerationAgent - the fusion point, and where cost is controlled.

    PowerShell has gathered the facts; the decision of what to change is
    delegated to the tested Python core (recipes, grouping, batching).

    Order matters, and it is what keeps the bill down:

      1. import   the Sonar issues into the remediation store
      2. plan     group ~N issues into patterns and route each one:
                    mechanical -> a deterministic recipe, zero tokens
                    ai         -> needs judgement, one session per *group*
                    skip       -> vendored or generated code
      3. gate     stop unless a human approved the AI groups
      4. apply    recipes first, then only the approved AI groups

    Default is -PlanOnly: it costs nothing and prints what a run would do.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [Parameter(Mandatory)][string]$ProjectKey,
    [switch]$PlanOnly,
    [switch]$ApplyMechanical,
    [string[]]$ApproveAiGroups = @(),
    [string]$Notes = ''
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '08-FixGenerationAgent' -RunDir $RunDir -Inputs @{
    projectKey      = $ProjectKey
    planOnly        = [bool]$PlanOnly
    applyMechanical = [bool]$ApplyMechanical
    approveAiGroups = $ApproveAiGroups
}

try {
    $discovery = Read-AgentOutput -RunDir $RunDir -Name '01-RepoDiscoveryAgent'
    $collection = Read-AgentOutput -RunDir $RunDir -Name '06-IssueCollectionAgent'
    $issuesFile = Join-Path $RunDir $collection.issuesFile

    if (-not $discovery.git.isRepository) {
        throw "$($discovery.repoPath) is not a git repository. Fixes are committed on a branch, so clone the repo with git rather than downloading a zip."
    }

    # --- 1. import ------------------------------------------------------------
    Write-Host '  importing issues into the remediation store'
    $import = Invoke-SonarFixPython -Arguments @(
        'import-issues', '--project', $ProjectKey, '--repo', $discovery.repoPath, '--issues', $issuesFile
    )
    if (-not $import.ok) { throw "Import failed: $($import.error)" }
    Write-Host "    imported $($import.imported) open issue(s)"

    # --- 2. plan (no LLM calls) ----------------------------------------------
    Write-Host '  planning (no AI calls)'
    $plan = Invoke-SonarFixPython -Arguments @('plan', '--project', $ProjectKey)
    if (-not $plan.ok) { throw "Planning failed: $($plan.error)" }

    $s = $plan.summary
    Write-Host ''
    Write-Host '  Remediation plan' -ForegroundColor White
    Write-Host ("    {0,5} issues in {1} pattern(s)" -f $s.total, $s.groups)
    Write-Host ("    {0,5} mechanical  (0 tokens)" -f $s.mechanical) -ForegroundColor Green
    Write-Host ("    {0,5} need AI     ({1} group(s) -> {1} session(s), not {0})" -f $s.ai, $s.ai_groups) -ForegroundColor Yellow
    Write-Host ("    {0,5} skipped     (vendored or generated)" -f $s.skip) -ForegroundColor DarkGray

    $mechGroups = @($plan.groups | Where-Object { $_.bucket -eq 'mechanical' })
    $aiGroups = @($plan.groups | Where-Object { $_.bucket -eq 'ai' })

    if ($mechGroups.Count -gt 0) {
        Write-Host ''
        Write-Host '  Mechanical groups (safe to apply now):' -ForegroundColor Green
        foreach ($g in $mechGroups) {
            Write-Host ("    {0,-34} {1,3} issue(s)  {2}" -f $g.id, $g.unique, $g.recipe.title)
        }
    }
    if ($aiGroups.Count -gt 0) {
        Write-Host ''
        Write-Host '  AI groups (need explicit approval):' -ForegroundColor Yellow
        foreach ($g in $aiGroups) {
            Write-Host ("    {0,-34} {1,3} issue(s)  {2}" -f $g.id, $g.unique, $g.message)
        }
    }

    $selectedAi = @($ApproveAiGroups | Where-Object { $_ -in $aiGroups.id })
    $rejected = @($ApproveAiGroups | Where-Object { $_ -notin $aiGroups.id })
    foreach ($r in $rejected) {
        Add-AgentWarning -Agent $agent -Message "'$r' is not an AI group in this plan and was ignored."
    }

    $data = [ordered]@{
        projectKey   = $ProjectKey
        imported     = $import.imported
        summary      = $s
        costModel    = $plan.costModel
        mechanicalGroups = @($mechGroups | ForEach-Object {
            [pscustomobject][ordered]@{ id = $_.id; rule = $_.rule; issues = $_.unique; recipe = $_.recipe.title }
        })
        aiGroups     = @($aiGroups | ForEach-Object {
            [pscustomobject][ordered]@{ id = $_.id; rule = $_.rule; issues = $_.unique; message = $_.message }
        })
        applied      = $false
        batchId      = $null
        branch       = $null
    }

    # --- 3. gate --------------------------------------------------------------
    if ($PlanOnly -or (-not $ApplyMechanical -and $selectedAi.Count -eq 0)) {
        Write-Host ''
        Write-Host '  plan only - nothing changed, nothing spent' -ForegroundColor Cyan
        Write-Host '  to apply: re-run with -ApplyMechanical and/or -ApproveAiGroups <id>' -ForegroundColor DarkGray
        exit (Complete-Agent -Agent $agent -Data $data)
    }

    # --- 4. apply -------------------------------------------------------------
    $selection = [ordered]@{
        mechanical = if ($ApplyMechanical) { @($mechGroups.id) } else { @() }
        ai         = $selectedAi
        approveAi  = $selectedAi.Count -gt 0
        notes      = $Notes
    }
    $selectionPath = Join-Path $RunDir 'fix-selection.json'
    $selection | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $selectionPath -Encoding UTF8

    Write-Host ''
    Write-Host ("  applying: {0} mechanical group(s), {1} AI session(s)" -f $selection.mechanical.Count, $selectedAi.Count)
    $apply = Invoke-SonarFixPython -Arguments @('apply', '--project', $ProjectKey, '--selection', $selectionPath)
    if (-not $apply.ok) { throw "Apply failed ($($apply.status)): $($apply.error)" }

    Write-Host ("    {0} mechanical fix(es), {1} AI fix(es) on {2}" -f $apply.mechanicalFixes, $apply.aiFixes, $apply.branch) -ForegroundColor Green

    $data.applied = $true
    $data.batchId = $apply.batchId
    $data.branch = $apply.branch
    $data.baseBranch = $apply.baseBranch
    $data.mechanicalFixes = $apply.mechanicalFixes
    $data.aiFixes = $apply.aiFixes
    $data.aiSessionsUsed = $apply.aiSessions
    $data.batchStatus = $apply.status
    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
