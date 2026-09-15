param(
  [string]$CcmHome=(Join-Path $env:USERPROFILE '.codex-control-mcp'),
  [string]$ControllerTask='Codex-Control-MCP-Core-Controller'
)
$ErrorActionPreference='Stop'
$state=Join-Path $CcmHome 'state'
$queue=Join-Path $state 'core-controller-requests'
New-Item -ItemType Directory -Path $queue -Force | Out-Null
$value=[ordered]@{
  schema=1
  operation='restart'
  request_id=[guid]::NewGuid().ToString('n')
  requested_by_pid=$PID
  created_at=[DateTimeOffset]::UtcNow.ToString('o')
}
$request=Join-Path $queue ($value.request_id+'.json')
$temp="$request.tmp"
$value | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temp -Encoding UTF8
Move-Item -LiteralPath $temp -Destination $request -Force
Start-ScheduledTask -TaskName $ControllerTask -ErrorAction Stop
[pscustomobject]@{accepted=$true;request_id=$value.request_id;controller_task=$ControllerTask} | ConvertTo-Json -Compress
