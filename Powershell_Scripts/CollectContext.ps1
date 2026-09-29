param(
    [string]$RepoPath,
    [string]$IssueFile
)

$file = Join-Path $RepoPath $IssueFile

$content = Get-Content $file

[PSCustomObject]@{
    File    = $IssueFile
    Content = $content
} |
ConvertTo-Json -Depth 10 |
Set-Content context.json