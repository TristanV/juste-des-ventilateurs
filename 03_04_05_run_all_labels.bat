@echo off
REM ============================================================
REM  run_all_labels.bat -- Juste des Ventilateurs
REM  Lance l'entrainement et le benchmark pour les 3 labels :
REM    failure_60s  (panne dans les 60 secondes)
REM    failure_30s  (panne dans les 30 secondes)
REM    hot_30s      (temperature > 95% seuil dans les 30 secondes)
REM
REM  Prerequis :
REM    - conda activate juste-des-ventilateurs
REM    - data/processed/episode=* disponibles (02_ingest_gen_features.bat)
REM
REM  Resultats produits dans evaluation/results/ :
REM    failure_prediction_results_failure_60s.json
REM    failure_prediction_results_failure_30s.json
REM    failure_prediction_results_hot_30s.json
REM    fan_control_results_failure_60s.json
REM    benchmark_results_failure_60s.json  (+ _30s, _hot_30s)
REM    robustness_results_failure_60s.json (+ _30s, _hot_30s)
REM ============================================================

chcp 65001 > nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

set FAILED=0
set LABELS=failure_60s failure_30s hot_30s

echo.
echo ============================================================
echo  run_all_labels.bat -- Entrainement et benchmark (3 labels)
echo ============================================================
echo.

REM -- Verifier que les donnees processed existent
set FOUND_EP=0
for /d %%D in (data\processed\episode=*) do set FOUND_EP=1
if %FOUND_EP%==0 (
    echo ERREUR : Aucun episode dans data\processed\
    echo Lancer d'abord : 02_ingest_gen_features.bat
    pause
    exit /b 1
)

REM ============================================================
REM  PHASE 3 : Entrainement des modeles predictifs (3 labels)
REM ============================================================
echo ============================================================
echo  PHASE 3 : Entrainement des modeles predictifs
echo ============================================================
echo.

for %%L in (%LABELS%) do (
    echo ------------------------------------------------------------
    echo   Label : %%L
    echo ------------------------------------------------------------
    call 03_train_models.bat %%L
    if errorlevel 1 (
        echo   ERREUR pour le label %%L
        set /a FAILED+=1
    )
    echo.
)

REM ============================================================
REM  PHASE 4 : Entrainement des controleurs (label failure_60s)
REM  Note : le controleur est entraine sur failure_60s uniquement
REM  car c'est le label utilise par le superviseur en production.
REM  Pour tester d'autres labels en Phase 6, le meme controleur
REM  est reutilise.
REM ============================================================
echo ============================================================
echo  PHASE 4 : Entrainement des controleurs (failure_60s)
echo ============================================================
echo.
call 04_train_fan_controllers.bat failure_60s
if errorlevel 1 (
    echo   ERREUR lors de l'entrainement des controleurs
    set /a FAILED+=1
)
echo.

REM ============================================================
REM  PHASE 5 : Benchmark offline (3 labels)
REM ============================================================
echo ============================================================
echo  PHASE 5 : Benchmark offline et robustesse (3 labels)
echo ============================================================
echo.

for %%L in (%LABELS%) do (
    echo ------------------------------------------------------------
    echo   Benchmark label : %%L
    echo ------------------------------------------------------------
    call 05_benchmark_offline_metrics.bat %%L
    if errorlevel 1 (
        echo   ERREUR benchmark pour le label %%L
        set /a FAILED+=1
    )
    echo.
)

REM ============================================================
REM  Resume final
REM ============================================================
echo ============================================================
if %FAILED% gtr 0 (
    echo  TERMINE avec %FAILED% erreur(s^) -- voir logs ci-dessus
) else (
    echo  SUCCES -- tous les labels entraines et benchmarkes
)
echo.
echo  Resultats dans evaluation\results\ :
for %%L in (%LABELS%) do (
    echo    failure_prediction_results_%%L.json
    echo    benchmark_results_%%L.json
    echo    robustness_results_%%L.json
)
echo    fan_control_results_failure_60s.json
echo.
echo  Visualisation : notebooks\05_evaluation_comparative.ipynb
echo ============================================================
echo.
pause
exit /b %FAILED%
