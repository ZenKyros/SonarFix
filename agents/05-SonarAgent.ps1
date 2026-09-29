<#
    SonarAgent - run SonarQube analysis over a solution that builds.

    RunSonar.ps1 was empty, so this is the whole step. For C# the analysis
    must wrap a real build:

        dotnet-sonarscanner begin  ->  MSBuild rebuild  ->  end

    The scanner hooks the compiler during that build; scanning without it
    yields almost no C# findings. Rebuild (not Build) is required, because an
    up-to-date project compiles nothing and the scanner then sees no code.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [Parameter(Mandatory)][string]$ProjectKey,
    [string]$SolutionPath,
    [string]$SonarUrl = 'http://localhost:9000',
    [string]$SonarToken = $env:SONAR_TOKEN,
    [int]$TimeoutSec = 1800
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '05-SonarAgent' -RunDir $RunDir `
    -Inputs @{ projectKey = $ProjectKey; sonarUrl = $SonarUrl; solution = $SolutionPath }

try {
    if (-not $SonarToken) {
        throw 'No SonarQube token. Pass -SonarToken or set $env:SONAR_TOKEN (My Account > Security > Generate Token).'
    }
    $discovery = Read-AgentOutput -RunDir $RunDir -Name '01-RepoDiscoveryAgent'
    $repo = $discovery.repoPath

    # Default to the best solution that actually built.
    if (-not $SolutionPath) {
        $validation = Read-AgentOutput -RunDir $RunDir -Name '04-BuildValidationAgent'
        $SolutionPath = @($validation.buildable)[0]
        if (-not $SolutionPath) { throw 'No solution built successfully, so there is nothing to analyse.' }
        Write-Host "  using first buildable solution: $SolutionPath"
    }
    $slnFull = Join-Path $repo ($SolutionPath -replace '/', '\')
    if (-not (Test-Path -LiteralPath $slnFull)) { throw "Solution not found: $slnFull" }

    $scanner = Get-Command dotnet-sonarscanner -ErrorAction SilentlyContinue
    $useDotnetTool = $null -ne $scanner
    if (-not $useDotnetTool) {
        $probe = & dotnet sonarscanner 2>&1 | Out-String
        if ($probe -notmatch 'SonarScanner') {
            throw 'dotnet-sonarscanner not found. Install with: dotnet tool install --global dotnet-sonarscanner'
        }
    }
    $msbuild = Find-MSBuild
    $logDir = Join-Path $RunDir 'sonar-logs'

    # Reachability check first: a wrong URL otherwise surfaces as a confusing
    # scanner error several minutes into the build.
    try {
        $status = Invoke-RestMethod -Uri "$SonarUrl/api/system/status" -TimeoutSec 15
        Write-Host "  SonarQube $($status.version) is $($status.status)"
        if ($status.status -ne 'UP') { throw "SonarQube reports status '$($status.status)'." }
    } catch {
        throw "Cannot reach SonarQube at $SonarUrl : $($_.Exception.Message)"
    }

    function Invoke-Scanner {
        param([string[]]$ScannerArgs, [string]$LogName)
        $file = if ($useDotnetTool) { 'dotnet-sonarscanner' } else { 'dotnet' }
        $argv = if ($useDotnetTool) { $ScannerArgs } else { @('sonarscanner') + $ScannerArgs }
        Invoke-Tool -FilePath $file -Arguments $argv -WorkingDirectory $repo `
            -LogPath (Join-Path $logDir $LogName) -TimeoutSec $TimeoutSec
    }

    # --- begin ----------------------------------------------------------------
    Write-Host '  scanner begin'
    $begin = Invoke-Scanner -LogName 'begin.log' -ScannerArgs @(
        'begin'
        "/k:$ProjectKey"
        "/d:sonar.host.url=$SonarUrl"
        "/d:sonar.token=$SonarToken"
        # bin/obj hold build output and vendored DLL copies: analysing them
        # produces findings nobody can act on.
        '/d:sonar.exclusions=**/bin/**,**/obj/**,**/packages/**,**/3rdPartyLibs/**,**/*.Designer.cs'
        '/d:sonar.scanner.scanAll=false'
    )
    if ($begin.ExitCode -ne 0) {
        throw "Scanner 'begin' failed (exit $($begin.ExitCode)); see $($begin.LogPath)"
    }

    # --- rebuild --------------------------------------------------------------
    Write-Host '  rebuilding under the scanner'
    $build = Invoke-Tool -FilePath $msbuild `
        -Arguments @($slnFull, '/t:Rebuild', '/p:Configuration=Debug', '/v:minimal', '/nologo', '/m') `
        -WorkingDirectory $repo -LogPath (Join-Path $logDir 'build.log') -TimeoutSec $TimeoutSec
    if ($build.ExitCode -ne 0) {
        Add-AgentWarning -Agent $agent -Message "Rebuild exited $($build.ExitCode); analysis will cover only the projects that compiled."
    }

    # --- end (uploads) --------------------------------------------------------
    Write-Host '  scanner end (uploading)'
    $end = Invoke-Scanner -LogName 'end.log' -ScannerArgs @('end', "/d:sonar.token=$SonarToken")
    if ($end.ExitCode -ne 0) {
        throw "Scanner 'end' failed (exit $($end.ExitCode)); see $($end.LogPath)"
    }

    # --- wait for the server to finish processing -----------------------------
    # Issues are not queryable until the background task completes, so polling
    # here prevents IssueCollectionAgent from reading an empty project.
    $taskUrl = $null
    $reportFile = Get-ChildItem -Path $repo -Recurse -Filter 'report-task.txt' -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($reportFile) {
        foreach ($line in (Get-Content -LiteralPath $reportFile.FullName)) {
            if ($line -like 'ceTaskUrl=*') { $taskUrl = $line.Substring(10) }
        }
    }
    $analysisStatus = 'UNKNOWN'
    if ($taskUrl) {
        $headers = @{ Authorization = 'Bearer ' + $SonarToken }
        $deadline = (Get-Date).AddMinutes(10)
        do {
            Start-Sleep -Seconds 5
            try {
                $task = Invoke-RestMethod -Uri $taskUrl -Headers $headers -TimeoutSec 20
                $analysisStatus = $task.task.status
            } catch { break }
        } while ($analysisStatus -in 'PENDING', 'IN_PROGRESS' -and (Get-Date) -lt $deadline)
        Write-Host "  server task: $analysisStatus"
        if ($analysisStatus -ne 'SUCCESS') {
            Add-AgentWarning -Agent $agent -Message "Server-side analysis finished as '$analysisStatus'."
        }
    } else {
        Add-AgentWarning -Agent $agent -Message 'No report-task.txt found; cannot confirm the server finished processing.'
    }

    $data = [ordered]@{
        projectKey     = $ProjectKey
        sonarUrl       = $SonarUrl
        solution       = $SolutionPath
        buildExitCode  = $build.ExitCode
        analysisStatus = $analysisStatus
        dashboardUrl   = "$SonarUrl/dashboard?id=$ProjectKey"
        logDir         = 'sonar-logs'
    }
    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
