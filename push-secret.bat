@echo off
REM Push .env.prod into the cluster as the `oura-shared` Secret, read by both
REM oura-mcp and oura-sync (both set existingSecret: oura-shared).
REM
REM Validated first: kubectl --from-env-file keeps quotes and trailing
REM whitespace verbatim, and both fail silently at runtime.
REM
REM Usage:  push-secret.bat [--overwrite|-o] [env-file] [namespace]

setlocal

set "OVERWRITE=0"
set "ENV_FILE="
set "NAMESPACE="

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--overwrite" (
    set "OVERWRITE=1"
    shift
    goto parse_args
)
if /I "%~1"=="-o" (
    set "OVERWRITE=1"
    shift
    goto parse_args
)
if /I "%~1"=="--help" goto usage
if /I "%~1"=="-h" goto usage
if not defined ENV_FILE (
    set "ENV_FILE=%~1"
    shift
    goto parse_args
)
if not defined NAMESPACE (
    set "NAMESPACE=%~1"
    shift
    goto parse_args
)

echo Unexpected argument: %~1
echo.
goto usage

:args_done
if "%ENV_FILE%"=="" set "ENV_FILE=.env.prod"
if "%NAMESPACE%"=="" set "NAMESPACE=oura"

echo Validating %ENV_FILE% ...
python scripts\validate_env_file.py "%ENV_FILE%"
if errorlevel 1 (
    echo.
    echo Validation failed. Nothing was pushed.
    exit /b 1
)

echo.
if "%OVERWRITE%"=="1" (
    echo Overwriting secret oura-shared in namespace %NAMESPACE% ...
    python scripts\render_secret_manifest.py "%ENV_FILE%" oura-shared "%NAMESPACE%" | kubectl apply -f -
) else (
    echo Creating secret oura-shared in namespace %NAMESPACE% ...
    python scripts\render_secret_manifest.py "%ENV_FILE%" oura-shared "%NAMESPACE%" | kubectl create -f -
)
if errorlevel 1 (
    if not "%OVERWRITE%"=="1" (
        echo.
        echo If the Secret already exists, rerun with --overwrite to update it.
    )
    exit /b 1
)

echo.
echo Restarting both deployments so they pick up the new values ...
REM envFrom values are read at container start; a Secret change alone does not
REM restart anything.
kubectl rollout restart deployment oura-mcp -n %NAMESPACE%
if errorlevel 1 exit /b 1
kubectl rollout restart deployment oura-sync -n %NAMESPACE%
if errorlevel 1 exit /b 1

kubectl rollout status deployment oura-mcp -n %NAMESPACE% --timeout=120s
if errorlevel 1 exit /b 1
kubectl rollout status deployment oura-sync -n %NAMESPACE% --timeout=120s

endlocal
exit /b %ERRORLEVEL%

:usage
echo Usage: push-secret.bat [--overwrite^|-o] [env-file] [namespace]
echo.
echo Defaults: env-file=.env.prod namespace=oura
echo Without --overwrite, the command fails if the Secret already exists.
exit /b 1
