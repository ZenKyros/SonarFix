param([string]$Solution)

dotnet restore $Solution

dotnet build $Solution

$success = ($LASTEXITCODE -eq 0)

Write-Host "Build Success: $success"