# Creates a Desktop shortcut "Flowstate.lnk" that launches open_flowstate.vbs
# via wscript.exe - silent, no console window flash.
# Run once after clone:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_desktop_shortcut.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$VbsPath = Join-Path $RepoRoot "scripts\open_flowstate.vbs"
$IcoPath = Join-Path $RepoRoot "assets\flowstate-icon.ico"

if (-not (Test-Path $VbsPath)) {
    throw "Silent launcher not found: $VbsPath"
}

$Desktop = [Environment]::GetFolderPath("Desktop")
if (-not $Desktop) {
    $Desktop = Join-Path $env:USERPROFILE "Desktop"
}
$ShortcutPath = Join-Path $Desktop "Flowstate.lnk"

$WscriptPath = Join-Path $env:SystemRoot "System32\wscript.exe"

$Wsh = New-Object -ComObject WScript.Shell
$Shortcut = $Wsh.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $WscriptPath
$Shortcut.Arguments = "`"$VbsPath`""
$Shortcut.WorkingDirectory = $RepoRoot
$Shortcut.WindowStyle = 1  # Normal (the .vbs itself runs the .bat hidden)
$Shortcut.Description = "Open Flowstate schedule optimizer (localhost:8501) - silent, no console window"
if (Test-Path $IcoPath) {
    $Shortcut.IconLocation = "$IcoPath,0"
}
$Shortcut.Save()

Write-Host "Created Desktop shortcut: $ShortcutPath"
Write-Host "Double-click 'Flowstate' to start/open http://localhost:8501 (no console window)"
