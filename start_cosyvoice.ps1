$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    $cosyHealth = Invoke-RestMethod 'http://127.0.0.1:50001/health' -TimeoutSec 2
    if ($cosyHealth.ready -and $cosyHealth.sample_rate) {
        Write-Host 'CosyVoice is already running at http://127.0.0.1:50001'
        return
    }
} catch { }
$cosyPython = Join-Path $PSScriptRoot '.venv-cosy\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $cosyPython)) { throw 'Run ./setup_cosyvoice.ps1 first.' }
if (-not (Test-Path -LiteralPath 'models\CosyVoice2-0.5B\cosyvoice2.yaml')) { throw 'Model not ready. Run .venv-cosy/Scripts/python.exe download_cosyvoice.py first.' }
& $cosyPython cosy_server.py
