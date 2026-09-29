<#
    ContextCollectionAgent - assemble the smallest context that still lets a
    fix be written correctly.

    This is the agent that enforces "never send the entire repository". The
    old CollectContext.ps1 read whole files, so one 4000-line legacy class
    would dominate a prompt on its own.

    For each issue it sends:
      - the enclosing method or property, found by brace matching
      - the file's using directives and the declaring type's signature
      - declaration lines only for repository types the snippet references
      - the owning project and its direct dependencies, as names

    Everything is capped, and the cap is reported so the cost is visible.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [int]$MaxIssues = 200,
    [int]$MaxMemberLines = 120,
    [int]$MaxBytesPerIssue = 8000
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '07-ContextCollectionAgent' -RunDir $RunDir `
    -Inputs @{ maxIssues = $MaxIssues; maxMemberLines = $MaxMemberLines; maxBytesPerIssue = $MaxBytesPerIssue }

try {
    $discovery = Read-AgentOutput -RunDir $RunDir -Name '01-RepoDiscoveryAgent'
    $collection = Read-AgentOutput -RunDir $RunDir -Name '06-IssueCollectionAgent'
    $graph = Read-AgentOutput -RunDir $RunDir -Name '03-DependencyGraphAgent' -Optional

    $repo = $discovery.repoPath
    $issues = (Get-Content -LiteralPath (Join-Path $RunDir $collection.issuesFile) -Raw -Encoding UTF8 |
        ConvertFrom-Json).issues

    # Map a source file to the project that compiles it, by longest matching
    # directory prefix - the project nearest the file owns it.
    $projectDirs = foreach ($p in $discovery.projects) {
        [pscustomobject]@{ dir = (Split-Path -Parent $p.path) -replace '\\', '/'; project = $p }
    }
    $projectDirs = @($projectDirs | Sort-Object { $_.dir.Length } -Descending)

    $graphByPath = @{}
    if ($graph) { foreach ($n in $graph.nodes) { $graphByPath[$n.path] = $n } }

    $fileCache = @{}
    function Get-Lines {
        param([string]$Rel)
        if ($fileCache.ContainsKey($Rel)) { return $fileCache[$Rel] }
        $full = Join-Path $repo ($Rel -replace '/', '\')
        $lines = if (Test-Path -LiteralPath $full) {
            [IO.File]::ReadAllLines($full)
        } else { $null }
        $fileCache[$Rel] = $lines
        $fileCache[$Rel]
    }

    <#
        Walks outward from the reported line to the enclosing member: back to
        a signature line, then forward matching braces. Falls back to a fixed
        window when the shape is not recognisable (generated or minified code).
    #>
    function Get-EnclosingMember {
        param([string[]]$Lines, [int]$Line)
        $i = [Math]::Min([Math]::Max($Line - 1, 0), $Lines.Count - 1)

        $signature = -1
        $limit = [Math]::Max(0, $i - 400)
        for ($j = $i; $j -ge $limit; $j--) {
            $t = $Lines[$j].Trim()
            if ($t -match '^\s*(\[[^\]]+\]\s*)*((public|private|protected|internal|static|virtual|override|sealed|async|partial|extern|unsafe)\s+)+[\w<>\[\],\.\?]+\s+[\w<>]+\s*\(' -or
                $t -match '^\s*((public|private|protected|internal|static)\s+)+[\w<>\[\],\.\?]+\s+[\w]+\s*\{\s*(get|set)') {
                $signature = $j
                break
            }
        }
        if ($signature -lt 0) {
            $start = [Math]::Max(0, $i - 20)
            $end = [Math]::Min($Lines.Count - 1, $i + 20)
            return [pscustomobject]@{ start = $start; end = $end; kind = 'window' }
        }

        # Match braces from the signature to the member's closing brace.
        $depth = 0; $opened = $false; $end = $signature
        for ($j = $signature; $j -lt [Math]::Min($Lines.Count, $signature + $MaxMemberLines * 3); $j++) {
            foreach ($ch in $Lines[$j].ToCharArray()) {
                if ($ch -eq '{') { $depth++; $opened = $true }
                elseif ($ch -eq '}') { $depth-- }
            }
            $end = $j
            if ($opened -and $depth -le 0) { break }
            # Expression-bodied or abstract member: ends at the semicolon.
            if (-not $opened -and $Lines[$j] -match ';\s*$') { break }
        }
        if (($end - $signature) -gt $MaxMemberLines) { $end = $signature + $MaxMemberLines }
        [pscustomobject]@{ start = $signature; end = $end; kind = 'member' }
    }

    $selected = @($issues | Select-Object -First $MaxIssues)
    if ($issues.Count -gt $MaxIssues) {
        Add-AgentWarning -Agent $agent -Message "Context built for the first $MaxIssues of $($issues.Count) issues; raise -MaxIssues to cover more."
    }

    $totalBytes = 0
    $truncated = 0
    $contexts = foreach ($issue in $selected) {
        # component is "projectKey:relative/path"
        $rel = $issue.component
        if ($rel -match '^[^:]+:(.+)$') { $rel = $Matches[1] }
        # @() normalises: PowerShell unrolls a one-line file to a bare string,
        # which has no .Count under StrictMode.
        $lines = Get-Lines -Rel $rel
        if ($null -ne $lines) { $lines = @($lines) }

        $owner = @($projectDirs | Where-Object { $rel.StartsWith($_.dir + '/', [StringComparison]::OrdinalIgnoreCase) } |
            Select-Object -First 1)
        $ownerProject = if ($owner.Count -gt 0) { $owner[0].project } else { $null }

        $entry = [ordered]@{
            issueKey   = $issue.key
            rule       = $issue.rule
            severity   = $issue.severity
            type       = $issue.type
            message    = $issue.message
            file       = $rel
            line       = $issue.line
            available  = $null -ne $lines
            project    = if ($ownerProject) { $ownerProject.path } else { $null }
            projectName = if ($ownerProject) { $ownerProject.name } else { $null }
            dependsOn  = @()
            usings     = @()
            declaringType = $null
            snippet    = $null
            snippetKind = $null
            snippetRange = $null
            relatedDeclarations = @()
            bytes      = 0
        }

        if (-not $lines) {
            $entry.snippet = $null
            Add-AgentWarning -Agent $agent -Message "File missing from the clone: $rel"
            [pscustomobject]$entry
            continue
        }

        # Direct dependencies only - names, never their source.
        if ($ownerProject -and $graphByPath.ContainsKey($ownerProject.path)) {
            $entry.dependsOn = @($graphByPath[$ownerProject.path].dependsOn | Select-Object -First 15)
        }

        $region = Get-EnclosingMember -Lines $lines -Line ([int]$issue.line)
        $body = $lines[$region.start..$region.end] -join "`n"
        $entry.snippetKind = $region.kind
        # Parenthesised: the comma operator binds tighter than +, so
        # @($a + 1, $b + 1) would parse as $a + (1, $b) + 1.
        $entry.snippetRange = @(($region.start + 1), ($region.end + 1))

        # using directives are small and prevent the fixer inventing imports.
        $entry.usings = @(
            $lines | Select-Object -First 60 |
                Where-Object { $_ -match '^\s*using\s+[\w\.]+\s*;' } |
                ForEach-Object { $_.Trim() } | Select-Object -First 25
        )
        foreach ($j in 0..([Math]::Min($region.start, $lines.Count - 1))) {
            if ($lines[$j] -match '^\s*(public|internal|private|protected|abstract|sealed|static|partial)[\w\s]*\b(class|struct|interface|record|enum)\s+\w+') {
                $entry.declaringType = $lines[$j].Trim()
            }
        }

        # Declaration lines for repository types this snippet mentions: enough
        # to fix a call, without shipping the other file.
        $related = [Collections.ArrayList]::new()
        if ($ownerProject) {
            $names = [Collections.Generic.HashSet[string]]::new()
            foreach ($m in [regex]::Matches($body, '\b([A-Z][A-Za-z0-9_]{2,})\b')) { [void]$names.Add($m.Groups[1].Value) }
            foreach ($dep in @($entry.dependsOn | Select-Object -First 5)) {
                $depNode = $discovery.projects | Where-Object { $_.path -eq $dep } | Select-Object -First 1
                if (-not $depNode) { continue }
                if ($names.Contains($depNode.assemblyName) -or $names.Contains($depNode.name)) {
                    [void]$related.Add([pscustomobject]@{ project = $dep; assembly = $depNode.assemblyName })
                }
            }
        }
        $entry.relatedDeclarations = @($related | Select-Object -First 8)

        # Hard byte cap: the prompt cost must be bounded per issue.
        if ($body.Length -gt $MaxBytesPerIssue) {
            $body = $body.Substring(0, $MaxBytesPerIssue) + "`n// ... truncated by ContextCollectionAgent"
            $truncated++
        }
        $entry.snippet = $body
        $entry.bytes = $body.Length
        $totalBytes += $body.Length
        [pscustomobject]$entry
    }

    $withCode = @($contexts | Where-Object { $_.available })
    $avg = if ($withCode.Count) { [math]::Round($totalBytes / $withCode.Count) } else { 0 }
    Write-Host "  context for $($contexts.Count) issue(s): $([math]::Round($totalBytes/1KB,1))KB total, ~$avg bytes each"
    Write-Host "  whole-repo source is $($discovery.totals.sourceMB)MB - this is the saving" -ForegroundColor DarkGray

    $contextPath = Join-Path $RunDir 'issue-contexts.json'
    @{ contexts = @($contexts) } | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $contextPath -Encoding UTF8

    $data = [ordered]@{
        issueCount     = $contexts.Count
        withSource     = $withCode.Count
        missingSource  = $contexts.Count - $withCode.Count
        truncated      = $truncated
        totalBytes     = $totalBytes
        averageBytes   = $avg
        repoSourceMB   = $discovery.totals.sourceMB
        contextsFile   = 'issue-contexts.json'
    }
    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
