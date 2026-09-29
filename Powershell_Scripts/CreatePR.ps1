param(
    [string]$Branch,
    [string]$IssueId
)

$body = @{
    title = "Sonar Fix $IssueId"
    description = "Automated remediation"
    source = @{
        branch = @{
            name = $Branch
        }
    }
}

$body |
ConvertTo-Json -Depth 10 |
Set-Content pr-payload.json