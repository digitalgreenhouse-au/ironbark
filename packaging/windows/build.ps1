<#
.SYNOPSIS
  Builds Ironbark for Windows: the packaged program, the installer, the portable zip and checksums.

.DESCRIPTION
  1. Runs the tests (skip with -SkipTests).
  2. PyInstaller packages the app (ironbark.spec) into a folder holding Ironbark.exe, with every
     bundled library's licence in its licenses folder (the build stops if one is missing).
  3. Runs the *built* Ironbark.exe --self-check; a failure stops the build.
  4. Inno Setup makes Ironbark-Setup-X.Y.Z.exe (skipped, with a warning, if Inno Setup 6 isn't installed).
  5. Zips the folder as Ironbark-X.Y.Z-windows-portable.zip, and writes SHA256SUMS.txt.

  Run from anywhere:  pwsh packaging\windows\build.ps1  [-OutDir <folder>] [-SkipTests] [-NoSync]
  -NoSync: don't update the environment first (use when Ironbark is running from this folder,
  which locks its .venv launcher).
#>
param(
    [string]$OutDir = "dist\windows",
    [switch]$SkipTests,
    [switch]$NoSync
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repo
$OutDir = [IO.Path]::GetFullPath((Join-Path $repo $OutDir))
$work = Join-Path $repo "build\windows"

$version = [regex]::Match((Get-Content "ironbark\__init__.py" -Raw), '__version__ = "([^"]+)"').Groups[1].Value
if (-not $version) { throw "No __version__ in ironbark\__init__.py" }
Write-Host "Building Ironbark $version for Windows -> $OutDir"

function Step($name, [scriptblock]$action) {
    Write-Host "`n== $name" -ForegroundColor Cyan
    & $action
    if ($LASTEXITCODE) { throw "$name failed (exit code $LASTEXITCODE)" }
}

$uvRun = if ($NoSync) { @("run", "--no-sync") } else { @("run", "--locked") }
if (-not $NoSync) { Step "Install exactly as locked" { uv sync --locked } }
if (-not $SkipTests) { Step "Tests" { uv @uvRun pytest -q } }

Step "Package (PyInstaller)" {
    uv @uvRun pyinstaller packaging\windows\ironbark.spec --noconfirm --clean `
        --distpath $OutDir --workpath $work
}
$app = Join-Path $OutDir "Ironbark"

Step "Self-check of the built program" {
    $report = Join-Path $work "self-check.txt"
    Remove-Item $report -ErrorAction SilentlyContinue
    $run = Start-Process (Join-Path $app "Ironbark.exe") -ArgumentList "--self-check", "`"$report`"" -Wait -PassThru
    if (Test-Path $report) { Get-Content $report | Write-Host }
    $global:LASTEXITCODE = $run.ExitCode
}

$iscc = @(
    (Get-Command iscc -ErrorAction SilentlyContinue).Source,
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe"
) | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
$files = @()
if ($iscc) {
    Step "Installer (Inno Setup)" {
        & $iscc /Q "/DAppVersion=$version" "/DSourceDir=$app" "/DOutputDir=$OutDir" packaging\windows\ironbark.iss
    }
    $files += "Ironbark-Setup-$version.exe"
} else {
    Write-Warning "Inno Setup 6 not found: no installer made (https://jrsoftware.org/isinfo.php)."
}

Step "Portable zip" {
    $zip = Join-Path $OutDir "Ironbark-$version-windows-portable.zip"
    Remove-Item $zip -ErrorAction SilentlyContinue
    Compress-Archive -Path $app -DestinationPath $zip -CompressionLevel Optimal
}
$files += "Ironbark-$version-windows-portable.zip"

Step "Checksums" {
    $lines = foreach ($name in $files) {
        "{0}  {1}" -f (Get-FileHash (Join-Path $OutDir $name) -Algorithm SHA256).Hash.ToLower(), $name
    }
    Set-Content (Join-Path $OutDir "SHA256SUMS.txt") $lines -Encoding ascii
    $lines | Write-Host
}

Write-Host "`nDone: Ironbark $version" -ForegroundColor Green
foreach ($name in $files + "SHA256SUMS.txt") {
    $item = Get-Item (Join-Path $OutDir $name)
    Write-Host ("  {0}  ({1:N1} MB)" -f $item.FullName, ($item.Length / 1MB))
}
