<#
    SolutionRankingAgent - decide which solutions are worth building and
    scanning first.

    Scanning all 144 solutions is neither affordable nor useful: many are
    test harnesses or samples, and some cannot build at all. This ranks them
    so the pipeline spends its build minutes on the solutions that carry real
    product code and are likely to compile.

    Reads 01-RepoDiscoveryAgent (no second disk walk).
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [int]$TopN = 10
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '02-SolutionRankingAgent' -RunDir $RunDir -Inputs @{ topN = $TopN }

try {
    $discovery = Read-AgentOutput -RunDir $RunDir -Name '01-RepoDiscoveryAgent'

    $byPath = @{}
    foreach ($p in $discovery.projects) { $byPath[$p.path.ToLowerInvariant()] = $p }

    $ranked = foreach ($sln in $discovery.solutions) {
        $members = foreach ($m in $sln.projects) { $byPath[$m.ToLowerInvariant()] }
        $members = @($members | Where-Object { $_ })

        $testCount = @($members | Where-Object { $_.isTest }).Count
        $productCount = $members.Count - $testCount
        $score = 0.0
        $reasons = [Collections.ArrayList]::new()

        # Value: product projects are what Sonar findings matter for.
        $score += $productCount * 12
        if ($productCount -gt 0) { [void]$reasons.Add("$productCount product project(s)") }

        # A solution that ships a library or service outranks a console sample.
        $libs = @($members | Where-Object { $_.outputType -eq 'Library' }).Count
        $score += $libs * 4

        # Blocker: a member project that is not on disk fails the build outright.
        if ($sln.missingProjects.Count -gt 0) {
            $score -= 500
            [void]$reasons.Add("$($sln.missingProjects.Count) referenced project(s) missing from disk")
        }

        # Test-only solutions produce findings nobody ships.
        if ($productCount -eq 0 -and $testCount -gt 0) {
            $score -= 200
            [void]$reasons.Add('test-only solution')
        }

        # Samples, harnesses and archived trees are low value to remediate.
        if ($sln.path -match '(?i)sample|demo|harness|scratch|backup|archive|obsolete|deprecated') {
            $score -= 150
            [void]$reasons.Add('sample or archived path')
        }

        # An empty solution cannot be scanned.
        if ($members.Count -eq 0) {
            $score -= 1000
            [void]$reasons.Add('no resolvable projects')
        }

        # Self-contained solutions build without hunting for outside output.
        $external = 0
        foreach ($m in $members) {
            foreach ($r in $m.projectRefs) {
                if ($sln.projects -notcontains $r) { $external++ }
            }
        }
        if ($external -gt 0) {
            $score -= [math]::Min($external * 8, 120)
            [void]$reasons.Add("$external cross-solution project reference(s)")
        }

        # packages.config needs nuget.exe restore, which may not be present.
        $needsNuGet = @($members | Where-Object { $_.hasPackagesConfig }).Count -gt 0

        [pscustomobject][ordered]@{
            path             = $sln.path
            name             = $sln.name
            score            = [math]::Round($score, 1)
            projectCount     = $members.Count
            productProjects  = $productCount
            testProjects     = $testCount
            missingProjects  = $sln.missingProjects.Count
            externalRefs     = $external
            needsNuGetRestore = $needsNuGet
            requiresMSBuild  = @($members | Where-Object { $_.style -eq 'legacy' }).Count -gt 0
            buildable        = ($members.Count -gt 0 -and $sln.missingProjects.Count -eq 0)
            reasons          = @($reasons)
        }
    }

    $sorted = @($ranked | Sort-Object -Property @{ Expression = 'score'; Descending = $true },
                                                @{ Expression = 'projectCount'; Descending = $true })
    $candidates = @($sorted | Where-Object { $_.buildable } | Select-Object -First $TopN)

    if ($candidates.Count -eq 0) {
        Add-AgentWarning -Agent $agent -Message 'No solution passed the buildable check; every one is missing a referenced project.'
    }

    Write-Host "  ranked $($sorted.Count) solutions; $($candidates.Count) candidate(s) selected"
    foreach ($c in ($candidates | Select-Object -First 5)) {
        Write-Host ("    {0,7:N1}  {1}" -f $c.score, $c.path) -ForegroundColor DarkGray
    }

    $data = [ordered]@{
        topN       = $TopN
        totalRanked = $sorted.Count
        buildableCount = @($sorted | Where-Object { $_.buildable }).Count
        candidates = $candidates
        allRanked  = $sorted
    }
    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
