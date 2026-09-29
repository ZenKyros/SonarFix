<#
    PRAgent - push the fix branch and open a Bitbucket pull request.

    CreatePR.ps1 only wrote a payload file and never called Bitbucket, and it
    used the Cloud schema; this server is Bitbucket Data Center, which takes
    fromRef/toRef under /rest/api/1.0/. The push and the API call are done by
    the Python client, which is covered by tests against that exact payload.

    Three gates must pass, because this is the step that leaves the machine:
      - the build verification gate passed
      - the secret scan found nothing
      - -Confirm was given

    The pull request is opened for review. Nothing is ever merged.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$RunDir,
    [Parameter(Mandatory)][string]$ProjectKey,
    [switch]$Confirm,
    [switch]$IgnoreBuildGate
)

. "$PSScriptRoot\_Common.ps1"
$agent = Start-Agent -Name '11-PRAgent' -RunDir $RunDir `
    -Inputs @{ projectKey = $ProjectKey; confirmed = [bool]$Confirm }

try {
    $fix = Read-AgentOutput -RunDir $RunDir -Name '08-FixGenerationAgent'
    $git = Read-AgentOutput -RunDir $RunDir -Name '10-GitAgent'
    $verify = Read-AgentOutput -RunDir $RunDir -Name '09-BuildVerificationAgent' -Optional

    if (-not $fix.applied) { throw 'No fixes were applied, so there is nothing to publish.' }
    if (-not $git.safeToPush) { throw 'GitAgent flagged a possible secret in the diff. Resolve that before pushing.' }

    if ($verify -and $verify.PSObject.Properties.Name -contains 'passesGate') {
        if (-not $verify.passesGate -and -not $IgnoreBuildGate) {
            throw "Build verification failed ($($verify.failed) project(s)). Fix the branch, or pass -IgnoreBuildGate to override deliberately."
        }
        if (-not $verify.passesGate) {
            Add-AgentWarning -Agent $agent -Message 'Opening a pull request despite a failed build, because -IgnoreBuildGate was given.'
        }
    } else {
        Add-AgentWarning -Agent $agent -Message 'No build verification for this branch; reviewers will have to compile it themselves.'
    }

    # Check Bitbucket is configured before claiming anything will work.
    $scmStatus = Invoke-SonarFixPython -Arguments @('scm-status', '--project', $ProjectKey)
    if (-not $scmStatus.ready) { throw "Bitbucket is not ready: $($scmStatus.reason)" }
    Write-Host "  target: $($scmStatus.provider) $($scmStatus.repository)"
    Write-Host "  branch: $($git.branch) -> $($git.baseBranch), $($git.commitCount) commit(s)"

    if (-not $Confirm) {
        Write-Host ''
        Write-Host '  dry run - nothing pushed' -ForegroundColor Cyan
        Write-Host '  re-run with -Confirm to push the branch and open the pull request' -ForegroundColor DarkGray
        $data = [ordered]@{
            dryRun = $true; branch = $git.branch; baseBranch = $git.baseBranch
            provider = $scmStatus.provider; repository = $scmStatus.repository; prUrl = $null
        }
        exit (Complete-Agent -Agent $agent -Data $data)
    }

    Write-Host '  pushing and opening the pull request'
    $pr = Invoke-SonarFixPython -Arguments @('pull-request', '--batch', "$($fix.batchId)")
    if (-not $pr.ok) { throw "Bitbucket refused the pull request: $($pr.error)" }

    Write-Host "  pull request open: $($pr.prUrl)" -ForegroundColor Green
    Write-Host '  waiting for human review - nothing is merged automatically' -ForegroundColor DarkGray

    $data = [ordered]@{
        dryRun     = $false
        batchId    = $pr.batchId
        branch     = $pr.branch
        baseBranch = $git.baseBranch
        provider   = $scmStatus.provider
        repository = $scmStatus.repository
        prUrl      = $pr.prUrl
        merged     = $false
    }
    exit (Complete-Agent -Agent $agent -Data $data)
} catch {
    Add-AgentError -Agent $agent -Message (Format-AgentError -ErrorRecord $_)
    exit (Complete-Agent -Agent $agent -Data @{} -Status 'failed')
}
