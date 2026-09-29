<#
    BuildVerificationAgent - prove the fixes still compile.

    Rebuilding all 850 projects after a small change wastes hours. The
    dependency graph gives the blast radius: the projects that own the changed
    files, plus everything that consumes them. Only those are rebuilt.

    A failure here is a stop signal - the branch must not become a pull
    request until it builds.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [int]$MaxProjects = 40,
    [int]$TimeoutSec = 1200
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '09-BuildVerificationAgent' -RunDir $RunDir -Inputs @{ maxProjects = $MaxProjects }

try {
    $discovery = Read-AgentOutput -RunDir $RunDir -Name '01-RepoDiscoveryAgent'
    $graph = Read-AgentOutput -RunDir $RunDir -Name '03-DependencyGraphAgent'
    $fix = Read-AgentOutput -RunDir $RunDir -Name '08-FixGenerationAgent'
    $repo = $discovery.repoPath

    if (-not $fix.applied) {
        Write-Host '  no fixes were applied; nothing to verify'
        exit (Complete-Agent -Agent $agent -Data ([ordered]@{ skipped = $true; reason = 'no fixes applied' }))
    }

    # Which files changed on the fix branch?
    $changed = @(& git -C $repo diff --name-only "$($fix.baseBranch)...$($fix.branch)" 2>$null |
        Where-Object { $_ } | ForEach-Object { $_ -replace '\\', '/' })
    if ($changed.Count -eq 0) { throw "No file differences between $($fix.baseBranch) and $($fix.branch)." }
    Write-Host "  $($changed.Count) changed file(s)"

    # Owning project = the one in the nearest parent directory.
    $projectDirs = @(
        foreach ($p in $discovery.projects) {
            [pscustomobject]@{ dir = (Split-Path -Parent $p.path) -replace '\\', '/'; path = $p.path }
        }
    ) | Sort-Object { $_.dir.Length } -Descending

    $owners = [Collections.Generic.HashSet[string]]::new()
    foreach ($file in $changed) {
        $hit = @($projectDirs | Where-Object { $file.StartsWith($_.dir + '/', [StringComparison]::OrdinalIgnoreCase) } |
            Select-Object -First 1)
        if ($hit.Count -gt 0) { [void]$owners.Add($hit[0].path) }
        else { Add-AgentWarning -Agent $agent -Message "No project owns $file; it will not be build-checked." }
    }

    # Blast radius: owners plus every consumer, from the graph.
    $graphByPath = @{}
    foreach ($n in $graph.nodes) { $graphByPath[$n.path] = $n }
    $toBuild = [Collections.Generic.HashSet[string]]::new()
    foreach ($o in $owners) {
        [void]$toBuild.Add($o)
        if ($graphByPath.ContainsKey($o)) {
            foreach ($c in $graphByPath[$o].usedBy) { [void]$toBuild.Add($c) }
        }
    }
    Write-Host "  $($owners.Count) owning project(s); blast radius $($toBuild.Count) of $($graph.nodeCount)"

    $targets = @($toBuild) | Select-Object -First $MaxProjects
    if ($toBuild.Count -gt $MaxProjects) {
        Add-AgentWarning -Agent $agent -Message "Blast radius is $($toBuild.Count) projects; verifying the first $MaxProjects. Raise -MaxProjects for full coverage."
    }

    $msbuild = Find-MSBuild
    $logDir = Join-Path $RunDir 'verify-logs'
    $onBranch = (& git -C $repo rev-parse --abbrev-ref HEAD 2>$null)
    $switched = $false
    if ($onBranch -ne $fix.branch) {
        & git -C $repo checkout $fix.branch 2>&1 | Out-Null
        $switched = $true
    }

    try {
        $results = foreach ($proj in $targets) {
            $full = Join-Path $repo ($proj -replace '/', '\')
            $safe = ($proj -replace '[\\/:]', '_')
            $r = Invoke-Tool -FilePath $msbuild `
                -Arguments @($full, '/t:Build', '/p:Configuration=Debug', '/v:minimal', '/nologo', '/m') `
                -WorkingDirectory $repo -LogPath (Join-Path $logDir "$safe.log") -TimeoutSec $TimeoutSec
            $errs = @()
            if ($r.ExitCode -ne 0) {
                $errs = @(Get-Content -LiteralPath $r.LogPath |
                    Select-String -Pattern 'error [A-Z]{2,}\d+' |
                    ForEach-Object { $_.Line.Trim() } | Sort-Object -Unique | Select-Object -First 5)
            }
            [pscustomobject][ordered]@{
                project = $proj
                success = ($r.ExitCode -eq 0)
                exitCode = $r.ExitCode
                timedOut = $r.TimedOut
                errors  = $errs
            }
        }
    } finally {
        if ($switched) { & git -C $repo checkout $onBranch 2>&1 | Out-Null }
    }

    $failed = @($results | Where-Object { -not $_.success })
    $verdict = $failed.Count -eq 0
    if ($verdict) {
        Write-Host "  all $($results.Count) project(s) built - safe to open a pull request" -ForegroundColor Green
    } else {
        Add-AgentError -Agent $agent -Message "$($failed.Count) project(s) failed to build; do not open a pull request from this branch."
        foreach ($f in ($failed | Select-Object -First 3)) {
            Write-Host "    $($f.project): $(@($f.errors)[0])" -ForegroundColor Red
        }
    }

    $data = [ordered]@{
        branch          = $fix.branch
        changedFiles    = $changed.Count
        owningProjects  = @($owners)
        blastRadius     = $toBuild.Count
        verified        = $results.Count
        succeeded       = @($results | Where-Object { $_.success }).Count
        failed          = $failed.Count
        passesGate      = $verdict
        results         = @($results)
    }
    exit (Complete-Agent -Agent $agent -Data $data -Status $(if ($verdict) { 'ok' } else { 'partial' }))
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
