param(
  [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
if (-not $env:DATA_ROOT -and -not ($env:S3_BUCKET -and $env:S3_ENDPOINT)) {
  Write-Error "Set DATA_ROOT or S3_BUCKET and S3_ENDPOINT before starting."
}
Write-Host "LeRobot Visualizer: http://127.0.0.1:8000/view"
Write-Host "MCP endpoint:       http://127.0.0.1:8000/mcp"
Write-Host "Health:              http://127.0.0.1:8000/health"
& $Python -m app

