@echo off
REM Installation du plugin QD RIP Auvergne dans QGIS 3 (Windows)
REM Usage : double-cliquer sur ce fichier ou l'exécuter dans un terminal

SET PLUGIN_NAME=QD_RIP_Auvergne_QC
SET SCRIPT_DIR=%~dp0
SET SRC=%SCRIPT_DIR%%PLUGIN_NAME%
SET DEST=%APPDATA%\QGIS\QGIS3\profiles\default\python\plugins

echo Source      : %SRC%
echo Destination : %DEST%\%PLUGIN_NAME%

IF NOT EXIST "%SRC%" (
    echo ERREUR : dossier source introuvable : %SRC%
    pause
    exit /b 1
)

IF NOT EXIST "%DEST%" (
    mkdir "%DEST%"
)

IF EXIST "%DEST%\%PLUGIN_NAME%" (
    echo Suppression de l'ancienne version...
    rmdir /s /q "%DEST%\%PLUGIN_NAME%"
)

xcopy /E /I /Q "%SRC%" "%DEST%\%PLUGIN_NAME%"

echo.
echo  Plugin installe dans : %DEST%\%PLUGIN_NAME%
echo.
echo  Dans QGIS :
echo    1. Menu Extensions ^> Gerer et installer des extensions
echo    2. Onglet "Installees" -^> cocher "QD RIP Auvergne"
echo    3. (ou) Menu Extensions ^> QD RIP Auvergne
echo.
pause
