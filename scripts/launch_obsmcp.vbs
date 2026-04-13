Option Explicit

Dim shell
Dim fso
Dim root
Dim pythonPath
Dim command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
pythonPath = root & "\.venv\Scripts\python.exe"

If fso.FileExists(pythonPath) Then
  command = "cmd.exe /c cd /d """ & root & """ && """ & pythonPath & """ -u -m server.main"
Else
  command = "cmd.exe /c cd /d """ & root & """ && py -3 -u -m server.main"
End If

shell.Run command, 0, False
