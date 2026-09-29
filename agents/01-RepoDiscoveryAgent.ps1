<#
    RepoDiscoveryAgent - inventory the repository.

    One filesystem pass builds the whole inventory. The earlier script walked
    the tree seven times, which on a 1 GB repo costs minutes rather than
    seconds.

    Emits: solutions (with the projects each one contains), projects (with
    style, framework and references), file counts, and the git remote.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RepoPath,
    [Parameter(Mandatory)][string]$RunDir
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '01-RepoDiscoveryAgent' -RunDir $RunDir -Inputs @{ repoPath = $RepoPath }

try {
    if (-not (Test-Path -LiteralPath $RepoPath)) { throw "Repository not found: $RepoPath" }
    $root = (Resolve-Path -LiteralPath $RepoPath).Path
    Write-Host "  scanning $root"

    # --- single pass ----------------------------------------------------------
    $solutionFiles = [Collections.ArrayList]::new()
    $projectFiles = [Collections.ArrayList]::new()
    $counts = @{}
    $sourceBytes = 0L

    foreach ($file in [IO.Directory]::EnumerateFiles($root, '*', [IO.SearchOption]::AllDirectories)) {
        $ext = [IO.Path]::GetExtension($file).ToLowerInvariant()
        if (-not $ext) { continue }
        if (Test-ExcludedPath -Path $file.Substring($root.Length)) { continue }

        if (-not $counts.ContainsKey($ext)) { $counts[$ext] = 0 }
        $counts[$ext]++

        switch ($ext) {
            '.sln' { [void]$solutionFiles.Add($file) }
            '.csproj' { [void]$projectFiles.Add($file) }
            '.vbproj' { [void]$projectFiles.Add($file) }
            { $_ -in '.cs', '.vb' } { $sourceBytes += (Get-Item -LiteralPath $file).Length }
        }
    }
    Write-Host "  found $($solutionFiles.Count) solutions, $($projectFiles.Count) projects"

    # --- projects -------------------------------------------------------------
    $projects = @{}
    $projectPattern = [regex]'<ProjectReference\s+Include="([^"]+)"'
    $assemblyPattern = [regex]'<Reference\s+Include="([^",]+)'

    foreach ($path in $projectFiles) {
        $rel = ConvertTo-RepoRelative -RepoPath $root -FullPath $path
        $entry = [ordered]@{
            path             = $rel
            name             = [IO.Path]::GetFileNameWithoutExtension($path)
            language         = if ($path -like '*.vbproj') { 'vbnet' } else { 'cs' }
            style            = 'legacy'
            targetFramework  = $null
            outputType       = $null
            assemblyName     = $null
            isTest           = $false
            projectRefs      = @()
            assemblyRefs     = @()
            hasPackagesConfig = Test-Path -LiteralPath (Join-Path (Split-Path -Parent $path) 'packages.config')
            parseError       = $null
        }
        try {
            $text = Get-Content -LiteralPath $path -Raw -Encoding UTF8

            # SDK-style projects carry an Sdk attribute; everything else is the
            # 2003 XML schema and needs full MSBuild rather than `dotnet build`.
            if ($text -match '<Project[^>]+Sdk\s*=') { $entry.style = 'sdk' }

            if ($text -match '<TargetFrameworkVersion>([^<]+)<') { $entry.targetFramework = $Matches[1] }
            elseif ($text -match '<TargetFrameworks?>([^<]+)<') { $entry.targetFramework = $Matches[1] }
            if ($text -match '<OutputType>([^<]+)<') { $entry.outputType = $Matches[1] }
            if ($text -match '<AssemblyName>([^<]+)<') { $entry.assemblyName = $Matches[1] }

            $refs = foreach ($m in $projectPattern.Matches($text)) {
                $raw = $m.Groups[1].Value
                $abs = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $path) $raw))
                if ($abs.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) {
                    ConvertTo-RepoRelative -RepoPath $root -FullPath $abs
                }
            }
            $entry.projectRefs = @($refs | Where-Object { $_ } | Sort-Object -Unique)
            $entry.assemblyRefs = @(
                foreach ($m in $assemblyPattern.Matches($text)) { $m.Groups[1].Value.Trim() }
            ) | Sort-Object -Unique

            $entry.isTest = (
                $entry.name -match '(?i)test|spec' -or
                $text -match '(?i)nunit|xunit|MSTest|Microsoft\.VisualStudio\.QualityTools'
            )
        } catch {
            $entry.parseError = $_.Exception.Message
            Add-AgentWarning -Agent $agent -Message "Could not parse $rel"
        }
        if (-not $entry.assemblyName) { $entry.assemblyName = $entry.name }
        $projects[$rel.ToLowerInvariant()] = $entry
    }

    # --- solutions ------------------------------------------------------------
    # Solution entries look like:
    #   Project("{GUID}") = "Name", "relative\path.csproj", "{GUID}"
    $slnPattern = [regex]'(?m)^Project\("\{[^}]+\}"\)\s*=\s*"[^"]*",\s*"([^"]+)"'
    $solutions = foreach ($path in $solutionFiles) {
        $dir = Split-Path -Parent $path
        $rel = ConvertTo-RepoRelative -RepoPath $root -FullPath $path
        $members = [Collections.ArrayList]::new()
        $missing = [Collections.ArrayList]::new()
        try {
            $text = Get-Content -LiteralPath $path -Raw -Encoding UTF8
            foreach ($m in $slnPattern.Matches($text)) {
                $inner = $m.Groups[1].Value
                if ($inner -notmatch '\.(cs|vb)proj$') { continue }  # skip solution folders
                $abs = [IO.Path]::GetFullPath((Join-Path $dir $inner))
                if (-not $abs.StartsWith($root, [StringComparison]::OrdinalIgnoreCase)) { continue }
                $memberRel = ConvertTo-RepoRelative -RepoPath $root -FullPath $abs
                if ($projects.ContainsKey($memberRel.ToLowerInvariant())) { [void]$members.Add($memberRel) }
                else { [void]$missing.Add($memberRel) }
            }
        } catch {
            Add-AgentWarning -Agent $agent -Message "Could not parse solution $rel"
        }
        $member = foreach ($m in $members) { $projects[$m.ToLowerInvariant()] }
        [pscustomobject][ordered]@{
            path            = $rel
            name            = [IO.Path]::GetFileNameWithoutExtension($path)
            directory       = (ConvertTo-RepoRelative -RepoPath $root -FullPath $dir)
            projectCount    = $members.Count
            projects        = @($members)
            missingProjects = @($missing)   # referenced but absent: a build blocker
            testProjectCount = @($member | Where-Object { $_.isTest }).Count
            styles          = @($member | ForEach-Object { $_.style } | Sort-Object -Unique)
        }
    }

    # --- git ------------------------------------------------------------------
    $git = [ordered]@{ isRepository = $false; remote = $null; branch = $null }
    if (Test-Path -LiteralPath (Join-Path $root '.git')) {
        $git.isRepository = $true
        $git.remote = (& git -C $root remote get-url origin 2>$null)
        $git.branch = (& git -C $root rev-parse --abbrev-ref HEAD 2>$null)
    } else {
        Add-AgentWarning -Agent $agent -Message 'Not a git repository - GitAgent and PRAgent cannot run. Clone it with git instead of downloading a zip.'
    }

    $sdkCount = @($projects.Values | Where-Object { $_.style -eq 'sdk' }).Count
    $legacyCount = $projects.Count - $sdkCount
    if ($sdkCount -eq 0 -and $projects.Count -gt 0) {
        Add-AgentWarning -Agent $agent -Message 'Every project is legacy MSBuild format; builds must use MSBuild.exe, not `dotnet build`.'
    }

    $data = [ordered]@{
        repoPath       = $root
        scannedAt      = (Get-Date).ToUniversalTime().ToString('o')
        git            = $git
        totals         = [ordered]@{
            solutions     = $solutions.Count
            projects      = $projects.Count
            legacyProjects = $legacyCount
            sdkProjects   = $sdkCount
            testProjects  = @($projects.Values | Where-Object { $_.isTest }).Count
            sourceFiles   = (@($counts['.cs']) + @($counts['.vb']) | Measure-Object -Sum).Sum
            sourceMB      = [math]::Round($sourceBytes / 1MB, 1)
        }
        requiresMSBuild = ($legacyCount -gt 0)
        frameworks     = @($projects.Values | ForEach-Object { $_.targetFramework } |
                            Where-Object { $_ } | Sort-Object -Unique)
        extensions     = [ordered]@{}
        solutions      = @($solutions | Sort-Object -Property projectCount -Descending)
        projects       = @($projects.Values)
    }
    foreach ($kv in ($counts.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 25)) {
        $data.extensions[$kv.Key] = $kv.Value
    }

    Write-Host "  legacy=$legacyCount sdk=$sdkCount tests=$($data.totals.testProjects) source=$($data.totals.sourceMB)MB"
    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
