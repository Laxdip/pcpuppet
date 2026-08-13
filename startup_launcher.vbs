' startup_launcher.vbs
' Starts Laptop Controller silently with NO admin, NO UAC

Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
scriptPath = scriptDir & "\laptop_controller.py"

Set shell = CreateObject("WScript.Shell")
shell.CurrentDirectory = scriptDir
shell.Run "pythonw.exe """ & scriptPath & """", 0, False