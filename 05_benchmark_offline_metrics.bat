@echo off
chcp 65001 > nul
setlocal enabledelayedexpansion

set LABEL=%1
if "%LABEL%"=="" set LABEL=failure_60s

echo ============================================================
echo Phase 6 -- Boucle fermee et evaluation comparative
echo Label : %LABEL%
echo ============================================================
echo.

set FAILED=0

REM ----------------------------------------------------------------
REM 1. Benchmark comparatif (native vs threshold vs ml)
REM ----------------------------------------------------------------
echo [1/3] Benchmark comparatif (native / threshold / ml)...
python -m evaluation.benchmark --label %LABEL%
set _RC=%ERRORLEVEL%
call :check_step "benchmark" %_RC%

echo.

REM ----------------------------------------------------------------
REM 2. Test de robustesse par scenario
REM ----------------------------------------------------------------
echo [2/3] Test de robustesse par scenario...
python -m evaluation.robustness --label %LABEL%
set _RC=%ERRORLEVEL%
call :check_step "robustness" %_RC%

echo.

REM ----------------------------------------------------------------
REM 3. Verification des artefacts produits
REM ----------------------------------------------------------------
echo [3/3] Verification des artefacts produits...

set BENCH_FILE=evaluation\results\benchmark_results_%LABEL%.json
if exist "%BENCH_FILE%" (
    echo   OK : %BENCH_FILE%
) else (
    echo   MANQUANT : %BENCH_FILE%
    set FAILED=1
)

set ROB_FILE=evaluation\results\robustness_results_%LABEL%.json
if exist "%ROB_FILE%" (
    echo   OK : %ROB_FILE%
) else (
    echo   MANQUANT : %ROB_FILE%
    set FAILED=1
)

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
echo Phase 6 terminee avec succes
echo Resultats :
echo   evaluation\results\benchmark_results_%LABEL%.json
echo   evaluation\results\robustness_results_%LABEL%.json
echo ============================================================
echo.
echo Prochaine etape : visualiser avec notebooks\05_evaluation_comparative.ipynb
exit /b 0

:check_step
set STEP_NAME=%~1
set STEP_CODE=%~2
if %STEP_CODE% neq 0 (
    echo   ERREUR dans l'etape : %STEP_NAME% ^(code %STEP_CODE%^)
    set /a FAILED+=1
)
goto :eof
