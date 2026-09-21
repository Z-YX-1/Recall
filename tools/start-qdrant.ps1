# Start Qdrant native server (127.0.0.1:6333)
$qdrantDir = Join-Path $PSScriptRoot "qdrant"
$qdrantExe = Join-Path $qdrantDir "qdrant.exe"

if (-not (Test-Path $qdrantExe)) {
    Write-Error "qdrant.exe not found. Download v1.19.0 from https://github.com/qdrant/qdrant/releases"
    exit 1
}

Start-Process -FilePath $qdrantExe -WorkingDirectory $qdrantDir -WindowStyle Hidden
Write-Host "Qdrant started at http://127.0.0.1:6333"
