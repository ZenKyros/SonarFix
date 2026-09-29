<#
    GitAgent - report on the branch the fixes were committed to.

    The fix branch, its commits and the diff are produced by the Python core
    (it commits each mechanical and AI step separately). This agent inspects
    that result and applies the safety checks that must hold before anything
    is pushed:

      - the branch exists and is not the base branch
      - nothing was committed to a protected branch
      - the diff contains no obvious secret

    It never pushes. Pushing belongs to PRAgent, behind its own gate.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [string[]]$ProtectedBranches = @('main', 'master', 'develop', 'release')
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '10-GitAgent' -RunDir $RunDir -Inputs @{ protectedBranches = $ProtectedBranches }

try {
    $discovery = Read-AgentOutput -RunDir $RunDir -Name '01-RepoDiscoveryAgent'
    $fix = Read-AgentOutput -RunDir $RunDir -Name '08-FixGenerationAgent'
    $repo = $discovery.repoPath

    if (-not $fix.applied) {
        Write-Host '  no fixes applied; no branch to inspect'
        exit (Complete-Agent -Agent $agent -Data ([ordered]@{ skipped = $true; reason = 'no fixes applied' }))
    }

    $branch = $fix.branch
    $base = $fix.baseBranch
    $exists = & git -C $repo rev-parse --verify --quiet "refs/heads/$branch" 2>$null
    if (-not $exists) { throw "Branch '$branch' does not exist in $repo." }

    if ($branch -in $ProtectedBranches) {
        throw "Refusing to continue: fixes landed on protected branch '$branch'."
    }

    $commits = @(& git -C $repo log --format='%H%x1f%s' "$base..$branch" 2>$null | ForEach-Object {
        $parts = $_ -split "`u{1f}"
        [pscustomobject][ordered]@{ sha = $parts[0].Substring(0, 8); subject = $parts[1] }
    })
    $stat = & git -C $repo diff --shortstat "$base...$branch" 2>$null
    $files = @(& git -C $repo diff --name-only "$base...$branch" 2>$null | Where-Object { $_ })

    # Cheap secret scan over added lines only. Not a replacement for a real
    # scanner, but it catches the obvious mistake before it leaves the machine.
    $added = @(& git -C $repo diff "$base...$branch" 2>$null |
        Where-Object { $_.StartsWith('+') -and -not $_.StartsWith('+++') })
    $patterns = @{
        'private key'     = '-----BEGIN [A-Z ]*PRIVATE KEY-----'
        'AWS access key'  = '\bAKIA[0-9A-Z]{16}\b'
        'connection password' = '(?i)(password|pwd)\s*=\s*[^;''"\s]{6,}'
        'bearer token'    = '(?i)\b(bearer|api[_-]?key|secret)\b\s*[:=]\s*[''"][^''"]{16,}'
    }
    # @() so an empty result still has .Count under StrictMode.
    $suspects = @(foreach ($kv in $patterns.GetEnumerator()) {
        foreach ($line in $added) {
            if ($line -match $kv.Value) {
                [pscustomobject]@{ kind = $kv.Key; line = $line.Trim().Substring(0, [Math]::Min(120, $line.Trim().Length)) }
                break
            }
        }
    })
    if ($suspects.Count -gt 0) {
        foreach ($s in $suspects) { Add-AgentError -Agent $agent -Message "Possible $($s.kind) in the diff - review before pushing." }
    }

    Write-Host "  branch $branch : $($commits.Count) commit(s), $($files.Count) file(s)"
    Write-Host "  $stat".TrimEnd()

    $clean = $suspects.Count -eq 0
    $data = [ordered]@{
        branch        = $branch
        baseBranch    = $base
        commits       = $commits
        commitCount   = $commits.Count
        changedFiles  = $files
        shortstat     = "$stat".Trim()
        secretSuspects = @($suspects)
        safeToPush    = $clean
    }
    exit (Complete-Agent -Agent $agent -Data $data -Status $(if ($clean) { 'ok' } else { 'partial' }))
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
