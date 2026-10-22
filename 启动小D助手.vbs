' ============================================================
'  小D语音陪聊助手 · 启动器（无窗口，绝不会弹出黑色命令窗）
'  直接双击本文件即可启动；也可放到「启动」目录做开机自启。
'  原理：本脚本自身用 wscript 宿主运行（无控制台），再以
'  隐藏方式启动 小D智能助手.exe（exe 本身也是无控制台窗口程序）。
'  多个构建并存时，自动选时间最新的那个，避免启动到旧版。
' ============================================================
Option Explicit
Dim sh, fso, base, exe, cands, i, bestTs, haveBest
Set sh  = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

base = fso.GetParentFolderName(WScript.ScriptFullName)

' 依次尝试常见位置（同目录 > 新构建 > 旧构建），选最新的
cands = Array( _
    base & "\小D智能助手.exe", _
    base & "\dist_crashfix\小D智能助手\小D智能助手.exe", _
    base & "\dist\小D智能助手\小D智能助手.exe" _
)

exe = ""
haveBest = False
For i = 0 To UBound(cands)
    If fso.FileExists(cands(i)) Then
        If Not haveBest Then
            bestTs = fso.GetFile(cands(i)).DateLastModified
            exe = cands(i)
            haveBest = True
        ElseIf fso.GetFile(cands(i)).DateLastModified > bestTs Then
            bestTs = fso.GetFile(cands(i)).DateLastModified
            exe = cands(i)
        End If
    End If
Next

If exe = "" Then
    WScript.Echo "找不到 小D智能助手.exe，请把它和本启动器放在同一目录。"
    WScript.Quit 1
End If

sh.CurrentDirectory = fso.GetParentFolderName(exe)
' 第二个参数 0 = 隐藏窗口；第三个参数 False = 不等待
sh.Run """" & exe & """", 0, False
