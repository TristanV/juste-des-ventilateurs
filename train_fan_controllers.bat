@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

set LABEL=%1
if "%LABEL%"=="" set LABEL=failure_60s

echo ============================================================
echo Phase 5 -- Entrainement et evaluation des controleurs fans
echo Label : %LABEL%
echo ============================================================
echo.

set FAILED=0

REM ----------------------------------------------------------------
REM 1. EDA rapide pour verifier les donnees
REM ----------------------------------------------------------------
echo [1/3] Verification des donnees (EDA)...
python ingest_quick_EDA.py --processed-only
set _RC=%ERRORLEVEL%
call :check_step "EDA" %_RC%

echo.

REM ----------------------------------------------------------------
REM 2. Entrainement et evaluation comparative de tous les controleurs
REM ----------------------------------------------------------------
echo [2/3] Evaluation comparative de tous les controleurs...
python -m evaluation.fan_control_eval --label %LABEL% --models all
set _RC=%ERRORLEVEL%
call :check_step "fan_control_eval" %_RC%

echo.

REM ----------------------------------------------------------------
REM 3. Verification des fichiers produits
REM ----------------------------------------------------------------
echo [3/3] Verification des artefacts produits...

set RESULTS_FILE=evaluation\results\fan_control_results.json
if exist "%RESULTS_FILE%" (
    echo   OK : %RESULTS_FILE%
) else (
    echo   MANQUANT : %RESULTS_FILE%
    set FAILED=1
)

call :check_saved baseline_fixed_1500.json
call :check_saved baseline_threshold.json
call :check_saved baseline_pid.json
call :check_saved supervised.joblib
call :check_saved score_controller.json

echo.
if %FAILED% gtr 0 goto :failure
goto :success

:failure
echo ============================================================
echo ECHEC : %FAILED% etape(s) ont echoue
echo ============================================================
exit /b 1

:success

echo ============================================================
echo Phase 5 terminee avec succes
echo Resultats : evaluation\results\fan_control_results.json
echo ============================================================
exit /b 0

:check_step
set STEP_NAME=%~1
set STEP_CODE=%~2
if %STEP_CODE% neq 0 (
    echo   ERREUR dans l'etape : %STEP_NAME% (code %STEP_CODE%)
    set /a FAILED+=1
)
goto :eof

:check_saved
set SAVED_FILE=models\fan_control\saved\%~1
if exist "%SAVED_FILE%" (
    echo   OK : %SAVED_FILE%
) else (
    echo   INFO : %SAVED_FILE% -- non genere si controleur saute
)
goto :eof
