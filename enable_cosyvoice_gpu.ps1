$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
try {
    $gpuService = Invoke-RestMethod 'http://127.0.0.1:50001/health' -TimeoutSec 2
} catch { $gpuService = $null }
if ($gpuService.ready) { throw 'Stop the CosyVoice service before replacing PyTorch packages.' }
$gpuTorch = 'https://mirrors.aliyun.com/pytorch-wheels/cu121/torch-2.3.1%2Bcu121-cp311-cp311-win_amd64.whl#sha256=5a578516d0caf233993b3161d7dce1472bb917c59dd767c51921cd6696c3f3f7'
uv pip install --python .venv-cosy/Scripts/python.exe $gpuTorch 'torchaudio==2.3.1+cu121' --index-url https://download.pytorch.org/whl/cu121
if ($LASTEXITCODE -ne 0) { throw 'CUDA PyTorch installation failed.' }
& .venv-cosy/Scripts/python.exe -c "import torch; print('CUDA available:', torch.cuda.is_available()); print('PyTorch:', torch.__version__)"
