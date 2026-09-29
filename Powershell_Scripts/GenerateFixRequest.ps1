$issues = Get-Content issues.json -Raw |
ConvertFrom-Json

$request = @{
    GeneratedOn = Get-Date
    Issues      = $issues.issues
}

$request |
ConvertTo-Json -Depth 20 |
Set-Content fix-request.json