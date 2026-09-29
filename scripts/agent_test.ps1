<#
    End-to-end test of the agent pipeline and its handoff to the Python core.

    Builds a throwaway C# git repository and a mock SonarQube issue file, then
    runs agents 01-11. SonarQube itself is not needed: 05-SonarAgent and
    06-IssueCollectionAgent are the only network steps, and the issue file
    stands in for them.

    Proves the parts that matter: recipes fix C# without tokens, AI never runs
    unapproved, context stays small, the blast radius is computed from the
    dependency graph, and the pull request is gated.

        pwsh -File scripts/agent_test.ps1
#>
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$agents = Join-Path $root 'agents'
$work = Join-Path ([IO.Path]::GetTempPath()) ("sonarfix-agents-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$repo = Join-Path $work 'repo'
$runDir = Join-Path $work 'run'
$project = 'AgentTest_CK'

$checks = [Collections.ArrayList]::new()
function Check {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    [void]$checks.Add([pscustomobject]@{ Name = $Name; Ok = $Ok; Detail = $Detail })
}
function Get-StageData {
    param([string]$Name)
    $p = Join-Path $runDir "$Name.json"
    if (-not (Test-Path $p)) { return $null }
    (Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json).data
}

# --- fixture ------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path (Join-Path $repo 'src/Core'), (Join-Path $repo 'src/Api'), (Join-Path $repo 'vendor/Acme') | Out-Null

# Core library: three issues a recipe can fix deterministically.
@'
using System;
using System.Text;

namespace Core
{
    public class Calculator
    {
        public int Total(int[] values)
        {
            var sum = 0;
            foreach (var v in values) { sum += v; }
            // sum = Recalculate(values);
            return sum;
        }

        public void Run()
        {
            try { Total(new int[0]); }
            catch (Exception ex)
            {
                Console.WriteLine(ex.Message);
                throw ex;
            }
        }
    }
}
'@ | Set-Content -LiteralPath (Join-Path $repo 'src/Core/Calculator.cs') -Encoding UTF8

@'
<?xml version="1.0" encoding="utf-8"?>
<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003" ToolsVersion="12.0">
  <PropertyGroup>
    <OutputType>Library</OutputType>
    <AssemblyName>Core</AssemblyName>
    <TargetFrameworkVersion>v4.8</TargetFrameworkVersion>
  </PropertyGroup>
  <ItemGroup><Compile Include="Calculator.cs" /></ItemGroup>
</Project>
'@ | Set-Content -LiteralPath (Join-Path $repo 'src/Core/Core.csproj') -Encoding UTF8

# Api depends on Core by assembly name: the shared-DLL edge case.
@'
using System;

namespace Api
{
    public class Service
    {
        public int Compute()
        {
            var calc = new Core.Calculator();
            return calc.Total(new[] { 1, 2, 3 });
        }
    }
}
'@ | Set-Content -LiteralPath (Join-Path $repo 'src/Api/Service.cs') -Encoding UTF8

@'
<?xml version="1.0" encoding="utf-8"?>
<Project DefaultTargets="Build" xmlns="http://schemas.microsoft.com/developer/msbuild/2003" ToolsVersion="12.0">
  <PropertyGroup>
    <OutputType>Library</OutputType>
    <AssemblyName>Api</AssemblyName>
    <TargetFrameworkVersion>v4.8</TargetFrameworkVersion>
  </PropertyGroup>
  <ItemGroup><Reference Include="Core" /></ItemGroup>
  <ItemGroup><Compile Include="Service.cs" /></ItemGroup>
</Project>
'@ | Set-Content -LiteralPath (Join-Path $repo 'src/Api/Api.csproj') -Encoding UTF8

# Vendored code: must be skipped, never fixed.
'using System; using System.Text; class Vendored { }' |
    Set-Content -LiteralPath (Join-Path $repo 'vendor/Acme/Vendored.cs') -Encoding UTF8

@'
Microsoft Visual Studio Solution File, Format Version 12.00
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Core", "src\Core\Core.csproj", "{A1}"
EndProject
Project("{FAE04EC0-301F-11D3-BF4B-00C04F79EFBC}") = "Api", "src\Api\Api.csproj", "{A2}"
EndProject
'@ | Set-Content -LiteralPath (Join-Path $repo 'App.sln') -Encoding UTF8

Push-Location $repo
git init -q -b main 2>&1 | Out-Null
git config user.email 'test@example.com'; git config user.name 'Agent Test'
git config core.autocrlf false
git add -A 2>&1 | Out-Null
git commit -q -m 'initial' 2>&1 | Out-Null
git remote add origin 'https://bitbucket.example.com/scm/DEMO/app.git'
Pop-Location

# --- mock Sonar issues ---------------------------------------------------------
$issues = @(
    @{ key = 'I-using'; rule = 'csharpsquid:S1128'; severity = 'MINOR'; type = 'CODE_SMELL'; status = 'OPEN'
       message = "Remove this unnecessary 'using'."; component = "${project}:src/Core/Calculator.cs"; line = 2
       textRange = @{ startLine = 2; endLine = 2; startOffset = 0; endOffset = 18 } }
    @{ key = 'I-throw'; rule = 'csharpsquid:S3445'; severity = 'MAJOR'; type = 'CODE_SMELL'; status = 'OPEN'
       message = "Consider using 'throw;' to preserve the stack trace."
       component = "${project}:src/Core/Calculator.cs"; line = 22
       textRange = @{ startLine = 22; endLine = 22; startOffset = 16; endOffset = 25 } }
    @{ key = 'I-comment'; rule = 'csharpsquid:S125'; severity = 'MAJOR'; type = 'CODE_SMELL'; status = 'OPEN'
       message = 'Remove this commented out code.'; component = "${project}:src/Core/Calculator.cs"; line = 12
       textRange = @{ startLine = 12; endLine = 12; startOffset = 12; endOffset = 42 } }
    @{ key = 'I-complex'; rule = 'csharpsquid:S3776'; severity = 'CRITICAL'; type = 'CODE_SMELL'; status = 'OPEN'
       message = 'Refactor this method to reduce its Cognitive Complexity.'
       component = "${project}:src/Api/Service.cs"; line = 7
       textRange = @{ startLine = 7; endLine = 7; startOffset = 19; endOffset = 26 } }
    @{ key = 'I-vendor'; rule = 'csharpsquid:S1128'; severity = 'MINOR'; type = 'CODE_SMELL'; status = 'OPEN'
       message = "Remove this unnecessary 'using'."; component = "${project}:vendor/Acme/Vendored.cs"; line = 1
       textRange = @{ startLine = 1; endLine = 1; startOffset = 0; endOffset = 18 } }
    @{ key = 'I-closed'; rule = 'csharpsquid:S1128'; severity = 'MINOR'; type = 'CODE_SMELL'; status = 'CLOSED'
       message = 'Already fixed.'; component = "${project}:src/Core/Calculator.cs"; line = 1
       textRange = @{ startLine = 1; endLine = 1; startOffset = 0; endOffset = 12 } }
)
New-Item -ItemType Directory -Force -Path $runDir | Out-Null
@{ issues = $issues } | ConvertTo-Json -Depth 10 |
    Set-Content -LiteralPath (Join-Path $runDir 'issues-raw.json') -Encoding UTF8

Write-Host "workspace: $work" -ForegroundColor DarkGray

# --- 01-03 ---------------------------------------------------------------------
& pwsh -NoProfile -File (Join-Path $agents '01-RepoDiscoveryAgent.ps1') -RepoPath $repo -RunDir $runDir | Out-Null
$d = Get-StageData '01-RepoDiscoveryAgent'
Check 'discovery finds both projects' ($d.totals.projects -eq 2) "got $($d.totals.projects)"
Check 'discovery finds the solution' ($d.totals.solutions -eq 1)
Check 'vendored code excluded from inventory' (-not ($d.projects.path -match 'vendor'))
Check 'legacy style detected' ($d.requiresMSBuild -eq $true)
Check 'git remote detected' ($d.git.isRepository -and $d.git.remote -like '*bitbucket*')

& pwsh -NoProfile -File (Join-Path $agents '02-SolutionRankingAgent.ps1') -RunDir $runDir | Out-Null
$r = Get-StageData '02-SolutionRankingAgent'
Check 'solution ranked buildable' ($r.candidates.Count -eq 1 -and $r.candidates[0].buildable)

& pwsh -NoProfile -File (Join-Path $agents '03-DependencyGraphAgent.ps1') -RunDir $runDir | Out-Null
$g = Get-StageData '03-DependencyGraphAgent'
$core = $g.nodes | Where-Object { $_.assemblyName -eq 'Core' }
Check 'shared-DLL edge resolved (Api -> Core)' ($g.sharedDllEdges -ge 1 -and $core.usedBy -contains 'src/Api/Api.csproj') "edges=$($g.sharedDllEdges)"
Check 'blast radius computed' ($core.impactedCount -eq 1) "impacted=$($core.impactedCount)"
Check 'no false cycles' ($g.cycles.Count -eq 0)

# --- 06 stub (issue file already written), then 07 -----------------------------
@{
    agent = '06-IssueCollectionAgent'; version = '1.0'; status = 'ok'
    startedAt = (Get-Date).ToUniversalTime().ToString('o'); finishedAt = (Get-Date).ToUniversalTime().ToString('o')
    durationSec = 0; inputs = @{}; warnings = @(); errors = @()
    data = @{ projectKey = $project; totalIssues = $issues.Count; issuesFile = 'issues-raw.json' }
} | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $runDir '06-IssueCollectionAgent.json') -Encoding UTF8

& pwsh -NoProfile -File (Join-Path $agents '07-ContextCollectionAgent.ps1') -RunDir $runDir | Out-Null
$c = Get-StageData '07-ContextCollectionAgent'
$ctx = (Get-Content -LiteralPath (Join-Path $runDir 'issue-contexts.json') -Raw | ConvertFrom-Json).contexts
$throwCtx = $ctx | Where-Object { $_.issueKey -eq 'I-throw' }
Check 'context extracts the enclosing member' ($throwCtx.snippetKind -eq 'member' -and $throwCtx.snippet -match 'public void Run') $throwCtx.snippetKind
Check 'context excludes unrelated members' ($throwCtx.snippet -notmatch 'public int Total') 'Total leaked into the Run snippet'
Check 'context captures using directives' (@($throwCtx.usings).Count -ge 2)
Check 'context records the declaring type' ($throwCtx.declaringType -match 'class Calculator')
Check 'context maps the owning project' ($throwCtx.project -eq 'src/Core/Core.csproj') $throwCtx.project
Check 'context stays small' ($c.averageBytes -lt 2000) "avg=$($c.averageBytes) bytes"

# --- 08 plan-only: must spend nothing ------------------------------------------
& pwsh -NoProfile -File (Join-Path $agents '08-FixGenerationAgent.ps1') -RunDir $runDir -ProjectKey $project -PlanOnly | Out-Null
$p = Get-StageData '08-FixGenerationAgent'
Check 'closed issue not imported' ($p.imported -eq 5) "imported=$($p.imported)"
Check 'plan routes 3 issues to recipes' ($p.summary.mechanical -eq 3) "mechanical=$($p.summary.mechanical)"
Check 'plan routes complexity to AI' ($p.summary.ai -eq 1) "ai=$($p.summary.ai)"
Check 'plan skips vendored code' ($p.summary.skip -eq 1) "skip=$($p.summary.skip)"
Check 'plan-only changes nothing' ($p.applied -eq $false)
Check 'cost model reports zero-token count' ($p.costModel.mechanicalTokens -eq 0)

$before = & git -C $repo rev-parse HEAD
Check 'plan-only leaves no commits' ((& git -C $repo rev-list --count HEAD) -eq '1')

# --- 08 apply mechanical only: still no AI -------------------------------------
& pwsh -NoProfile -File (Join-Path $agents '08-FixGenerationAgent.ps1') -RunDir $runDir -ProjectKey $project -ApplyMechanical | Out-Null
$p2 = Get-StageData '08-FixGenerationAgent'
Check 'mechanical fixes applied' ($p2.applied -and $p2.mechanicalFixes -eq 3) "applied=$($p2.mechanicalFixes)"
Check 'no AI session used' ($p2.aiSessionsUsed -eq 0) "sessions=$($p2.aiSessionsUsed)"

# Join: git returns lines, and -match on an array yields matches, not a bool.
$fixed = (& git -C $repo show "$($p2.branch):src/Core/Calculator.cs") -join "`n"
Check 'unused using removed' ($fixed -notmatch 'using System.Text;')
Check 'needed using kept' ($fixed -match 'using System;')
Check 'throw ex; rewritten to throw;' (($fixed -match '\bthrow;') -and ($fixed -notmatch 'throw ex;'))
Check 'commented-out code deleted' ($fixed -notmatch 'Recalculate')
Check 'real code untouched' ($fixed -match 'sum \+= v;')
$vendored = Get-Content -LiteralPath (Join-Path $repo 'vendor/Acme/Vendored.cs') -Raw
Check 'vendored file never modified' ($vendored -match 'using System.Text;')
Check 'working tree returned to base' ((& git -C $repo rev-parse --abbrev-ref HEAD).Trim() -eq 'main')

# --- 09 verification uses the blast radius -------------------------------------
& pwsh -NoProfile -File (Join-Path $agents '09-BuildVerificationAgent.ps1') -RunDir $runDir -MaxProjects 5 | Out-Null
$v = Get-StageData '09-BuildVerificationAgent'
Check 'verification finds the changed file' ($v.changedFiles -eq 1) "changed=$($v.changedFiles)"
Check 'verification picks the owning project' ($v.owningProjects -contains 'src/Core/Core.csproj')
Check 'verification includes the consumer' ($v.blastRadius -eq 2) "radius=$($v.blastRadius)"

# --- 10 git inspection ---------------------------------------------------------
& pwsh -NoProfile -File (Join-Path $agents '10-GitAgent.ps1') -RunDir $runDir | Out-Null
$gi = Get-StageData '10-GitAgent'
Check 'git agent sees one commit' ($gi.commitCount -eq 1) "commits=$($gi.commitCount)"
Check 'secret scan clean' ($gi.safeToPush -eq $true)
Check 'branch is not protected' ($gi.branch -notin @('main', 'master'))

# --- 11 PR is gated ------------------------------------------------------------
# The synthetic projects do not compile (no .NET Framework targeting pack), so
# the build gate must refuse to publish. That refusal is the check.
& pwsh -NoProfile -File (Join-Path $agents '11-PRAgent.ps1') -RunDir $runDir -ProjectKey $project 2>&1 | Out-Null
$blocked = (Get-Content -LiteralPath (Join-Path $runDir '11-PRAgent.json') -Raw | ConvertFrom-Json)
Check 'failed build blocks the pull request' ($blocked.status -eq 'failed' -and ($blocked.errors -join ' ') -match 'Build verification failed')

# With the gate deliberately overridden, it must still be a dry run.
& pwsh -NoProfile -File (Join-Path $agents '11-PRAgent.ps1') -RunDir $runDir -ProjectKey $project -IgnoreBuildGate 2>&1 | Out-Null
$pr = Get-StageData '11-PRAgent'
Check 'dry run without -Confirm' ($null -ne $pr -and $pr.dryRun -eq $true -and $null -eq $pr.prUrl)
Check 'dry run still targets Bitbucket' ($pr.provider -like 'bitbucket*' -and $pr.repository -eq 'DEMO/app') "$($pr.provider) $($pr.repository)"
$remoteBranches = (& git -C $repo branch -r 2>$null) -join ''
Check 'nothing pushed during dry run' ([string]::IsNullOrWhiteSpace($remoteBranches))

# --- results -------------------------------------------------------------------
Write-Host ''
$width = ($checks.Name | Measure-Object -Property Length -Maximum).Maximum
$failed = 0
foreach ($c in $checks) {
    if ($c.Ok) { Write-Host ("PASS  {0}" -f $c.Name.PadRight($width)) -ForegroundColor Green }
    else { $failed++; Write-Host ("FAIL  {0}  {1}" -f $c.Name.PadRight($width), $c.Detail) -ForegroundColor Red }
}
Write-Host ''
Write-Host "$($checks.Count - $failed)/$($checks.Count) checks passed" -ForegroundColor $(if ($failed) { 'Red' } else { 'Green' })
if ($failed -eq 0) { Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue }
else { Write-Host "workspace kept for inspection: $work" -ForegroundColor Yellow }
exit $(if ($failed) { 1 } else { 0 })
