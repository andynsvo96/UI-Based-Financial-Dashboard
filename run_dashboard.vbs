Option Explicit

Dim shell, fso, folder, launcher, venvPythonw, command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

folder = fso.GetParentFolderName(WScript.ScriptFullName)
launcher = fso.BuildPath(folder, "dashboard_launcher.pyw")
venvPythonw = fso.BuildPath(folder, ".venv\Scripts\pythonw.exe")

shell.CurrentDirectory = folder

If fso.FileExists(venvPythonw) Then
  command = """" & venvPythonw & """ """ & launcher & """"
Else
  command = "pyw.exe -3 """ & launcher & """"
End If

shell.Run command, 0, False
