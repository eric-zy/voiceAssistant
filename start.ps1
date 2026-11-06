param([switch]$UpdateKey, [ValidateSet('balanced','smooth','stream')][string]$ClonePlayback)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if ($PSBoundParameters.ContainsKey('ClonePlayback')) { $env:VOICE_CLONE_PLAYBACK = $ClonePlayback }
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Please create .venv and install requirements.txt first.' }
if ($UpdateKey -or -not $env:DEEPSEEK_API_KEY) {
    $secureKey = Read-Host 'DeepSeek API Key (hidden)' -AsSecureString
    $keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $env:DEEPSEEK_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer).Trim()
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
        $secureKey.Dispose()
    }
}
& $python check_api.py --env
if ($LASTEXITCODE -ne 0) { throw 'API verification failed. See the diagnostic above; the web server was not started.' }
Write-Host 'Starting chat at http://127.0.0.1:8765 - keep this terminal open; Ctrl+C stops the server.'
$playbackMode = if ($env:VOICE_CLONE_PLAYBACK) { $env:VOICE_CLONE_PLAYBACK } else { 'balanced' }
Write-Host "Custom voice playback: $playbackMode (balanced = short first sentence, then remaining reply)."
& $python -m uvicorn app:app --host 127.0.0.1 --port 8765 --ws-max-size 2097152
$chatExitCode = $LASTEXITCODE
if ($chatExitCode -ne 0) { throw "Chat server exited with code $chatExitCode. Review the error above." }
Write-Host 'Chat server has stopped. Run ./start.ps1 to start it again.'
