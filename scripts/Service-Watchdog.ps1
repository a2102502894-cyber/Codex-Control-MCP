$ErrorActionPreference='SilentlyContinue'
$ccmHome=Join-Path $env:USERPROFILE '.codex-control-mcp'
$state=Join-Path $ccmHome 'state'
$log=Join-Path $ccmHome 'logs\watchdog.jsonl'
$coreTask='Codex-Control-MCP-OnDemand'
$controllerTask='Codex-Control-MCP-Core-Controller'
$httpsTask='Codex-Control-MCP-HTTPS-OnDemand'
$publicMetadata='https://codex-control.aiwsb.site/.well-known/oauth-authorization-server'
$leasePath=Join-Path $state 'maintenance.lock'
$coreFailureState=Join-Path $state 'watchdog-core-failures.json'
$tunnelFailureState=Join-Path $state 'watchdog-tunnel-failures.json'
function Log-Event([string]$event,[string]$detail=''){
  try{ [pscustomobject]@{at=(Get-Date).ToString('o');event=$event;detail=$detail} | ConvertTo-Json -Compress | Add-Content -LiteralPath $log -Encoding UTF8 }catch{}
}
function Test-Port([int]$port){ return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1) }
function Wait-Port([int]$port,[int]$seconds){ for($i=0;$i -lt ($seconds*2);$i++){ if(Test-Port $port){return $true}; Start-Sleep -Milliseconds 500 }; return (Test-Port $port) }
function Test-PidAlive([int]$id){ try{$null=Get-Process -Id $id -ErrorAction Stop;return $true}catch{return $false} }
function Get-ActiveLease{
  # The controller owns an OS byte lock on a permanent guard file. Metadata
  # alone (especially a stale empty file) never disables recovery indefinitely.
  $guard=$null
  try{
    $guard=[IO.File]::Open(($leasePath+'.guard'),[IO.FileMode]::OpenOrCreate,[IO.FileAccess]::ReadWrite,[IO.FileShare]::ReadWrite)
    try{$guard.Lock(0,1)}catch{return @{owner='controller_os_lock';pid=0}}
    $guard.Unlock(0,1)
  }finally{if($guard){$guard.Dispose()}}
  if(-not (Test-Path -LiteralPath $leasePath)){return $null}
  try{
    $lease=Get-Content -LiteralPath $leasePath -Raw -Encoding UTF8|ConvertFrom-Json
    if(([int]$lease.pid -gt 0) -and (Test-PidAlive ([int]$lease.pid))){return $lease}
    Log-Event 'maintenance_lease_stale' ("owner="+$lease.owner+";pid="+$lease.pid)
  }catch{Log-Event 'maintenance_lease_stale' 'invalid_or_empty'}
  # Only the locked controller reclaims metadata; avoid racing a new owner.
  return $null
}
function Get-FailureCount([string]$path){try{if(Test-Path $path){return [int]((Get-Content $path -Raw|ConvertFrom-Json).count)}}catch{};return 0}
function Set-FailureCount([string]$path,[int]$count){try{[pscustomobject]@{count=$count;updated_at=(Get-Date).ToString('o')}|ConvertTo-Json -Compress|Set-Content -LiteralPath $path -Encoding UTF8}catch{}}
function Clear-FailureCount([string]$path){try{Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue}catch{}}
function Test-PublicMetadata{try{$r=Invoke-WebRequest -UseBasicParsing -Uri $publicMetadata -TimeoutSec 6;return ($r.StatusCode -eq 200)}catch{return $false}}

$activeLease=Get-ActiveLease
if($activeLease){Log-Event 'maintenance_lease_active' ("owner="+$activeLease.owner+";pid="+$activeLease.pid);exit 0}

# Only consecutive absent-listener observations request recovery. All starts
# and fallback belong to the independent locked controller, never this tick.
if(Test-Port 8774){
  Clear-FailureCount $coreFailureState
  Log-Event 'core_watchdog_healthy'
}else{
  $failures=(Get-FailureCount $coreFailureState)+1
  Set-FailureCount $coreFailureState $failures
  Log-Event 'core_down' ("consecutive="+$failures)
  if($failures -ge 2){
    $controller=Get-ScheduledTask -TaskName $controllerTask -ErrorAction SilentlyContinue
    if($controller -and $controller.State -eq 'Running'){
      Log-Event 'core_controller_already_running'
    }else{
      try{Start-ScheduledTask -TaskName $controllerTask -ErrorAction Stop;Log-Event 'core_controller_requested' ("consecutive="+$failures)}
      catch{Log-Event 'core_controller_start_error' $_.Exception.GetType().Name;exit 2}
    }
  }
}

# HTTPS/tunnel policy is intentionally unchanged: public probe failures never stop a
# live cloudflared child, and only three consecutive failures permit task recovery.
$tunnelJson=Join-Path $state 'tunnel.json'
if(Test-Path $tunnelJson){
  try{$tr=Get-Content -LiteralPath $tunnelJson -Raw|ConvertFrom-Json}catch{$tr=$null}
  if($tr -and $tr.origin -eq 'http://127.0.0.1:8774'){
    if(Test-PublicMetadata){Clear-FailureCount $tunnelFailureState}
    else{
      $failures=(Get-FailureCount $tunnelFailureState)+1
      Set-FailureCount $tunnelFailureState $failures
      Log-Event 'tunnel_public_probe_failed' ("count="+$failures)
      if($failures -ge 3){
        $svcPath=Join-Path $state 'tunnel-service.json';$alive=$false
        if(Test-Path $svcPath){try{$svc=Get-Content $svcPath -Raw|ConvertFrom-Json;$cp=Get-Process -Id ([int]$svc.child_pid) -ErrorAction Stop;$alive=($cp.ProcessName -eq 'cloudflared')}catch{$alive=$false}}
        if($alive){Log-Event 'tunnel_edge_unreachable_process_alive' ("count="+$failures)}
        else{
          Log-Event 'tunnel_confirmed_down' ("count="+$failures)
          $task=Get-ScheduledTask -TaskName $httpsTask -ErrorAction SilentlyContinue
          if($task -and $task.State -eq 'Running'){try{Stop-ScheduledTask -TaskName $httpsTask -ErrorAction SilentlyContinue}catch{};Start-Sleep -Seconds 2}
          try{Start-ScheduledTask -TaskName $httpsTask -ErrorAction Stop}catch{Log-Event 'tunnel_task_start_error' $_.Exception.GetType().Name}
          Start-Sleep -Seconds 8
          if(Test-PublicMetadata){Clear-FailureCount $tunnelFailureState;Log-Event 'tunnel_task_recovered'}else{Log-Event 'tunnel_task_failed'}
        }
      }
    }
  }
}

