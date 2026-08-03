# Creates a Desktop shortcut "Flowstate.lnk" that launches open_flowstate.bat.
# Run once after clone:
#   powershell -ExecutionPolicy Bypass -File .\scripts\install_desktop_shortcut.ps1

$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$BatPath = Join-Path $RepoRoot "scripts\open_flowstate.bat"
$IcoPath = Join-Path $RepoRoot "assets\flowstate-icon.ico"

if (-not (Test-Path $BatPath)) {
    throw "Launcher not found: $BatPath"
}

$Desktop = [Environment]::GetFolderPath("Desktop")
if (-not $Desktop) {
    $Desktop = Join-Path $env:USERPROFILE "Desktop"
}
$ShortcutPath = Join-Path $Desktop "Flowstate.lnk"

$Wsh = New-Object -ComObject WScript.Shell
$Shortcut = $Wsh.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $BatPath
$Shortcut.WorkingDirectory = $RepoRoot
$Shortcut.WindowStyle = 7  # Minimized
$Shortcut.Description = "Open Flowstate schedule optimizer (localhost:8501)"
if (Test-Path $IcoPath) {
    $Shortcut.IconLocation = "$IcoPath,0"
}
$Shortcut.Save()

Write-Host "Created Desktop shortcut: $ShortcutPath"
Write-Host "Double-click 'Flowstate' to start/open http://localhost:8501"
