' ============================================================
'  小D语音网关 · 启动（无窗口，不会弹出黑色命令窗）
'  双击本文件即可；也可放到「启动」目录做开机自启。
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
' 0 = 隐藏窗口，False = 不等待
cmd = """" & py & """ """ & script & """ start"
sh.Run cmd, 0, False
