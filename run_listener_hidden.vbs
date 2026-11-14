' Runs run_listener.bat with no window, from this script's own folder (wherever the repo was cloned).
Set objShell = CreateObject("WScript.Shell")
folder = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
objShell.CurrentDirectory = folder
objShell.Run """" & folder & "\run_listener.bat""", 0, False
