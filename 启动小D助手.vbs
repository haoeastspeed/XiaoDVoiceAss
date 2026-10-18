' ============================================================
'  小D语音陪聊助手 · 启动器（无窗口，绝不会弹出黑色命令窗）
'  直接双击本文件即可启动；也可放到「启动」目录做开机自启。
'  原理：本脚本自身用 wscript 宿主运行（无控制台），再以
'  隐藏方式启动 小D智能助手.exe（exe 本身也是无控制台窗口程序）。
' ============================================================
Option Explicit
Dim sh, fso, base, exe, cands, i, c
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)

' 依次尝试常见位置，找到真正的 exe 即可
cands = Array( _
    base & "\小D智能助手.exe", _
    base & "\dist\小D智能助手\小D智能助手.exe", _
    base & "\dist_crashfix\小D智能助手\小D智能助手.exe" _
)
exe = ""
For i = 0 To UBound(cands)
    c = cands(i)
    If fso.FileExists(c) Then
        exe = c
        Exit For
    End If
Next

If exe = "" Then
    WScript.Echo "找不到 小D智能助手.exe，请把它和本启动器放在同一目录。"
    WScript.Quit 1
End If

sh.CurrentDirectory = fso.GetParentFolderName(exe)
' 第二个参数 0 = 隐藏窗口；第三个参数 False = 不等待
sh.Run """" & exe & """", 0, False
