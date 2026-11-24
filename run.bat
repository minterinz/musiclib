@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PY=%~dp0venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"

if "%~1" NEQ "" (
    "%PY%" "%~dp0musiclib.py" %*
    goto :end
)

:menu
cls
echo ============================================================
echo    musiclib - 音乐库整理 / 修改工具
echo ============================================================
echo    解释器: %PY%
echo.
echo   [1] 扫描统计      (只读, 生成 scan.md / scan.csv)
echo   [2] 规范命名      (默认 dry-run 预览)
echo   [3] 目录扁平化    (默认 dry-run 预览)
echo   [4] 封面内嵌      (默认 dry-run 预览)
echo   [5] 5.1 降混      (默认 dry-run 预览)
echo   [6] 旧文件清理    (默认 dry-run 预览)
echo   [7] 库体检        (只读, 生成 verify.md)
echo   [8] 安装/修复依赖 (创建 venv 并安装 mutagen pillow)
echo   [9] 查看这个目录的报告文件
echo   [0] 退出
echo.
set /p ROOT=请输入音乐库根目录 (回车=退出): 
if "%ROOT%"=="" goto :end
if not exist "%ROOT%" (
    echo [!] 目录不存在: %ROOT%
    pause
    goto :menu
)

:action
cls
echo ============================================================
echo    音乐库: %ROOT%
echo ============================================================
echo   [1] 扫描统计
echo   [2] 规范命名        [d] 预览   [a] 执行
echo   [3] 目录扁平化      [d] 预览   [a] 执行(含清理空目录)
echo   [4] 封面内嵌        [d] 预览   [a] 执行
echo   [5] 5.1 降混        [d] 预览   [a] 执行(保留原件)
echo   [6] 旧文件清理      [d] 预览   [a] 执行(需再输 DELETE 确认)
echo   [7] 库体检
echo   [b] 返回   [0] 退出
echo.
set /p ACT=选择: 

if /i "%ACT%"=="1" "%PY%" "%~dp0musiclib.py" scan --root "%ROOT%" & pause & goto :action
if /i "%ACT%"=="7" "%PY%" "%~dp0musiclib.py" verify --root "%ROOT%" & pause & goto :action

if /i "%ACT%"=="2d" "%PY%" "%~dp0musiclib.py" rename --root "%ROOT%" & pause & goto :action
if /i "%ACT%"=="2a" "%PY%" "%~dp0musiclib.py" rename --root "%ROOT%" --apply & pause & goto :action

if /i "%ACT%"=="3d" "%PY%" "%~dp0musiclib.py" flatten --root "%ROOT%" & pause & goto :action
if /i "%ACT%"=="3a" "%PY%" "%~dp0musiclib.py" flatten --root "%ROOT%" --apply --prune-empty & pause & goto :action

if /i "%ACT%"=="4d" "%PY%" "%~dp0musiclib.py" cover --root "%ROOT%" & pause & goto :action
if /i "%ACT%"=="4a" "%PY%" "%~dp0musiclib.py" cover --root "%ROOT%" --apply & pause & goto :action

if /i "%ACT%"=="5d" "%PY%" "%~dp0musiclib.py" downmix --root "%ROOT%" & pause & goto :action
if /i "%ACT%"=="5a" "%PY%" "%~dp0musiclib.py" downmix --root "%ROOT%" --apply & pause & goto :action

if /i "%ACT%"=="6d" "%PY%" "%~dp0musiclib.py" cleanup --root "%ROOT%" & pause & goto :action
if /i "%ACT%"=="6a" (
    echo [!] 这会删除旧文件，已做覆盖校验，但请先确认已备份。
    set /p CONFIRM=输入 DELETE 确认删除: 
    if /i "!CONFIRM!"=="DELETE" (
        "%PY%" "%~dp0musiclib.py" cleanup --root "%ROOT%" --apply --force
    ) else (
        echo 已取消
    )
    pause & goto :action
)

if /i "%ACT%"=="b" goto :menu
if /i "%ACT%"=="0" goto :end
goto :action

:end
endlocal
