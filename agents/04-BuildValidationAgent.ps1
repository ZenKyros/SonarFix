<#
    BuildValidationAgent - prove which candidate solutions actually compile.

    Sonar's C# analyser only produces findings for code that builds, so this
    gate decides what the rest of the pipeline can work on.

    Uses MSBuild.exe, not `dotnet build`: every project here is legacy
    MSBuild-2003 format, which the dotnet CLI cannot load. Build output goes
    to log files under the run directory, never into the repository.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [int]$MaxSolutions = 5,
    [int]$TimeoutSec = 900,
    [switch]$SkipRestore
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '04-BuildValidationAgent' -RunDir $RunDir `
    -Inputs @{ maxSolutions = $MaxSolutions; timeoutSec = $TimeoutSec }

try {
    $discovery = Read-AgentOutput -RunDir $RunDir -Name '01-RepoDiscoveryAgent'
    $ranking = Read-AgentOutput -RunDir $RunDir -Name '02-SolutionRankingAgent'
    $repo = $discovery.repoPath

    $msbuild = Find-MSBuild
    Write-Host "  msbuild: $msbuild"
    $nuget = Find-NuGet
    if (-not $nuget) {
        Add-AgentWarning -Agent $agent -Message 'nuget.exe not found; packages.config solutions may fail to restore. Put nuget.exe on PATH or in tools\.'
    }

    $logDir = Join-Path $RunDir 'build-logs'
    $candidates = @($ranking.candidates | Select-Object -First $MaxSolutions)
    if ($candidates.Count -eq 0) { throw 'Ranking produced no buildable candidates.' }

    $results = foreach ($c in $candidates) {
        $slnFull = Join-Path $repo ($c.path -replace '/', '\')
        $safe = ($c.path -replace '[\\/:]', '_')
        Write-Host "  building $($c.name)"

        $restore = $null
        if (-not $SkipRestore) {
            if ($c.needsNuGetRestore -and $nuget) {
                $restore = Invoke-Tool -FilePath $nuget -Arguments @('restore', $slnFull) `
                    -WorkingDirectory $repo -LogPath (Join-Path $logDir "$safe.restore.log") -TimeoutSec 600
            } else {
                # PackageReference projects restore through MSBuild itself.
                $restore = Invoke-Tool -FilePath $msbuild -Arguments @($slnFull, '/t:Restore', '/v:minimal', '/nologo') `
                    -WorkingDirectory $repo -LogPath (Join-Path $logDir "$safe.restore.log") -TimeoutSec 600
            }
        }

        $buildLog = Join-Path $logDir "$safe.build.log"
        $build = Invoke-Tool -FilePath $msbuild `
            -Arguments @($slnFull, '/t:Build', '/p:Configuration=Debug', '/v:minimal', '/nologo', '/m',
                         '/consoleloggerparameters:NoSummary', '/p:TreatWarningsAsErrors=false') `
            -WorkingDirectory $repo -LogPath $buildLog -TimeoutSec $TimeoutSec

        # Pull the distinct compiler errors out so a human sees *why* it failed
        # without opening a multi-megabyte log.
        $errors = @()
        if ($build.ExitCode -ne 0 -and (Test-Path $buildLog)) {
            $errors = @(
                Get-Content -LiteralPath $buildLog |
                    Select-String -Pattern 'error [A-Z]{2,}\d+' |
                    ForEach-Object { $_.Line.Trim() } |
                    Sort-Object -Unique | Select-Object -First 8
            )
        }
        $ok = $build.ExitCode -eq 0
        Write-Host ("    {0}" -f $(if ($ok) { 'built' } elseif ($build.TimedOut) { "timed out after ${TimeoutSec}s" } else { "failed ($($errors.Count) distinct error(s))" })) `
            -ForegroundColor $(if ($ok) { 'Green' } else { 'Yellow' })

        [pscustomobject][ordered]@{
            path           = $c.path
            name           = $c.name
            score          = $c.score
            projectCount   = $c.projectCount
            restoreExit    = if ($restore) { $restore.ExitCode } else { $null }
            buildExit      = $build.ExitCode
            timedOut       = $build.TimedOut
            success        = $ok
            sampleErrors   = $errors
            buildLog       = (ConvertTo-RepoRelative -RepoPath $RunDir -FullPath $buildLog)
        }
    }

    $built = @($results | Where-Object { $_.success })
    if ($built.Count -eq 0) {
        Add-AgentError -Agent $agent -Message "No candidate solution compiled. Sonar cannot analyse C# without a successful build; review the logs in $logDir."
    }
    Write-Host "  $($built.Count)/$($results.Count) solution(s) built"

    $data = [ordered]@{
        msbuildPath   = $msbuild
        nugetPath     = $nuget
        attempted     = $results.Count
        succeeded     = $built.Count
        buildable     = @($built | ForEach-Object { $_.path })
        results       = @($results)
    }
    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
