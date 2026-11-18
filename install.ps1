#requires -Version 5.0
<#
.SYNOPSIS
    Installe la CLI soapalin sur Windows.

.DESCRIPTION
    Cree un venv Python et installe les dependances de la CLI. Aucune interface
    web n'est installee. zmmailbox n'existe pas sous Windows ; la copie des
    boites utilise REST.

.PARAMETER InstallDir
    Dossier d'installation. Par defaut, le dossier du script.

.PARAMETER Python
    Executable Python a utiliser. Autodetection par defaut.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

function Fail {
    param([string]$Message)
    Write-Host ""
    Write-Host "Erreur : $Message" -ForegroundColor Red
    exit 1
}

function Get-PythonVersion {
    param([string]$Exe)
    $text = & $Exe -c "import sys; print('%d.%d.%d' % sys.version_info[:3])" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $text) { return $null }
    try { return [version](([string]($text | Select-Object -First 1)).Trim()) }
    catch { return $null }
}

function Find-Python {
    param([string]$Requested)
    if ($Requested) {
        if (Test-Path -LiteralPath $Requested) { return (Resolve-Path -LiteralPath $Requested).Path }
        $command = Get-Command $Requested -ErrorAction SilentlyContinue
        if ($command) { return $command.Source }
        return $null
    }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $output = & $launcher.Source -3 -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $output) {
            return ([string]($output | Select-Object -First 1)).Trim()
        }
    }
    foreach ($name in @("python3", "python")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (-not $command) { continue }
        $output = & $command.Source -c "import sys; print(sys.executable)" 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $output) { continue }
        $candidate = ([string]($output | Select-Object -First 1)).Trim()
        $version = Get-PythonVersion $candidate
        if ($version -and $version -ge [version]"3.8") { return $candidate }
    }
    return $null
}

function Copy-AppItem {
    param([string]$Source, [string]$Destination)
    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Recurse -Force
    }
    Copy-Item -LiteralPath $Source -Destination $Destination -Recurse -Force
}

$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $InstallDir) { $InstallDir = $ProjectDir }
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
$ProjectDir = [System.IO.Path]::GetFullPath($ProjectDir)

$root = [System.IO.Path]::GetPathRoot($InstallDir)
if ($InstallDir.TrimEnd('\') -eq $root.TrimEnd('\')) {
    Fail "Dossier d'installation invalide : la racine d'un disque n'est pas autorisee."
}
if ($InstallDir.TrimEnd('\') -eq $env:SystemRoot.TrimEnd('\')) {
    Fail "Dossier d'installation invalide : le dossier Windows n'est pas autorise."
}

Write-Step "Recherche de Python"
$PythonExe = Find-Python $Python
if (-not $PythonExe) {
    Fail "Python 3.8+ introuvable. Installez Python 3.8+ depuis python.org, puis relancez ce script."
}
$PythonVersion = Get-PythonVersion $PythonExe
if (-not $PythonVersion -or $PythonVersion -lt [version]"3.8") {
    Fail "Python 3.8+ est requis. Version detectee : $PythonVersion"
}
Write-Host "Python $PythonVersion : $PythonExe"

if ($ProjectDir -ne $InstallDir) {
    Write-Step "Copie de la CLI vers $InstallDir"
    New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
    $items = @(
        "soapalin.py",
        "soapalin.cmd",
        "soaplun",
        "vendor",
        "requirements.txt",
        "pyproject.toml",
        "install.ps1",
        "install.bat",
        "uninstall.ps1",
        "uninstall.bat",
        "README.md"
    )
    foreach ($item in $items) {
        $source = Join-Path $ProjectDir $item
        if (-not (Test-Path -LiteralPath $source)) { Fail "Element manquant : $source" }
        Copy-AppItem -Source $source -Destination (Join-Path $InstallDir $item)
    }
}

Write-Step "Preparation des dossiers"
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "config") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $InstallDir "data") | Out-Null

$VenvDir = Join-Path $InstallDir ".venv"
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Step "Creation du virtualenv"
    & $PythonExe -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPython)) {
        Fail "Impossible de creer le virtualenv."
    }
}

$venvVersion = Get-PythonVersion $VenvPython
if (-not $venvVersion -or $venvVersion -lt [version]"3.8") {
    Fail "Le virtualenv n'utilise pas Python 3.8+."
}
Write-Host "Python du venv : $venvVersion"

& $VenvPython -m pip --version *> $null
if ($LASTEXITCODE -ne 0) {
    & $VenvPython -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) { Fail "pip est absent du venv et ensurepip a echoue." }
}

Write-Step "Mise a jour de pip"
& $VenvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Fail "Impossible de mettre a jour pip." }

Write-Step "Installation des dependances"
& $VenvPython -m pip install -r (Join-Path $InstallDir "requirements.txt")
if ($LASTEXITCODE -ne 0) { Fail "L'installation des dependances a echoue." }

Write-Step "Termine"
Write-Host "Commande CLI :"
Write-Host "  $(Join-Path $InstallDir 'soapalin.cmd') server list"
Write-Host ""
Write-Host "Configuration : $(Join-Path $InstallDir 'config')"
Write-Host "Donnees       : $(Join-Path $InstallDir 'data')"
Write-Host ""
Write-Host "Rappel : sous Windows, zmmailbox est indisponible ; la copie des boites passe par REST."
