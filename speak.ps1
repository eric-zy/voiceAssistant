param([string]$InputPath, [string]$OutputPath)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$request = Get-Content -LiteralPath $InputPath -Raw -Encoding UTF8 | ConvertFrom-Json
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    if ($request.voice) { $synth.SelectVoice($request.voice) }
    $synth.SetOutputToWaveFile($OutputPath)
    $synth.Speak([string]$request.text)
} finally { $synth.Dispose() }
