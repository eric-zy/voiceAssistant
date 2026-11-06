$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath 'vendor\CosyVoice\cosyvoice')) {
    git clone --depth 1 --recurse-submodules --shallow-submodules https://github.com/QwenAudio/CosyVoice.git vendor/CosyVoice
    if ($LASTEXITCODE -ne 0) { throw 'CosyVoice source download failed.' }
    git -C vendor/CosyVoice fetch --depth 1 origin 074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
    if ($LASTEXITCODE -ne 0) { throw 'Pinned source revision download failed.' }
    git -C vendor/CosyVoice checkout --detach 074ca6dc9e80a2f424f1f74b48bdd7d3fea531cc
    if ($LASTEXITCODE -ne 0) { throw 'Pinned source revision checkout failed.' }
    git -C vendor/CosyVoice submodule update --init --recursive --depth 1
    if ($LASTEXITCODE -ne 0) { throw 'CosyVoice submodule preparation failed.' }
}
uv venv --python 3.11 --allow-existing .venv-cosy
if ($LASTEXITCODE -ne 0) { throw 'Python environment preparation failed.' }
uv pip install --python .venv-cosy/Scripts/python.exe -r cosy-requirements.txt --build-constraint cosy-build-constraints.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& .venv-cosy/Scripts/python.exe download_cosyvoice.py
if ($LASTEXITCODE -ne 0) { throw 'Model download failed. Re-run the download command to retry.' }
