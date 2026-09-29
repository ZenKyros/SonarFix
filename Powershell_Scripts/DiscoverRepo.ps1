param(
    [string]$RepoPath
)

if (-not $RepoPath) {
    Write-Error "Usage: .\01-DiscoverRepo.ps1 -RepoPath <repo_path>"
    exit 1
}

Write-Host "==========================================="
Write-Host " SonarFix Repository Discovery Agent"
Write-Host "==========================================="
Write-Host "Repository: $RepoPath"
Write-Host ""

$result = [ordered]@{}

$result.Repository = $RepoPath
$result.ScanTime = (Get-Date)

# -----------------------------
# Solutions
# -----------------------------
$solutions = Get-ChildItem $RepoPath -Recurse -Filter *.sln -ErrorAction SilentlyContinue

# -----------------------------
# Projects
# -----------------------------
$csproj = Get-ChildItem $RepoPath -Recurse -Filter *.csproj -ErrorAction SilentlyContinue
$vbproj = Get-ChildItem $RepoPath -Recurse -Filter *.vbproj -ErrorAction SilentlyContinue

# -----------------------------
# Source Files
# -----------------------------
$csFiles = Get-ChildItem $RepoPath -Recurse -Filter *.cs -ErrorAction SilentlyContinue
$vbFiles = Get-ChildItem $RepoPath -Recurse -Filter *.vb -ErrorAction SilentlyContinue

$result.TotalSolutions = $solutions.Count
$result.TotalProjects  = $csproj.Count + $vbproj.Count
$result.CSharpFiles    = $csFiles.Count
$result.VBFiles        = $vbFiles.Count

# -----------------------------
# Test Projects
# -----------------------------
$testProjects = $csproj | Where-Object {
    $_.Name -match "test|tests|unittest|integration"
}

$result.TestProjects = $testProjects.Count

# -----------------------------
# Framework Discovery
# -----------------------------
$frameworks = @()

foreach ($proj in $csproj) {

    try {

        [xml]$xml = Get-Content $proj.FullName

        $targets = @(
            $xml.Project.PropertyGroup.TargetFramework,
            $xml.Project.PropertyGroup.TargetFrameworks,
            $xml.Project.PropertyGroup.TargetFrameworkVersion
        ) | Where-Object { $_ }

        $frameworks += $targets

    } catch {
    }
}

$result.Frameworks =
$frameworks |
Where-Object { $_ } |
Sort-Object -Unique

# -----------------------------
# Top Solutions
# -----------------------------
$topSolutions = @()

foreach ($sln in $solutions) {

    try {

        $projectCount =
        (
            Get-Content $sln.FullName |
            Select-String 'Project\('
        ).Count

        $topSolutions += [PSCustomObject]@{
            Name     = $sln.Name
            Projects = $projectCount
            Path     = $sln.FullName
        }

    } catch {
    }
}

$result.TopSolutions =
$topSolutions |
Sort-Object Projects -Descending |
Select-Object -First 20

# -----------------------------
# Language Detection
# -----------------------------
$languages = @()

if ($csFiles.Count -gt 0) { $languages += "C#" }
if ($vbFiles.Count -gt 0) { $languages += "VB.NET" }

$result.Languages = $languages

# -----------------------------
# File Extension Statistics
# -----------------------------
$result.TopExtensions =
Get-ChildItem $RepoPath -Recurse -File -ErrorAction SilentlyContinue |
Group-Object Extension |
Sort-Object Count -Descending |
Select-Object -First 30 Name,Count

# -----------------------------
# Summary
# -----------------------------
Write-Host ""
Write-Host "Discovery Summary"
Write-Host "------------------"
Write-Host "Solutions      : $($result.TotalSolutions)"
Write-Host "Projects       : $($result.TotalProjects)"
Write-Host "C# Files       : $($result.CSharpFiles)"
Write-Host "VB Files       : $($result.VBFiles)"
Write-Host "Test Projects  : $($result.TestProjects)"
Write-Host ""

# -----------------------------
# Export JSON
# -----------------------------
$jsonFile = Join-Path $PWD "repo-discovery.json"

$result |
ConvertTo-Json -Depth 10 |
Set-Content $jsonFile

Write-Host "Discovery report written to:"
Write-Host $jsonFile -ForegroundColor Green