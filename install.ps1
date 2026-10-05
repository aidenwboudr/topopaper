# topopaper installer for Windows 10 and 11 (x64).
#
#   irm https://raw.githubusercontent.com/aidenwboudr/topopaper/main/install.ps1 | iex
#   .\install.ps1 [-Yes] [-NoStarter] [-NoAutostart] [-NoLaunch] [-Zip FILE] [-Version TAG]
#
# It downloads the prebuilt engine from the GitHub release, puts it in
# %LOCALAPPDATA%\Programs\topopaper, sets up a Python environment for the map
# builder in %LOCALAPPDATA%\topopaper\venv (installing Python with winget if
# you have none, after asking), adds Start menu entries, downloads the
# starter globe and asks about starting at sign-in and the Win+Shift+B search
# shortcut. Nothing needs administrator rights. uninstall.ps1 (installed next
# to the program) takes it all back.
#
# Through `irm | iex` the options come from environment variables instead:
# TOPOPAPER_YES=1, TOPOPAPER_NO_STARTER=1, TOPOPAPER_NO_AUTOSTART=1,
# TOPOPAPER_NO_LAUNCH=1, TOPOPAPER_VERSION=v1.1.0.
param(
    [switch]$Yes,
    [switch]$NoStarter,
    [switch]$NoAutostart,
    [switch]$NoLaunch,
    [string]$Zip = "",
    [string]$Version = ""
)
# "Continue": Windows PowerShell 5.1 turns a native program's stderr into a
# terminating error under "Stop"; cmdlets that must not fail say -ErrorAction
# Stop, native programs are checked through $LASTEXITCODE
$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"          # Invoke-WebRequest is slow with a progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

if ($env:TOPOPAPER_YES -eq "1") { $Yes = $true }
if ($env:TOPOPAPER_NO_STARTER -eq "1") { $NoStarter = $true }
if ($env:TOPOPAPER_NO_AUTOSTART -eq "1") { $NoAutostart = $true }
if ($env:TOPOPAPER_NO_LAUNCH -eq "1") { $NoLaunch = $true }
if (-not $Version -and $env:TOPOPAPER_VERSION) { $Version = $env:TOPOPAPER_VERSION }

$Repo   = "aidenwboudr/topopaper"
$Prefix = Join-Path $env:LOCALAPPDATA "Programs\topopaper"
$Data   = Join-Path $env:LOCALAPPDATA "topopaper"
$Venv   = Join-Path $Data "venv"
$Bin    = Join-Path $Prefix "bin"
$Share  = Join-Path $Prefix "share\topopaper"
$Ctl    = Join-Path $Bin "topopaper-ctl.cmd"
$Menu   = Join-Path ([Environment]::GetFolderPath("Programs")) "Topopaper"

function Say($m)  { Write-Host "==> $m" -ForegroundColor White }
function Ok($m)   { Write-Host "  + $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  ! $m" -ForegroundColor Yellow }
function Die($m)  { Write-Host "error: $m" -ForegroundColor Red; throw $m }

function Ask($q) {
    if ($Yes) { return $true }
    try { $a = Read-Host "$q [y/N]" } catch { return $false }
    return $a -match '^(y|yes)$'
}

# ---- 1. Windows --------------------------------------------------------------------
Say "Checking Windows"
$build = [Environment]::OSVersion.Version.Build
if ([Environment]::OSVersion.Version.Major -lt 10) { Die "topopaper needs Windows 10 or 11" }
if (-not [Environment]::Is64BitOperatingSystem) { Die "topopaper needs 64-bit Windows" }
Ok ("Windows {0} (build {1})" -f $(if ($build -ge 22000) { "11" } else { "10" }), $build)

# ---- 2. Python ---------------------------------------------------------------------
# Any CPython 3.9+ with venv and Tk; the Microsoft Store's python.exe stub
# (which only opens the Store) fails the probe and is skipped.
function Find-Python {
    $probe = "import sys, venv, tkinter; print(sys.executable); sys.exit(0 if sys.version_info >= (3, 9) else 1)"
    $cands = @()
    if (Get-Command py -ErrorAction SilentlyContinue) { $cands += ,@("py", "-3") }
    foreach ($n in "python", "python3") {
        if (Get-Command $n -ErrorAction SilentlyContinue) { $cands += ,@($n) }
    }
    $known = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                           "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
             Sort-Object FullName -Descending
    foreach ($k in $known) { $cands += ,@($k.FullName) }
    foreach ($c in $cands) {
        try {
            $exe = $c[0]; $rest = @($c | Select-Object -Skip 1)
            $out = & $exe @rest -c $probe 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1).Trim() }
        } catch { }
    }
    return $null
}

Say "Checking Python"
$Py = Find-Python
if (-not $Py) {
    Warn "no Python 3.9+ with Tk found (the map builder and the settings window are Python)"
    if ((Get-Command winget -ErrorAction SilentlyContinue) -and
        (Ask "  Install Python 3.13 for your user with winget?")) {
        winget install -e --id Python.Python.3.13 --scope user --silent `
            --accept-package-agreements --accept-source-agreements | Out-Host
        $Py = Find-Python
    }
    if (-not $Py) { Die "install Python 3 from https://www.python.org/downloads/ (keep 'tcl/tk' ticked), then run this again" }
}
Ok $Py

# ---- 3. the program ----------------------------------------------------------------
Say "Getting topopaper"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("topopaper-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
try {
    if ($Zip) {
        $archive = (Resolve-Path $Zip).Path
        Ok "using $archive"
    } else {
        $base = if ($Version) { "https://github.com/$Repo/releases/download/$Version" }
                else { "https://github.com/$Repo/releases/latest/download" }
        $archive = Join-Path $tmp "topopaper-windows-x64.zip"
        Invoke-WebRequest "$base/topopaper-windows-x64.zip" -OutFile $archive -UseBasicParsing -ErrorAction Stop
        $sum = (Invoke-WebRequest "$base/topopaper-windows-x64.zip.sha256" -UseBasicParsing -ErrorAction Stop).Content
        if ($sum -is [byte[]]) { $sum = [Text.Encoding]::ASCII.GetString($sum) }
        $want = ($sum -split '\s+')[0].ToLower()
        $got = (Get-FileHash $archive -Algorithm SHA256).Hash.ToLower()
        if ($want -ne $got) { Die "checksum mismatch (expected $want, got $got)" }
        Ok "downloaded and verified"
    }
    # a running wallpaper holds topopaper.exe open: stop it first
    if (Test-Path $Ctl) { & $Ctl stop 2>&1 | Out-Null }
    Get-Process topopaper -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Expand-Archive $archive -DestinationPath $tmp -Force -ErrorAction Stop
    $src = Join-Path $tmp "topopaper"
    if (-not (Test-Path (Join-Path $src "bin\topopaper.exe"))) { Die "the archive has no topopaper\bin\topopaper.exe" }
    foreach ($d in "bin", "share") {
        $t = Join-Path $Prefix $d
        if (Test-Path $t) { Remove-Item $t -Recurse -Force -ErrorAction Stop }
    }
    New-Item -ItemType Directory -Path $Prefix -Force | Out-Null
    Copy-Item (Join-Path $src "*") $Prefix -Recurse -Force -ErrorAction Stop
    Ok "installed to $Prefix"
} finally {
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
}

# ---- 4. Python environment for the map builder ----------------------------------
Say "Setting up the map builder"
New-Item -ItemType Directory -Path $Data -Force | Out-Null
$VPy = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $VPy)) {
    & $Py -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Die "python -m venv failed" }
    Ok "created $Venv"
}
& $VPy -m pip install -q --disable-pip-version-check --upgrade numpy pillow zstandard tzdata 2>&1 | Out-Host
if ($LASTEXITCODE -ne 0) { Die "pip could not install numpy and Pillow (offline?)" }
& $VPy -m pip install -q --disable-pip-version-check timezonefinder 2>&1 | Out-Null
if ($LASTEXITCODE -eq 0) { Ok "numpy, Pillow and extras installed" } else { Ok "numpy and Pillow installed" }
# the venv imports the installed package from anywhere (shortcuts, sign-in)
$site = & $VPy -c "import sysconfig; print(sysconfig.get_paths()['purelib'])"
Set-Content -Path (Join-Path $site "topopaper.pth") -Value $Share -Encoding ASCII

# ---- 5. PATH and Start menu -----------------------------------------------------------
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if (-not $userPath) { $userPath = "" }
if (($userPath -split ';') -notcontains $Bin) {
    [Environment]::SetEnvironmentVariable("Path", ($userPath.TrimEnd(';') + ";" + $Bin).TrimStart(';'), "User")
    Ok "added $Bin to your PATH (new terminals get topopaper-ctl)"
}
$env:Path = "$Bin;$env:Path"

New-Item -ItemType Directory -Path $Menu -Force | Out-Null
$ws = New-Object -ComObject WScript.Shell
$pyw = Join-Path $Venv "Scripts\pythonw.exe"
$icon = Join-Path $Share "icons\topopaper.ico"
foreach ($s in @(@("Topopaper", "-m topopaper.settings", "Settings for the topopaper wallpaper"),
                 @("Topopaper search", "-m topopaper.cli search", "Fly the wallpaper anywhere on Earth"))) {
    $lnk = $ws.CreateShortcut((Join-Path $Menu ($s[0] + ".lnk")))
    $lnk.TargetPath = $pyw
    $lnk.Arguments = $s[1]
    $lnk.Description = $s[2]
    $lnk.IconLocation = "$icon,0"
    $lnk.WorkingDirectory = $Data
    $lnk.Save()
}
Ok "Start menu: Topopaper, Topopaper search"

# ---- 6. starter globe ---------------------------------------------------------------
if (-not $NoStarter) {
    Say "Getting the starter globe (about 30 MB)"
    & $Ctl starter
    if ($LASTEXITCODE -ne 0) { Warn "couldn't get the globe now; the wallpaper will fetch it on first start" }
}

# ---- 7. sign-in + shortcut ------------------------------------------------------------
if (-not $NoAutostart) {
    if (Ask "Start topopaper automatically when you sign in?") { & $Ctl autostart enable }
    if (Ask "Add a Win+Shift+B shortcut to search the map?") { & $Ctl keybind add }
}

# ---- 8. go ---------------------------------------------------------------------------------
Say "Done"
& $Ctl doctor --offline
if (-not $NoLaunch) {
    & $Ctl restart | Out-Null
    & $Ctl settings | Out-Null
    Write-Host "The wallpaper is starting, and the settings window is opening."
} else {
    Write-Host "Start it with:  topopaper-ctl restart     Settings: Start menu > Topopaper"
}
Write-Host "To remove it:   $Prefix\uninstall.ps1"
