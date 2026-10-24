# Ollama install + glm4:9b model setup for aigateway E2E validation.
# Usage: powershell -ExecutionPolicy Bypass -File scripts/setup_ollama.ps1
# (Run from the repository root.)

$ErrorActionPreference = "Stop"

$zipUrl = "https://github.com/ollama/ollama/releases/download/v0.40.10/ollama-windows-amd64.zip"
$zipPath = "$env:TEMP\ollama-windows-amd64.zip"
$installDir = "$env:LOCALAPPDATA\Programs\Ollama"

# 1) install (skip if already available)
if (-not (Test-Path "$installDir\ollama.exe")) {
    if (-not (Test-Path $zipPath)) {
        Write-Host "Downloading Ollama (~1.6GB)..."
        # Slow networks: use the parallel downloader from this repo instead.
        python scripts/download_parallel.py $zipUrl $zipPath 40
    }
    Expand-Archive -Path $zipPath -DestinationPath $installDir -Force
}
$env:Path = "$installDir;$env:Path"
Write-Host "ollama version: $(ollama --version)"

# 2) start server in background if not running
$null = ollama list 2>$null
if ($LASTEXITCODE -ne 0) {
    Start-Process -FilePath "$installDir\ollama.exe" -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

# 3) model: prefer the fast ModelScope GGUF + Modelfile; fall back to ollama pull
$ggufPath = "$env:TEMP\glm-4-9b-chat.Q4_K_S.gguf"
if (Test-Path $ggufPath) {
    ollama create glm4:9b -f Modelfile
} else {
    ollama pull glm4:9b
}
ollama list
