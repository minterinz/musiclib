@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ============================================================
echo    安装 / 修复依赖
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [!] 找不到 python，请先安装 Python 3.8+ 并勾选 Add to PATH
    pause
    exit /b 1
)

if not exist "%~dp0venv\Scripts\python.exe" (
    echo [1/3] 创建虚拟环境 venv ...
    python -m venv "%~dp0venv"
    if errorlevel 1 (
        echo [!] 创建 venv 失败
        pause
        exit /b 1
    )
) else (
    echo [1/3] venv 已存在，跳过创建
)

echo [2/3] 升级 pip ...
"%~dp0venv\Scripts\python.exe" -m pip install --quiet --upgrade pip

echo [3/3] 安装 mutagen pillow ...
"%~dp0venv\Scripts\python.exe" -m pip install --quiet mutagen pillow
if errorlevel 1 (
    echo [!] 安装依赖失败，检查网络或换国内源：
    echo     venv\Scripts\python.exe -m pip install -i https://pypi.tuna.tsinghua.edu.cn/simple mutagen pillow
    pause
    exit /b 1
)

echo.
echo 依赖安装完成。运行 run.bat 开始使用。
echo 提示：5.1 降混需要 ffmpeg，可用 --ffmpeg 指定路径，或把 ffmpeg.exe 放进 PATH。
pause
