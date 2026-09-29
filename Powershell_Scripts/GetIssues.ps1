param(
    [string]$ProjectKey,
    [string]$Token
)

$url =
"http://localhost:9000/api/issues/search?componentKeys=$ProjectKey&ps=500"

$pair = "$($Token):"
$bytes = [Text.Encoding]::ASCII.GetBytes($pair)

$base64 = [Convert\]::ToBase64String($bytes)

$headers = @{
    Authorization = "Basic $base64"
}

$result = Invoke-RestMethod `
    -Uri $url `
    -Headers $headers

$result |
ConvertTo-Json -Depth 20 |
Set-Content issues.json