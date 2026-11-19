#requires -Version 5.0
<#
.SYNOPSIS
    Uninstall the soapalin CLI.

.DESCRIPTION
    By default, the virtual environment is removed and config\ / data\ are kept.
    -Purge also removes config\ and data\.
#>
[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [switch]$Purge,
    [switch]$Yes
)

$ErrorActionPreference = "Stop"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host "==> $Message" -ForegroundColor Cyan
}

if ($InstallDir) {
    $AppDir = [System.IO.Path]::GetFullPath($InstallDir)
} else {
    $AppDir = $PSScriptRoot
}

$root = [System.IO.Path]::GetPathRoot($AppDir)
if ($AppDir.TrimEnd('\') -eq $root.TrimEnd('\')) {
    Write-Host "Erreur : la racine d'un disque n'est pas autorisee." -ForegroundColor Red
    exit 1
}
if ($AppDir.TrimEnd('\') -eq $env:SystemRoot.TrimEnd('\')) {
    Write-Host "Erreur : le dossier Windows n'est pas autorise." -ForegroundColor Red
    exit 1
}

if (-not $Yes) {
    $extra = ""
    if ($Purge) { $extra = " et les dossiers config\data" }
    $answer = Read-Host "Supprimer le venv$extra ? [o/N]"
    if ($answer -notmatch '^(o|oui|y|yes)$') {
        Write-Host "Annule."
        exit 0
    }
}

Write-Step "Suppression du virtualenv"
$VenvDir = Join-Path $AppDir ".venv"
if (Test-Path -LiteralPath $VenvDir) {
    Remove-Item -LiteralPath $VenvDir -Recurse -Force
    Write-Host "Virtualenv supprime : $VenvDir"
} else {
    Write-Host "Aucun virtualenv dans $AppDir."
}

if ($Purge) {
    Write-Step "Suppression de la configuration et des donnees"
    foreach ($name in @("config", "data")) {
        $path = Join-Path $AppDir $name
        if (Test-Path -LiteralPath $path) {
            Remove-Item -LiteralPath $path -Recurse -Force
            Write-Host "Supprime : $path"
        }
    }
}

Write-Step "Termine"
Write-Host "Le code et les scripts sont conserves dans : $AppDir"
if (-not $Purge) { Write-Host "La configuration et les donnees sont conservees." }
