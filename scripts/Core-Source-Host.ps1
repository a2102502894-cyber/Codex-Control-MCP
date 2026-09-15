param(
  [Parameter(Mandatory=$true)][ValidateSet('current','lkg')][string]$Mode,
  [string]$Root=(Split-Path $PSScriptRoot -Parent),
  [string]$CcmHome=(Join-Path $env:USERPROFILE '.codex-control-mcp')
)
$ErrorActionPreference='Stop'
$python=Join-Path $Root '.venv\Scripts\python.exe'
if(-not (Test-Path -LiteralPath $python)){ throw "Python is missing: $python" }
if($Mode -eq 'lkg'){
  $lkg=Join-Path $CcmHome 'state\lkg-0.2.0\src'
  if(-not (Test-Path -LiteralPath $lkg)){ throw "LKG is missing: $lkg" }
  $env:PYTHONPATH=$lkg
  $working=$lkg
}else{
  Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
  $working=$Root
}
Set-Location -LiteralPath $working
$log=Join-Path $CcmHome ('logs\core-'+$Mode+'-fallback.log')
& $python -u -m codex_control_mcp --home $CcmHome serve --transport streamable-http >> $log 2>&1
exit $LASTEXITCODE
