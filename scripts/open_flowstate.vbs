' Silent launcher for Flowstate: runs open_flowstate.bat with no console window.
' Used by the Desktop shortcut created by install_desktop_shortcut.ps1.
' All start/health/open logic stays in the .bat — this just hides it.
Dim fso, shell, batPath
Set fso = CreateObject("Scripting.FileSystemObject")
Set shell = CreateObject("WScript.Shell")
batPath = fso.GetParentFolderName(WScript.ScriptFullName) & "\open_flowstate.bat"
shell.Run """" & batPath & """", 0, False
