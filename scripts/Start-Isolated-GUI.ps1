param([Parameter(Mandatory=$true)][string]$Exe,[Parameter(Mandatory=$true)][string]$Title,[Parameter(Mandatory=$true)][string]$Proof,[Parameter(Mandatory=$true)][string]$TaskName)
$ErrorActionPreference='Stop'
if($TaskName -notlike 'CCM-Isolated-GUI-*'){throw 'Only isolated fixture task names accepted'}
if((Split-Path $Exe -Leaf) -ne 'CodexControlGuiFixture.exe'){throw 'Only owned fixture executable accepted'}
$principal=New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
$action=New-ScheduledTaskAction -Execute $Exe -Argument ('"'+$Title+'" "'+$Proof+'"') -WorkingDirectory (Split-Path $Exe -Parent)
$settings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $TaskName -Action $action -Principal $principal -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $TaskName
