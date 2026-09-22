Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

appDir = fso.GetParentFolderName(WScript.ScriptFullName)
WshShell.CurrentDirectory = appDir

pythonwPath = appDir & "\.venv\Scripts\pythonw.exe"
scriptPath = appDir & "\desktop_app.py"
batPath = appDir & "\install_dependencies.bat"

' 1. Normal Launch: If virtual environment exists, launch completely silently (Style 0 = Hidden)
If fso.FileExists(pythonwPath) Then
    WshShell.Run """" & pythonwPath & """ """ & scriptPath & """", 0, False
Else
    ' 2. Self-Healing Fallback: If .venv is missing, run dependency installer
    If fso.FileExists(batPath) Then
        ' Show CMD window (Style 1) and wait until dependencies finish installing (True)
        returnCode = WshShell.Run("cmd.exe /c """"" & batPath & """""", 1, True)
        
        ' 3. Verify .venv was created successfully
        If fso.FileExists(pythonwPath) Then
            WshShell.Run """" & pythonwPath & """ """ & scriptPath & """", 0, False
        Else
            MsgBox "Dependency installation did not complete successfully." & vbCrLf & vbCrLf & _
                   "Could not locate Python runtime at:" & vbCrLf & _
                   pythonwPath & vbCrLf & vbCrLf & _
                   "Please ensure Python 3.10+ is installed and check install_log.txt for details.", _
                   16, "EmailAutomater Error"
        End If
    Else
        MsgBox "Critical installation file missing:" & vbCrLf & _
               batPath & vbCrLf & vbCrLf & _
               "Please reinstall EmailAutomater.", _
               16, "EmailAutomater Error"
    End If
End If