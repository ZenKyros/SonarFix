<#
    IssueCollectionAgent - pull every open issue from SonarQube.

    Replaces GetIssues.ps1, which had three defects: a stray backslash in
    [Convert\]::ToBase64String made it fail to parse, ps=500 silently dropped
    everything past the first page, and it kept resolved issues.

    SonarQube caps paging at 10000 results, so large projects are fetched in
    severity slices to stay under that ceiling.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [Parameter(Mandatory)][string]$ProjectKey,
    [string]$SonarUrl = 'http://localhost:9000',
    [string]$SonarToken = $env:SONAR_TOKEN,
    [string[]]$Severities = @('BLOCKER', 'CRITICAL', 'MAJOR', 'MINOR', 'INFO'),
    [int]$PageSize = 500
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '06-IssueCollectionAgent' -RunDir $RunDir `
    -Inputs @{ projectKey = $ProjectKey; sonarUrl = $SonarUrl; severities = $Severities }

try {
    if (-not $SonarToken) { throw 'No SonarQube token. Pass -SonarToken or set $env:SONAR_TOKEN.' }

    # SonarQube accepts the token as the basic-auth username with an empty
    # password. Build the header once.
    $basic = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${SonarToken}:"))
    $headers = @{ Authorization = "Basic $basic" }

    $all = [Collections.ArrayList]::new()
    $seen = [Collections.Generic.HashSet[string]]::new()

    foreach ($severity in $Severities) {
        $page = 1
        do {
            $uri = "$SonarUrl/api/issues/search" +
                   "?componentKeys=$([uri]::EscapeDataString($ProjectKey))" +
                   "&severities=$severity&statuses=OPEN,CONFIRMED,REOPENED" +
                   "&ps=$PageSize&p=$page"
            try {
                $resp = Invoke-RestMethod -Uri $uri -Headers $headers -TimeoutSec 60
            } catch {
                throw "SonarQube query failed for $severity page $page : $($_.Exception.Message)"
            }
            foreach ($issue in $resp.issues) {
                if ($seen.Add($issue.key)) { [void]$all.Add($issue) }
            }
            $total = $resp.paging.total
            $fetched = $page * $PageSize
            $page++
            # 10000 is the server's hard paging ceiling.
            $more = $fetched -lt $total -and $fetched -lt 10000
        } while ($more)

        if ($total -gt 10000) {
            Add-AgentWarning -Agent $agent -Message "$severity has $total issues; only the first 10000 are reachable through paging."
        }
        if ($total -gt 0) { Write-Host "    $severity : $total" }
    }

    if ($all.Count -eq 0) {
        Add-AgentWarning -Agent $agent -Message "No open issues for '$ProjectKey'. Check the project key and that analysis finished."
    }

    # Persist the raw dump; the Python bridge imports this file verbatim.
    $issuesPath = Join-Path $RunDir 'issues-raw.json'
    @{ issues = @($all) } | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $issuesPath -Encoding UTF8

    $bySeverity = @{}
    $byRule = @{}
    foreach ($i in $all) {
        $s = $i.severity; $r = $i.rule
        if ($s) { $bySeverity[$s] = 1 + ($(if ($bySeverity.ContainsKey($s)) { $bySeverity[$s] } else { 0 })) }
        if ($r) { $byRule[$r] = 1 + ($(if ($byRule.ContainsKey($r)) { $byRule[$r] } else { 0 })) }
    }
    $topRules = @(
        $byRule.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 15 |
            ForEach-Object { [pscustomobject]@{ rule = $_.Key; count = $_.Value } }
    )

    Write-Host "  $($all.Count) open issue(s) across $($byRule.Count) rule(s)"

    $data = [ordered]@{
        projectKey  = $ProjectKey
        totalIssues = $all.Count
        uniqueRules = $byRule.Count
        bySeverity  = $bySeverity
        topRules    = $topRules
        issuesFile  = 'issues-raw.json'
    }
    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
