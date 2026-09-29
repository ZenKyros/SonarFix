param(
    [string]$RepoPath
)

if (-not $RepoPath) {
    Write-Error "Usage: .\02-RankSolutions.ps1 -RepoPath <repo_path>"
    exit 1
}

Write-Host ""
Write-Host "===================================="
Write-Host " SonarFix Solution Ranking Agent"
Write-Host "===================================="
Write-Host ""

$results = @()

$solutions = Get-ChildItem `
    -Path $RepoPath `
    -Recurse `
    -Filter *.sln `
    -ErrorAction SilentlyContinue

foreach ($sln in $solutions)
{
    Write-Host "Analyzing: $($sln.Name)"

    try
    {
        $content = Get-Content $sln.FullName -ErrorAction SilentlyContinue

        $projectCount =
        ($content | Select-String 'Project\(').Count

        $folder = $sln.Directory.FullName

        $csCount =
        (Get-ChildItem `
            -Path $folder `
            -Recurse `
            -Filter *.cs `
            -ErrorAction SilentlyContinue).Count

        $testPenalty = 0

        if ($sln.Name -match "Test|Tests")
        {
            $testPenalty = 50
        }

        $legacyPenalty = 0

        if ($sln.Name -match "2010|NUnit|VSTS")
        {
            $legacyPenalty = 30
        }

        $score =
            ($projectCount * 10) +
            ($csCount) -
            $testPenalty -
            $legacyPenalty

        $results += [PSCustomObject]@{
            Solution     = $sln.Name
            Projects     = $projectCount
            SourceFiles  = $csCount
            Score        = $score
            Path         = $sln.FullName
        }
    }
    catch
    {
        Write-Warning "Failed to evaluate $($sln.FullName)"
    }
}

$ranked =
$results |
Sort-Object Score -Descending

# Console Output

Write-Host ""
Write-Host "===== TOP 20 SOLUTIONS =====" -ForegroundColor Green
Write-Host ""

$ranked |
Select-Object -First 20 |
Format-Table `
Solution,
Projects,
SourceFiles,
Score -AutoSize

# Export CSV

$csvFile = Join-Path $PWD "solution-ranking.csv"

$ranked |
Export-Csv `
$csvFile `
-NoTypeInformation

# Export JSON

$jsonFile = Join-Path $PWD "solution-ranking.json"

$ranked |
ConvertTo-Json -Depth 10 |
Set-Content $jsonFile

# Recommended Candidate

$best = $ranked | Select-Object -First 1

Write-Host ""
Write-Host "===================================="
Write-Host " Recommended Scan Candidate"
Write-Host "===================================="
Write-Host ""

Write-Host "Solution :" $best.Solution -ForegroundColor Yellow
Write-Host "Projects :" $best.Projects
Write-Host "Files    :" $best.SourceFiles
Write-Host "Score    :" $best.Score
Write-Host ""

Write-Host "CSV Report : $csvFile"
Write-Host "JSON Report: $jsonFile"