@echo off
REM Generate a unique tag using timestamp and git commit hash
for /f "tokens=*" %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set TAG=%%I

REM Get git commit hash (short version)
for /f %%i in ('git rev-parse --short HEAD 2^>nul') do set COMMIT=%%i
if defined COMMIT (
    set TAG=%TAG%-%COMMIT%
) else (
    echo Warning: Not a git repository, using timestamp only for tag
)

echo Building with tag: %TAG%

REM Build context is the repo root (..), not this directory: the Dockerfile
REM also copies the sibling oura-common/ shared source tree.
docker build -t azureks.azurecr.io/oura/oura-sync:%TAG% -t azureks.azurecr.io/oura/oura-sync:latest -f Dockerfile ..
if errorlevel 1 exit /b 1

echo Pushing tagged image: %TAG%
docker push azureks.azurecr.io/oura/oura-sync:%TAG%
if errorlevel 1 exit /b 1

echo Pushing latest tag
docker push azureks.azurecr.io/oura/oura-sync:latest
if errorlevel 1 exit /b 1

echo Deploying with Helm using tag: %TAG%
helm upgrade oura-sync ./helm/oura-sync -f values.prod.yaml --set image.repository=azureks.azurecr.io/oura/oura-sync --set image.tag=%TAG% --install -n oura
if errorlevel 1 exit /b 1

echo Deployment completed successfully with tag: %TAG%
