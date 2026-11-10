' ForexBot Background Silent Launcher (No CMD window)
Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
WshShell.Run "python -u server.py", 0, False
Set WshShell = Nothing
