<#
    Shared helpers for every SonarFix agent.

    Contract every agent honours:
      - takes -RunDir, a directory holding the state of one pipeline run
      - reads the JSON written by the agents before it
      - writes exactly one JSON envelope named <Order>-<Agent>.json
      - exits 0 on success, 1 on failure, 2 when it produced partial results

    Dot-source it:  . "$PSScriptRoot\_Common.ps1"
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$script:SonarFixVersion = '1.0'
# Declared up front: StrictMode throws when reading an unset variable.
$script:MSBuildPath = $null

# --- run state ----------------------------------------------------------------

function New-RunDir {
    param([string]$Root, [string]$RunId)
    if (-not $RunId) { $RunId = 'run-{0:yyyyMMdd-HHmmss}' -f (Get-Date) }
    $dir = Join-Path $Root $RunId
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $dir
}

function Get-AgentOutputPath {
    param([string]$RunDir, [string]$Name)
    Join-Path $RunDir "$Name.json"
}

<#
    Reads the output of an earlier agent. Fails loudly when the step is
    missing, because a silent $null here surfaces much later as a confusing
    error inside a different agent.
#>
function Read-AgentOutput {
    param(
        [Parameter(Mandatory)][string]$RunDir,
        [Parameter(Mandatory)][string]$Name,
        [switch]$Optional
    )
    $path = Get-AgentOutputPath -RunDir $RunDir -Name $Name
    if (-not (Test-Path $path)) {
        if ($Optional) { return $null }
        throw "Required input '$Name' is missing from $RunDir. Run that agent first."
    }
    $envelope = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($envelope.status -eq 'failed' -and -not $Optional) {
        throw "Input '$Name' failed on its own run; fix that before continuing."
    }
    $envelope.data
}

# --- envelope -----------------------------------------------------------------

function Start-Agent {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$RunDir,
        [hashtable]$Inputs = @{}
    )
    New-Item -ItemType Directory -Force -Path $RunDir | Out-Null
    Write-Host ''
    Write-Host "  $Name" -ForegroundColor Cyan
    Write-Host ('  ' + ('-' * $Name.Length)) -ForegroundColor DarkGray
    [pscustomobject]@{
        agent     = $Name
        version   = $script:SonarFixVersion
        runDir    = $RunDir
        startedAt = (Get-Date).ToUniversalTime().ToString('o')
        stopwatch = [Diagnostics.Stopwatch]::StartNew()
        inputs    = $Inputs
        warnings  = [Collections.ArrayList]::new()
        errors    = [Collections.ArrayList]::new()
    }
}

function Add-AgentWarning {
    param([Parameter(Mandatory)]$Agent, [Parameter(Mandatory)][string]$Message)
    [void]$Agent.warnings.Add($Message)
    Write-Host "  warn: $Message" -ForegroundColor Yellow
}

function Add-AgentError {
    param([Parameter(Mandatory)]$Agent, [Parameter(Mandatory)][string]$Message)
    [void]$Agent.errors.Add($Message)
    Write-Host "  error: $Message" -ForegroundColor Red
}

<#
    Writes the envelope and returns the exit code the agent should use, so a
    caller can `exit (Complete-Agent ...)`.
#>
function Complete-Agent {
    param(
        [Parameter(Mandatory)]$Agent,
        [Parameter(Mandatory)]$Data,
        [string]$Status
    )
    $Agent.stopwatch.Stop()
    if (-not $Status) {
        $Status = if ($Agent.errors.Count -gt 0) { 'partial' } else { 'ok' }
    }
    $envelope = [ordered]@{
        agent       = $Agent.agent
        version     = $Agent.version
        status      = $Status
        startedAt   = $Agent.startedAt
        finishedAt  = (Get-Date).ToUniversalTime().ToString('o')
        durationSec = [math]::Round($Agent.stopwatch.Elapsed.TotalSeconds, 2)
        inputs      = $Agent.inputs
        warnings    = @($Agent.warnings)
        errors      = @($Agent.errors)
        data        = $Data
    }
    $path = Get-AgentOutputPath -RunDir $Agent.runDir -Name $Agent.agent
    # -Depth 12 covers the deepest contract (dependency graph -> project -> refs).
    $envelope | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $path -Encoding UTF8
    $colour = @{ ok = 'Green'; partial = 'Yellow'; failed = 'Red' }[$Status]
    Write-Host "  $Status in $($envelope.durationSec)s -> $([IO.Path]::GetFileName($path))" -ForegroundColor $colour
    switch ($Status) { 'ok' { 0 } 'partial' { 2 } default { 1 } }
}

function Format-AgentError {
    param([Parameter(Mandatory)]$ErrorRecord)
    $i = $ErrorRecord.InvocationInfo
    $where = if ($i -and $i.ScriptName) {
        "$([IO.Path]::GetFileName($i.ScriptName)):$($i.ScriptLineNumber)"
    } else { 'unknown location' }
    "$($ErrorRecord.Exception.Message) [$where]"
}

# --- toolchain ----------------------------------------------------------------

<#
    Finds MSBuild.exe. This repository is entirely legacy MSBuild-2003 format
    (ToolsVersion 12.0, no Microsoft.NET.Sdk), so `dotnet build` cannot build
    it - only full MSBuild from a Visual Studio install can.
#>
function Find-MSBuild {
    if ($script:MSBuildPath -and (Test-Path $script:MSBuildPath)) { return $script:MSBuildPath }

    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path $vswhere) {
        $found = & $vswhere -latest -products * `
            -requires Microsoft.Component.MSBuild `
            -find 'MSBuild\**\Bin\MSBuild.exe' 2>$null | Select-Object -First 1
        if ($found -and (Test-Path $found)) {
            $script:MSBuildPath = $found
            return $found
        }
    }
    $fallback = Get-Command msbuild.exe -ErrorAction SilentlyContinue
    if ($fallback) { $script:MSBuildPath = $fallback.Source; return $fallback.Source }

    throw 'MSBuild.exe not found. Install Visual Studio Build Tools with the MSBuild component.'
}

function Find-NuGet {
    $cmd = Get-Command nuget.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $local = Join-Path $PSScriptRoot '..\tools\nuget.exe'
    if (Test-Path $local) { return (Resolve-Path $local).Path }
    $null
}

<#
    Runs an executable, capturing stdout+stderr to a log file rather than the
    console. Build logs are large and belong on disk, not in the transcript.
#>
function Invoke-Tool {
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [Parameter(Mandatory)][string[]]$Arguments,
        [string]$WorkingDirectory,
        [Parameter(Mandatory)][string]$LogPath,
        [int]$TimeoutSec = 900
    )
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null

    $psi = [Diagnostics.ProcessStartInfo]::new()
    $psi.FileName = $FilePath
    foreach ($a in $Arguments) { [void]$psi.ArgumentList.Add($a) }
    $psi.WorkingDirectory = if ($WorkingDirectory) { $WorkingDirectory } else { $PWD.Path }
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false

    $proc = [Diagnostics.Process]::Start($psi)
    # Read both streams as tasks: reading them in sequence deadlocks when a
    # build fills the stderr pipe buffer while we are still draining stdout.
    $stdout = $proc.StandardOutput.ReadToEndAsync()
    $stderr = $proc.StandardError.ReadToEndAsync()

    if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
        try { $proc.Kill($true) } catch { }
        "=== TIMED OUT after ${TimeoutSec}s ===" | Set-Content -LiteralPath $LogPath -Encoding UTF8
        return [pscustomobject]@{ ExitCode = 124; TimedOut = $true; LogPath = $LogPath }
    }

    ($stdout.Result + "`n" + $stderr.Result) | Set-Content -LiteralPath $LogPath -Encoding UTF8
    [pscustomobject]@{ ExitCode = $proc.ExitCode; TimedOut = $false; LogPath = $LogPath }
}

<#
    Calls the SonarFix Python package, which owns the tested remediation logic
    (mechanical recipes, issue grouping, Bitbucket pull requests). Returns the
    parsed JSON its bridge prints on stdout.
#>
function Invoke-SonarFixPython {
    param(
        [Parameter(Mandatory)][string[]]$Arguments,
        [int]$TimeoutSec = 1800
    )
    $root = Split-Path -Parent $PSScriptRoot
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = 'python' }

    $log = Join-Path $env:TEMP "sonarfix-py-$([guid]::NewGuid().ToString('N')).json"
    $result = Invoke-Tool -FilePath $python `
        -Arguments (@('-m', 'sonarfix.agentcli') + $Arguments) `
        -WorkingDirectory $root -LogPath $log -TimeoutSec $TimeoutSec

    $raw = Get-Content -LiteralPath $log -Raw -Encoding UTF8
    Remove-Item -LiteralPath $log -ErrorAction SilentlyContinue

    # The bridge prints one JSON object; anything before it is log noise.
    $start = $raw.IndexOf('{')
    if ($result.ExitCode -ne 0 -and $start -lt 0) {
        throw "sonarfix.agentcli failed (exit $($result.ExitCode)): $($raw.Trim())"
    }
    if ($start -lt 0) { throw "sonarfix.agentcli produced no JSON: $($raw.Trim())" }
    $raw.Substring($start) | ConvertFrom-Json
}

# --- paths --------------------------------------------------------------------

function Resolve-RepoRelative {
    param([Parameter(Mandatory)][string]$RepoPath, [Parameter(Mandatory)][string]$Path)
    $root = (Resolve-Path -LiteralPath $RepoPath).Path
    $full = [IO.Path]::GetFullPath((Join-Path $root $Path))
    if (-not $full.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Path escapes the repository: $Path"
    }
    $full
}

function ConvertTo-RepoRelative {
    param([Parameter(Mandatory)][string]$RepoPath, [Parameter(Mandatory)][string]$FullPath)
    $root = (Resolve-Path -LiteralPath $RepoPath).Path.TrimEnd('\')
    $FullPath.Substring($root.Length).TrimStart('\') -replace '\\', '/'
}

# Directories that are never source code worth scanning or fixing.
$script:ExcludedDirs = @(
    'bin', 'obj', 'packages', 'node_modules', '.git', '.vs', 'TestResults',
    '3rdPartyLibs', 'ThirdParty', 'lib', 'Debug', 'Release'
)

function Test-ExcludedPath {
    param([Parameter(Mandatory)][string]$Path)
    foreach ($segment in ($Path -split '[\\/]')) {
        if ($script:ExcludedDirs -contains $segment) { return $true }
    }
    $false
}
