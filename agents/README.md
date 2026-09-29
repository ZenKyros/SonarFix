# SonarFix agents

Claude decides, PowerShell acts, SonarQube observes, Bitbucket receives.

PowerShell handles everything .NET-specific — discovery, MSBuild, Sonar
scanning. The decision about *what to change* is delegated to the Python core,
which is covered by tests. Claude never scans, builds, or calls Bitbucket
directly.

## The one command you need

Fix the highest-priority issue and open a pull request for it:

```powershell
$env:SONAR_TOKEN = '<token from http://localhost:9000/account/security>'

# once per repository - inventory, ranking, dependency graph
.\Invoke-SonarFix.ps1 -RepoPath C:\src\repo -ProjectKey InfoImage_CK -Stage survey

# then, one issue at a time
.\Fix-OneIssue.ps1 -RunDir .sonarfix\runs\<run-id> -ProjectKey InfoImage_CK
```

That prints the priority queue and stops. Nothing is changed and nothing is
spent. Then:

| Add | What happens |
|---|---|
| *(nothing)* | Report only |
| `-Apply` | Fix it on a branch, verify the build. No push. |
| `-CreatePR` | Fix, verify, push, open the pull request |
| `-IssueKey MOCK-3` | Fix that issue instead of the top one |
| `-RefreshIssues` | Re-query SonarQube instead of using the cache |

Run it again for the next issue.

## How priority is decided

Computed from the survey, not guessed:

```
  type       VULNERABILITY 100 · SECURITY_HOTSPOT 80 · BUG 50 · CODE_SMELL 0
+ severity   BLOCKER 50 · CRITICAL 35 · MAJOR 20 · MINOR 8 · INFO 2
+ 25         a deterministic recipe exists (safe, and costs nothing)
- blast      dependent projects ÷ 20, capped at 25
- 30         the file belongs to a test project
```

Blast radius matters. Two identical `BLOCKER` vulnerabilities score
differently if one sits in an assembly 521 projects consume — that one is
riskier to change unattended, so it ranks lower.

Vendored, generated and build-output files are dropped before ranking. Those
are scan-configuration problems, not code to change.

## Cost

- A rule with a recipe is fixed deterministically — **zero tokens**.
- Anything else gets **one AI session for that one issue**.
- Context sent is the enclosing method plus the file's `using` directives,
  never the whole file and never the repository.

## The agents

| Agent | Does | Needs |
|---|---|---|
| `01-RepoDiscoveryAgent` | Inventory: solutions, projects, references | — |
| `02-SolutionRankingAgent` | Which solutions are worth building | 01 |
| `03-DependencyGraphAgent` | Dependency graph and blast radius | 01 |
| `04-BuildValidationAgent` | Compile the ranked candidates | 01, 02 |
| `05-SonarAgent` | `begin` → MSBuild rebuild → `end` | 04, SonarQube |
| `06-IssueCollectionAgent` | Download every open issue, paged | SonarQube |
| `07-ContextCollectionAgent` | Smallest useful context per issue | 01, 06 |
| `08-FixGenerationAgent` | Group, route, apply (the fusion point) | 01, 06 |
| `09-BuildVerificationAgent` | Rebuild only the blast radius | 03, 08 |
| `10-GitAgent` | Inspect the branch, scan for secrets | 08 |
| `11-PRAgent` | Push and open the pull request | 09, 10 |
| `Invoke-SonarFix.ps1` | Runs the above, resumable, staged | — |
| `Fix-OneIssue.ps1` | One issue, end to end | 01, 03 |

Every agent takes `-RunDir`, reads the JSON of the agents before it, and
writes one envelope: `{agent, status, startedAt, durationSec, warnings,
errors, data}`. A stage whose output already exists is skipped unless
`-Force`, so a slow build is never repeated because a later step failed.

## Gates

Nothing irreversible happens without an explicit flag.

1. **AI** — never runs unless a group is named and approved.
2. **Build** — a branch that does not compile cannot become a pull request
   (override with `-IgnoreBuildGate`).
3. **Secrets** — the diff is scanned before any push.
4. **Push** — requires `-Confirm`. Pull requests are opened for review and
   **never merged**.

## This repository

Measured on the InfoImage tree (1 GB, 144 solutions, 861 projects):

- Discovery takes about 20 seconds.
- **All 860 projects are legacy MSBuild-2003 format.** `dotnet build` cannot
  load them, so every build uses `MSBuild.exe`, located through `vswhere`.
- `CAL.Net` is consumed by 521 projects and `Common` by 457. Changes there
  have a large blast radius and are ranked down accordingly.
- 61 projects use `packages.config`, which needs `nuget.exe` on `PATH` for
  restore.

## Configuration

```ini
SONAR_URL=http://localhost:9000
SONAR_TOKEN=...
SONAR_ORG=                 # blank for self-hosted; only SonarCloud needs it

BITBUCKET_TOKEN=...
BITBUCKET_USERNAME=        # only for app passwords; blank sends a Bearer token
BITBUCKET_URL=https://ustr-bitbucket-1.na.uis.unisys.com
```

## Tests

```powershell
pwsh -File ..\scripts\agent_test.ps1      # agents, end to end, no SonarQube needed
```
```bash
python scripts/plan_test.py               # recipes, grouping, Bitbucket payloads
python scripts/smoke_test.py              # the analyse/approve/fix workflow
python scripts/config_test.py             # configuration and providers
```
