@echo off
rem 无窗口版启动：转交 VBS。用 start 让本 bat 的控制台一闪即退。
rem 需要看详细输出/排障时，请在命令行直接执行：
rem     python scripts\gateway_service.py start
start "" wscript "%~dp0启动网关服务.vbs"
exit
