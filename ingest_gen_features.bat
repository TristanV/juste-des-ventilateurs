@echo off
REM ============================================================
REM  ingest_gen_features.bat — Juste des Ventilateurs
REM  Genere les features pour chaque episode ingere dans data/raw/
REM
REM  Usage :
REM    ingest_gen_features.bat            -> traite tous les episodes trouves
REM    ingest_gen_features.bat 003        -> traite uniquement l'episode 003
REM
REM  Prerequis :
REM    - conda activate juste-des-ventilateurs
REM    - episodes ingestees dans data/raw/episode=*/
REM ============================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

set RAW_DIR=data\raw
set PROCESSED_DIR=data\processed
set EPISODE_FILTER=%1

echo.
echo ======================================================
echo   Juste des Ventilateurs — Generation des features
echo ======================================================

if "%EPISODE_FILTER%"=="" (
    echo   Mode : tous les episodes dans %RAW_DIR%\
) else (
    echo   Mode : episode specifique = %EPISODE_FILTER%
)
echo ======================================================
echo.

REM -- Verifier que le repertoire raw existe
if not exist "%RAW_DIR%" (
    echo ERREUR : Repertoire '%RAW_DIR%' introuvable.
    echo Lance d'abord ingest_mqtt_simulations.bat pour collecter les donnees.
    pause
    exit /b 1
)

set TOTAL=0
set SUCCESS=0
set FAILED=0

REM -- Parcourir tous les dossiers episode=* dans data/raw/
for /d %%D in (%RAW_DIR%\episode=*) do (
    REM Extraire le nom du dossier (ex: episode=001)
    set FOLDER=%%~nxD
    set EP_ID=!FOLDER:episode=!

    REM Filtrer si un episode specifique est demande
    if not "%EPISODE_FILTER%"=="" (
        if not "!EP_ID!"=="%EPISODE_FILTER%" (
            goto :next_episode
        )
    )

    set /a TOTAL+=1

    echo --------------------------------------------------
    echo [Episode !EP_ID!] Dossier : %%D
    echo --------------------------------------------------

    REM -- Verifier si des donnees parquet existent
    set FOUND_DATA=0
    for /r "%%D" %%F in (*.parquet *.csv) do (
        set FOUND_DATA=1
    )
    if !FOUND_DATA!==0 (
        echo [!EP_ID!] ATTENTION : Aucun fichier parquet/csv trouve, episode ignore.
        set /a FAILED+=1
        goto :next_episode
    )

    REM -- Construire le chemin de sortie
    set OUTPUT=%PROCESSED_DIR%\episode=!EP_ID!

    REM -- Verifier si metadata.json existe pour passer la config
    set CONFIG_ARG=
    if exist "%%D\metadata.json" (
        set CONFIG_ARG=--config %%D\metadata.json
        echo [!EP_ID!] Config : %%D\metadata.json
    ) else (
        echo [!EP_ID!] ATTENTION : Pas de metadata.json, les specs machines seront par defaut.
    )

    echo [!EP_ID!] Sortie : !OUTPUT!
    echo [!EP_ID!] Generation des features...

    python -m features.pipeline ^
        --input %%D ^
        --output !OUTPUT! ^
        !CONFIG_ARG!

    if errorlevel 1 (
        echo [!EP_ID!] ERREUR lors de la generation des features.
        set /a FAILED+=1
    ) else (
        echo [!EP_ID!] Features generees avec succes → !OUTPUT!
        set /a SUCCESS+=1
    )

    echo.

    :next_episode
)

if %TOTAL%==0 (
    if "%EPISODE_FILTER%"=="" (
        echo Aucun episode trouve dans %RAW_DIR%\.
        echo Lance d'abord ingest_mqtt_simulations.bat.
    ) else (
        echo Episode '%EPISODE_FILTER%' introuvable dans %RAW_DIR%\.
    )
    pause
    exit /b 1
)

echo ======================================================
echo   Feature engineering termine.
echo   Episodes traites : %TOTAL%
echo   Succes           : %SUCCESS%
echo   Echecs           : %FAILED%
echo   Donnees dans     : %PROCESSED_DIR%\
echo ======================================================
echo.

if %FAILED% gtr 0 (
    echo ATTENTION : %FAILED% episode(s) en erreur. Verifier les logs ci-dessus.
)

pause
