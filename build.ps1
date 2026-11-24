# 一键打包 Windows 单文件 exe
# 用法: powershell -ExecutionPolicy Bypass -File build.ps1
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install --quiet --upgrade pyinstaller | Out-Null

python -m PyInstaller --noconfirm --clean --onefile --windowed --name musiclib `
    --hidden-import mutagen --hidden-import mutagen.flac `
    --hidden-import PIL --hidden-import PIL.Image `
    --exclude-module numpy --exclude-module matplotlib --exclude-module scipy `
    musiclib_gui.py

Write-Host ""
Write-Host "产物: dist\musiclib.exe"
Write-Host "分发时把 bin\ffmpeg.exe 放到 exe 同级目录即可启用 5.1 降混。"
