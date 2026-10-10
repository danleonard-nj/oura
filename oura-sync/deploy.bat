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

REM config.json carries the production settings and secrets, and is baked
REM into the image. It is gitignored; CI/CD supplies it, and locally it is
REM your own copy built from config.example.json.
if not exist config.json (
    echo config.json is missing. Create it from config.example.json -- see README.md, Configuration.
    exit /b 1
)

REM `framework` installs from the private py-packages feed. The token is read
REM from the environment and handed to the build as a BuildKit secret, so it
REM is never written into the image or this file.
if not defined ARTIFACT_FEED_TOKEN (
    echo ARTIFACT_FEED_TOKEN is not set. Set it to a py-packages feed PAT and rerun.
    exit /b 1
)

echo Building with tag: %TAG%

set DOCKER_BUILDKIT=1
docker build --secret id=artifact_feed_token,env=ARTIFACT_FEED_TOKEN -t azureks.azurecr.io/oura/oura-sync:%TAG% -t azureks.azurecr.io/oura/oura-sync:latest -f Dockerfile .
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
