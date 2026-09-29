param([string]$RepoPath)

$results = @()

$solutions = Get-ChildItem $RepoPath -Recurse -Filter *.sln

foreach($sln in $solutions)
{
    Write-Host "Building $($sln.Name)"

    Push-Location $sln.Directory.FullName

    dotnet restore $sln.FullName *> $null

    dotnet build $sln.FullName --no-restore `
        -property:WarningLevel=0 `
        *> build.log

    $success = $LASTEXITCODE -eq 0

    $results += [PSCustomObject]@{
        Solution = $sln.Name
        Success  = $success
        Path     = $sln.FullName
    }

    Pop-Location
}

$results |
ConvertTo-Json -Depth 5 |
Set-Content build-results.json