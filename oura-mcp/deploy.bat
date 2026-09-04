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

REM No ARTIFACT_FEED build args: oura-mcp installs only from public PyPI,
REM so the private feed token is not needed here.
docker build -t azureks.azurecr.io/oura/oura-mcp:%TAG% -t azureks.azurecr.io/oura/oura-mcp:latest -f Dockerfile .
if errorlevel 1 exit /b 1

echo Pushing tagged image: %TAG%
docker push azureks.azurecr.io/oura/oura-mcp:%TAG%
if errorlevel 1 exit /b 1

echo Pushing latest tag
docker push azureks.azurecr.io/oura/oura-mcp:latest
if errorlevel 1 exit /b 1

echo Deploying with Helm using tag: %TAG%
helm upgrade oura-mcp ./helm/oura-mcp -f values.prod.yaml --set image.repository=azureks.azurecr.io/oura/oura-mcp --set image.tag=%TAG% --install -n oura
if errorlevel 1 exit /b 1

echo Deployment completed successfully with tag: %TAG%
