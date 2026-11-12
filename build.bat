@echo off
rem Construit dist\Minia.exe (FFmpeg doit etre dans le PATH ou a cote de l'exe).
cd /d "%~dp0"
python -m pip install -r requirements.txt pyinstaller || goto :error
set QT_QPA_PLATFORM=offscreen
python -c "from PySide6.QtGui import QGuiApplication; import sys; a=QGuiApplication(sys.argv); import app; app.save_icon('minia.ico')" || goto :error
set QT_QPA_PLATFORM=
python -m PyInstaller --noconfirm --clean --onefile --windowed --name Minia --icon minia.ico ^
  --exclude-module PySide6.QtNetwork --exclude-module PySide6.QtQml --exclude-module PySide6.QtQuick ^
  --exclude-module PySide6.QtPdf --exclude-module PySide6.QtWebEngineCore app.py || goto :error
echo.
echo Termine : dist\Minia.exe
exit /b 0
:error
echo La construction a echoue.
exit /b 1
