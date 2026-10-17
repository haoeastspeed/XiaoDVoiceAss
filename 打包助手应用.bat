@echo off
chcp 65001 >nul
cd /d "%~dp0"

set PY=C:\Python314\python.exe
if not exist "%PY%" set PY=python

echo ============================================================
echo   打包「小D智能助手」
echo   产物1：dist\小D智能助手\小D智能助手.exe   极速版（推荐日常用）
echo   产物2：dist\小D智能助手-单文件版.exe    便携版（启动较慢）
echo ============================================================
echo.

echo [1/2] 正在打包 极速版（文件夹版）...
set XIAOD_BUILD_MODE=onedir
"%PY%" -m PyInstaller app\build\xiaod.spec --noconfirm ^
    --distpath dist --workpath app\build\work_onedir
if errorlevel 1 goto fail
if exist "dist\小D智能助手-单文件版.exe" del "dist\小D智能助手-单文件版.exe"

echo.
echo [2/2] 正在打包 单文件版...
set XIAOD_BUILD_MODE=onefile
"%PY%" -m PyInstaller app\build\xiaod.spec --noconfirm ^
    --distpath dist_onefile --workpath app\build\work
if errorlevel 1 goto fail
if exist "dist_onefile\小D智能助手.exe" (
    move /y "dist_onefile\小D智能助手.exe" "dist\小D智能助手-单文件版.exe" >nul
    rmdir /s /q "dist_onefile" 2>nul
)

echo.
echo [完成]
echo   极速版： dist\小D智能助手\小D智能助手.exe
echo   单文件： dist\小D智能助手-单文件版.exe
echo.
pause
exit /b 0

:fail
echo.
echo [失败] 打包出错，请查看上面的日志。
pause
exit /b 1
