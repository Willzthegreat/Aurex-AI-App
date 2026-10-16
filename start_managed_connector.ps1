$ErrorActionPreference = 'Stop'

function New-Secret([int] $Bytes = 48) {
  $buffer = New-Object byte[] $Bytes
  [Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
  [Convert]::ToBase64String($buffer)
}

function New-HexSecret([int] $Bytes = 32) {
  $buffer = New-Object byte[] $Bytes
  [Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
  ([Convert]::ToHexString($buffer)).ToLowerInvariant()
}

function Ensure-UserEnvironment([string] $Name, [string] $Value) {
  [Environment]::SetEnvironmentVariable($Name, $Value, 'User')
}

if (-not $env:AUREX_CONNECTOR_SERVICE_TOKEN) {
  $env:AUREX_CONNECTOR_SERVICE_TOKEN = New-Secret
  Ensure-UserEnvironment 'AUREX_CONNECTOR_SERVICE_TOKEN' $env:AUREX_CONNECTOR_SERVICE_TOKEN
}
if (-not $env:AUREX_WORKER_ENCRYPTION_KEY) {
  $env:AUREX_WORKER_ENCRYPTION_KEY = New-HexSecret
  Ensure-UserEnvironment 'AUREX_WORKER_ENCRYPTION_KEY' $env:AUREX_WORKER_ENCRYPTION_KEY
}
if (-not $env:AUREX_WORKER_INTERNAL_KEY) {
  $env:AUREX_WORKER_INTERNAL_KEY = New-Secret
  Ensure-UserEnvironment 'AUREX_WORKER_INTERNAL_KEY' $env:AUREX_WORKER_INTERNAL_KEY
}

$terminal = if ($env:MT5_TERMINAL_PATH) { $env:MT5_TERMINAL_PATH } else { 'C:\Program Files\MetaTrader 5\terminal64.exe' }
if (-not (Test-Path -LiteralPath $terminal)) {
  throw "MT5 terminal not found at the configured path. Set MT5_TERMINAL_PATH to terminal64.exe."
}
$env:MT5_TERMINAL_PATH = $terminal
$env:AUREX_CONNECTOR_HOST = if ($env:AUREX_CONNECTOR_HOST) { $env:AUREX_CONNECTOR_HOST } else { '127.0.0.1' }
$env:AUREX_CONNECTOR_PORT = if ($env:AUREX_CONNECTOR_PORT) { $env:AUREX_CONNECTOR_PORT } else { '8010' }

if (-not (Test-Path -LiteralPath 'aurex-worker-data')) {
  New-Item -ItemType Directory -Path 'aurex-worker-data' | Out-Null
}
$env:AUREX_WORKER_DATA_DIR = if ($env:AUREX_WORKER_DATA_DIR) { $env:AUREX_WORKER_DATA_DIR } else { (Join-Path (Get-Location) 'aurex-worker-data') }

Write-Host 'Starting Aurex managed MT5 connector on 127.0.0.1:8010.'
Write-Host 'Secrets configured without printing their values.'
& python mt5_session_worker.py
