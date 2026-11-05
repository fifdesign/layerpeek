@echo off
rem Builds dist\LayerPeek.exe (single file, no console window)
python -m pip install -r requirements.txt || goto :error
python -m PyInstaller --onefile --windowed --name LayerPeek ^
    --collect-all tkinterdnd2 ^
    --icon assets\layerpeek.ico ^
    --add-data "assets;assets" ^
    --paths src ^
    src\layerpeek.py || goto :error
echo.
echo Done: dist\LayerPeek.exe
exit /b 0
:error
echo Build failed.
exit /b 1
