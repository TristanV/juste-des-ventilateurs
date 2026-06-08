@echo off
REM ============================================================
REM  build_clean_app.bat — Juste des Ventilateurs
REM  Supprime et reconstruit integralement les conteneurs Docker,
REM  puis lance l'application.
REM ============================================================

echo.
echo ======================================================
echo   Juste des Ventilateurs — Rebuild complet
echo ======================================================
echo.

REM -- Se placer dans le repertoire du script
cd /d "%~dp0"

REM -- Arreter et supprimer les conteneurs, images et volumes du projet
echo [1/4] Arret et suppression des conteneurs existants...
docker compose down --volumes --remove-orphans
if errorlevel 1 (
    echo     (aucun conteneur actif, on continue)
)

echo [2/4] Suppression des images du projet...
docker compose images -q 2>nul | findstr /r "." >nul 2>&1
if not errorlevel 1 (
    for /f "tokens=*" %%i in ('docker compose images -q 2^>nul') do docker rmi -f %%i 2>nul
)
echo     Images supprimees.

REM -- Verifier que .env existe
if not exist ".env" (
    echo.
    echo ATTENTION : fichier .env manquant.
    echo Copier .env.example vers .env et configurer les parametres.
    echo     copy .env.example .env
    echo.
    pause
    exit /b 1
)

REM -- Reconstruire sans cache
echo [3/4] Reconstruction des images (--no-cache)...
docker compose build --no-cache
if errorlevel 1 (
    echo ERREUR : la construction Docker a echoue.
    pause
    exit /b 1
)

REM -- Lancer l'application
echo [4/4] Demarrage de l'application...
echo.
docker compose up

echo.
echo Application arretee.
pause
