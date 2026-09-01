<#
.SYNOPSIS
    Build the test virtualenv for uponorX265.

.DESCRIPTION
    Creates .venv, installs requirements_test.txt, and — on Windows — puts
    tests/_win_stubs on the interpreter's path via a .pth file.

    That last step is the non-obvious one. pytest-homeassistant-custom-component
    imports homeassistant.runner while loading its plugin, and runner imports the
    POSIX-only `fcntl` and `resource` modules at module scope, so pytest cannot
    even start on Windows. A .pth is used rather than conftest.py because plugin
    entry points load before any conftest is read; .pth files run at interpreter
    startup, which is early enough.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\setup-test-env.ps1
#>

[CmdletBinding()]
param(
    # Python to build the venv from. HA 2026.8 supports 3.13 and 3.14.
    [string]$PythonVersion = '3.14',
    # Recreate .venv from scratch instead of reusing an existing one.
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$repoRoot = Split-Path -Parent $PSScriptRoot
$venv = Join-Path $repoRoot '.venv'
$venvPython = Join-Path $venv 'Scripts\python.exe'

if ($Force -and (Test-Path $venv)) {
    Write-Host "Removing existing $venv"
    Remove-Item -Recurse -Force $venv
}

if (-not (Test-Path $venvPython)) {
    Write-Host "Creating venv at $venv (Python $PythonVersion)"
    & py "-$PythonVersion" -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "Failed to create venv with Python $PythonVersion" }
}

Write-Host 'Installing test requirements'
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'Failed to upgrade pip' }
& $venvPython -m pip install -r (Join-Path $repoRoot 'requirements_test.txt')
if ($LASTEXITCODE -ne 0) { throw 'Failed to install requirements_test.txt' }

# Windows only: the real fcntl/resource exist everywhere else, and the stdlib
# copies take precedence over a .pth path anyway, but there is no reason to
# add the shim on a platform that does not need it.
if ($IsWindows -or $env:OS -eq 'Windows_NT') {
    $stubs = Join-Path $repoRoot 'tests\_win_stubs'
    $sitePackages = Join-Path $venv 'Lib\site-packages'
    $pth = Join-Path $sitePackages '_uponor_win_stubs.pth'
    # Forward slashes: a .pth line is used verbatim, and backslashes in it are
    # not unescaped, so a path with e.g. \t in it would silently not resolve.
    # WriteAllText with an explicit BOM-less encoding, because Windows
    # PowerShell 5.1's `-Encoding utf8` always emits a BOM and site.py takes
    # each .pth line as a literal path — a leading U+FEFF makes it not resolve.
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($pth, $stubs.Replace('\', '/') + "`n", $utf8NoBom)
    Write-Host "Wrote $pth -> $stubs"
}

Write-Host ''
Write-Host 'Done. Run the suite with:'
Write-Host '    .venv\Scripts\python.exe -m pytest'
