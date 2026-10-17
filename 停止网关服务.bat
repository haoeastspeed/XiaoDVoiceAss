@echo off
rem 无窗口版停止：转交 VBS。用 start 让本 bat 的控制台一闪即退。
start "" wscript "%~dp0停止网关服务.vbs"
exit
