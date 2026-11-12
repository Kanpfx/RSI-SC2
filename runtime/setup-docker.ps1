# Prepare Python in a Docker volume; keep the existing AiArena image unchanged.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$project = Split-Path -Parent $PSScriptRoot
$image = 'aiarena/arenaclient-sc2:v0.8.0'
$volume = 'rsi-sc2-python'
$archiveName = 'cpython-3.12.16+20261110-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'
$archiveHash = '6a9e62dad033790aef722dbd370e0ce76ce17b73627ba3ae679f537049350b39'
$archiveUrl = 'https://github.com/astral-sh/python-build-standalone/releases/download/20261110/cpython-3.12.16%2B20261110-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz'

docker image inspect $image --format '{{.Id}}'
if ($LASTEXITCODE -ne 0) { throw "The existing image $image is unavailable. Start Docker Desktop and check its local images." }

$cache = Join-Path $project 'outputs/setup'
New-Item -ItemType Directory -Path $cache -Force | Out-Null
$archive = Join-Path $cache $archiveName
if (-not (Test-Path -LiteralPath $archive)) {
    Write-Host 'Downloading standalone Linux Python 3.12 (about 35 MB)...'
    Invoke-WebRequest -UseBasicParsing -Uri $archiveUrl -OutFile "$archive.partial"
    if ((Get-FileHash -LiteralPath "$archive.partial" -Algorithm SHA256).Hash -ne $archiveHash) {
        throw 'Python archive checksum mismatch; the download was not installed.'
    }
    Move-Item -LiteralPath "$archive.partial" -Destination $archive
}
if ((Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash -ne $archiveHash) {
    throw "Python archive checksum mismatch: $archive"
}

docker volume create $volume
if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python environment volume.' }

$initialize = @'
set -eu
if [ ! -x /opt/rsi/python/bin/python3 ]; then
    tar --no-same-owner -xzf "/bootstrap/$1" -C /opt/rsi
fi
exec /opt/rsi/python/bin/python3 -B /setup/setup_docker.py
'@
docker run --rm --pull=never --init --read-only --cap-drop ALL --security-opt no-new-privileges `
    --tmpfs /tmp --env HOME=/tmp --env PYTHONDONTWRITEBYTECODE=1 `
    --env LITELLM_LOCAL_MODEL_COST_MAP=True --env OPENHANDS_SUPPRESS_BANNER=1 `
    --mount "type=volume,source=$volume,target=/opt/rsi" `
    --mount "type=bind,source=$cache,target=/bootstrap,readonly" `
    --mount "type=bind,source=$PSScriptRoot,target=/setup,readonly" `
    --mount "type=bind,source=$project/pyproject.toml,target=/pyproject.toml,readonly" `
    --entrypoint /bin/sh $image -c $initialize setup $archiveName
if ($LASTEXITCODE -ne 0) { throw 'Python environment initialization failed; fix the reported error and rerun this script.' }

Write-Host 'Environment ready. Run: docker compose run --rm experiment --rounds 1'
