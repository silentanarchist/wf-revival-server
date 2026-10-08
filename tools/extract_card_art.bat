@echo off
setlocal
cd /d "%~dp0.."
title Warpforge Revival - extract card art for the card creator

echo.
echo  Extract the official card pictures for the card creator's preview
echo  ------------------------------------------------------------------
echo  This reads your installed copy of the game and writes
echo    content\files\card-art.zip
echo  The pictures are the game's own art: keep them on your server, never publish them.
echo.

set "GAME=C:\Program Files (x86)\Steam\steamapps\common\Warhammer 40k Warpforge"
if not exist "%GAME%\Warpforge_Data" (
    echo  The game was not found at
    echo    %GAME%
    set /p "GAME=  Paste the game's folder ^(the one holding Warpforge_Data^): "
)
if not exist "%GAME%\Warpforge_Data" (
    echo  That folder does not hold Warpforge_Data.
    goto :end
)

where python >nul 2>nul
if errorlevel 1 (
    echo  This needs Python 3.9 or newer ^(python.org^).
    goto :end
)
python -c "import UnityPy, PIL" >nul 2>nul
if errorlevel 1 (
    echo  Installing UnityPy and Pillow ^(once^) ...
    python -m pip install --user UnityPy Pillow
)

python tools\extract_card_art.py --game "%GAME%"
if errorlevel 1 (
    echo.
    echo  That did not work - the message above says why.
    goto :end
)
echo.
echo  Done. The card creator shows the pictures straight away.

:end
echo.
pause
