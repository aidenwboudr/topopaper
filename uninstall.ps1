# Remove topopaper from Windows.
#
#   & "$env:LOCALAPPDATA\Programs\topopaper\uninstall.ps1"           keep your maps and settings
#   & "$env:LOCALAPPDATA\Programs\topopaper\uninstall.ps1" -Purge    also delete maps, settings, caches, logs
param([switch]$Purge)
$ErrorActionPreference = "Continue"

$Prefix = Join-Path $env:LOCALAPPDATA "Programs\topopaper"
$Data   = Join-Path $env:LOCALAPPDATA "topopaper"
$Conf   = Join-Path $env:APPDATA "topopaper"
$Bin    = Join-Path $Prefix "bin"
$Ctl    = Join-Path $Bin "topopaper-ctl.cmd"
$Menu   = Join-Path ([Environment]::GetFolderPath("Programs")) "Topopaper"

if (Test-Path $Ctl) {
    & $Ctl autostart disable 2>&1 | Out-Null
    & $Ctl keybind remove 2>&1 | Out-Null
    & $Ctl stop 2>&1 | Out-Null
}
Get-Process topopaper -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -and (($userPath -split ';') -contains $Bin)) {
    $kept = ($userPath -split ';') | Where-Object { $_ -and $_ -ne $Bin }
    [Environment]::SetEnvironmentVariable("Path", ($kept -join ';'), "User")
}
Remove-Item $Menu -Recurse -Force -ErrorAction SilentlyContinue
# this script lives in $Prefix: PowerShell has it in memory, so deleting is fine
Remove-Item $Prefix -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "topopaper removed from $Prefix"

if ($Purge) {
    Remove-Item $Data, $Conf -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "maps, settings, caches and logs deleted"
} else {
    Remove-Item (Join-Path $Data "venv") -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "kept your maps and settings ($Data, $Conf); -Purge deletes them"
}
