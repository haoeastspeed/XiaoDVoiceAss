' ============================================================
'  小D语音网关 · 停止（无窗口）
' ============================================================
Option Explicit
Dim sh, fso, base, py, script, cmd
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base   = fso.GetParentFolderName(WScript.ScriptFullName)
script = base & "\scripts\gateway_service.py"

py = base & "\reference\xiaozhi-server\main\xiaozhi-server\.venv\Scripts\pythonw.exe"
If Not fso.FileExists(py) Then
    py = base & "\reference\xiaozhi-server\main\xiaozhi-server\.venv\Scripts\python.exe"
End If
If Not fso.FileExists(py) Then py = "pythonw.exe"

sh.CurrentDirectory = base
cmd = """" & py & """ """ & script & """ stop"
sh.Run cmd, 0, True
MsgBox "已停止小D语音网关（如果没有自启实例，可能本来就没在跑）", 64, "小D语音网关"
