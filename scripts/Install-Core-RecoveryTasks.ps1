param(
  [switch]$Apply,
  [string]$Root=(Split-Path $PSScriptRoot -Parent),
  [string]$CcmHome=(Join-Path $env:USERPROFILE '.codex-control-mcp'),
  [string]$ExpectedVersion='0.2.0'
)
$ErrorActionPreference='Stop'
$state=Join-Path $CcmHome 'state'
$python=Join-Path $Root '.venv\Scripts\python.exe'
$hostScript=Join-Path $Root 'scripts\Core-Source-Host.ps1'
$controller=Join-Path $Root 'scripts\core_recovery_controller.py'
$watchdogSource=Join-Path $Root 'scripts\Service-Watchdog.ps1'
$watchdogTarget=Join-Path $state 'service-watchdog.ps1'
$watchdogLauncher=Join-Path $state 'service-watchdog-hidden.vbs'
$configPath=Join-Path $state 'core-controller-config.json'
$reportPath=Join-Path $state 'core-controller-report.json'
$requestPath=Join-Path $state 'core-controller-requests'
$leasePath=Join-Path $state 'maintenance.lock'
$coreTask='Codex-Control-MCP-OnDemand'
$currentTask='Codex-Control-MCP-Core-Current'
$lkgTask='Codex-Control-MCP-Core-LKG'
$controllerTask='Codex-Control-MCP-Core-Controller'
$watchdogTask='Codex-Control-MCP-Watchdog'
foreach($path in @($python,$hostScript,$controller,$watchdogSource)){
  if(-not (Test-Path -LiteralPath $path)){throw "Required path missing: $path"}
}
$lkg=Join-Path $state "lkg-$ExpectedVersion\src"
if(-not (Test-Path -LiteralPath $lkg)){throw "Required LKG missing: $lkg"}
$config=[ordered]@{
  schema=1; home=$CcmHome; port=8774; expected_version=$ExpectedVersion; expected_tool_count=47; probe_timeout_seconds=12
  lease=$leasePath; lease_seconds=300; report=$reportPath; request=$requestPath
  stop_timeout_seconds=20
  host_tasks=@($coreTask,$currentTask,$lkgTask)
  candidates=@(
    [ordered]@{name='formal_task';task=$coreTask;timeout_seconds=35;expected_tool_count=47},
    [ordered]@{name='current_source_direct';task=$currentTask;timeout_seconds=25;expected_tool_count=47},
    [ordered]@{name='lkg_direct';task=$lkgTask;timeout_seconds=25;expected_tool_count=47}
  )
}
$plan=[ordered]@{
  apply=[bool]$Apply; core_task=$coreTask; controller_task=$controllerTask
  fallback_tasks=@($currentTask,$lkgTask); watchdog_task=$watchdogTask
  core_triggers=@('AtStartup','AtLogOn'); expected_version=$ExpectedVersion
  https_task_changed=$false; config=$config
}
if(-not $Apply){$plan|ConvertTo-Json -Depth 8; exit 0}
New-Item -ItemType Directory -Path $state -Force | Out-Null
$backup=Join-Path $state ('recovery-task-backup-'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Path $backup -Force | Out-Null
foreach($name in @($coreTask,$currentTask,$lkgTask,$controllerTask,$watchdogTask)){
  $existing=Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
  if($existing){Export-ScheduledTask -TaskName $name | Set-Content -LiteralPath (Join-Path $backup ($name+'.xml')) -Encoding UTF8}
}
foreach($path in @($watchdogTarget,$watchdogLauncher,$configPath)){
  if(Test-Path -LiteralPath $path){Copy-Item -LiteralPath $path -Destination (Join-Path $backup (Split-Path $path -Leaf))}
}
$config | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $configPath -Encoding UTF8
Copy-Item -LiteralPath $watchdogSource -Destination $watchdogTarget -Force
$user=[Security.Principal.WindowsIdentity]::GetCurrent().Name
# Preserve interactive desktop + owner DPAPI/network identity, not S4U.
$principal=New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
$core=Get-ScheduledTask -TaskName $coreTask -ErrorAction Stop
$coreTriggers=@((New-ScheduledTaskTrigger -AtStartup),(New-ScheduledTaskTrigger -AtLogOn -User $user))
Set-ScheduledTask -TaskName $coreTask -Trigger $coreTriggers -Settings $settings -Principal $principal | Out-Null
$psExe="$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$wscriptExe="$env:SystemRoot\System32\wscript.exe"
$watchdogLauncherContent=@"
Set sh = CreateObject("WScript.Shell")
q = Chr(34)
cmd = q & "$psExe" & q & " -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File " & q & "$watchdogTarget" & q
rc = sh.Run(cmd, 0, True)
WScript.Quit rc
"@
[IO.File]::WriteAllText($watchdogLauncher,$watchdogLauncherContent,(New-Object Text.UTF8Encoding($false)))
function Register-OwnedTask([string]$Name,$Action,$Triggers=$null,$TaskSettings=$settings){
  $old=Get-ScheduledTask -TaskName $Name -ErrorAction SilentlyContinue
  if($old){
    if($null -eq $Triggers){Set-ScheduledTask -TaskName $Name -Action $Action -Principal $principal -Settings $TaskSettings | Out-Null}
    else{Set-ScheduledTask -TaskName $Name -Action $Action -Principal $principal -Settings $TaskSettings -Trigger $Triggers | Out-Null}
  }else{
    if($null -eq $Triggers){Register-ScheduledTask -TaskName $Name -Action $Action -Principal $principal -Settings $TaskSettings | Out-Null}
    else{Register-ScheduledTask -TaskName $Name -Action $Action -Principal $principal -Settings $TaskSettings -Trigger $Triggers | Out-Null}
  }
}
$currentAction=New-ScheduledTaskAction -Execute $psExe -Argument ('-WindowStyle Hidden -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$hostScript+'" -Mode current -Root "'+$Root+'" -CcmHome "'+$CcmHome+'"') -WorkingDirectory $Root
$lkgAction=New-ScheduledTaskAction -Execute $psExe -Argument ('-WindowStyle Hidden -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "'+$hostScript+'" -Mode lkg -Root "'+$Root+'" -CcmHome "'+$CcmHome+'"') -WorkingDirectory $lkg
$controllerAction=New-ScheduledTaskAction -Execute $python -Argument ('"'+$controller+'" --config "'+$configPath+'"') -WorkingDirectory $Root
$watchdogAction=New-ScheduledTaskAction -Execute $wscriptExe -Argument ('//B //Nologo "'+$watchdogLauncher+'"') -WorkingDirectory $state
Register-OwnedTask $currentTask $currentAction
Register-OwnedTask $lkgTask $lkgAction
$controllerSettings=New-ScheduledTaskSettingsSet -MultipleInstances Queue -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 5)
Register-OwnedTask $controllerTask $controllerAction $null $controllerSettings
$watchdogTriggers=@((New-ScheduledTaskTrigger -AtLogOn -User $user),(New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Days 3650)))
$watchdogSettings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 1)
Register-OwnedTask $watchdogTask $watchdogAction $watchdogTriggers $watchdogSettings
$plan.backup=$backup
$plan.config_path=$configPath
$plan.watchdog_target=$watchdogTarget
$plan|ConvertTo-Json -Depth 8
