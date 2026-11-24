[CmdletBinding()]
param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '1.3.2',
    [string]$PythonExecutable,
    [string]$InnoCompiler,
    [switch]$SkipInstaller
)

$ErrorActionPreference = 'Stop'

function Invoke-BuildCommand {
    param([string]$Executable, [string[]]$Arguments)

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Executable failed with exit code $LASTEXITCODE."
    }
}

function Find-InnoCompiler {
    param([string]$RequestedPath)

    if ($RequestedPath) {
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }

    $onPath = Get-Command ISCC.exe -CommandType Application -ErrorAction SilentlyContinue
    if ($onPath) {
        return $onPath.Source
    }

    $programFolders = @(
        $env:ProgramFiles
        ${env:ProgramFiles(x86)}
        (Join-Path $env:LOCALAPPDATA 'Programs')
    ) | Where-Object { $_ }
    foreach ($folder in $programFolders) {
        foreach ($edition in @('Inno Setup 7', 'Inno Setup 6')) {
            $candidate = Join-Path $folder "$edition\ISCC.exe"
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                return $candidate
            }
        }
    }

    # Inno Setup can be installed in a non-default directory.
    $compileAssociation = Get-Item -LiteralPath (
        'Registry::HKEY_CLASSES_ROOT\InnoSetupScriptFile\shell\Compile\Command'
    ) -ErrorAction SilentlyContinue
    if ($compileAssociation -and $compileAssociation.GetValue('') -match '^"([^"]+)"') {
        $candidate = Join-Path (Split-Path -Parent $Matches[1]) 'ISCC.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return $candidate
        }
    }

    throw 'Install Inno Setup 6.3 or later, specify -InnoCompiler, or use -SkipInstaller.'
}

$compilerPath = if (-not $SkipInstaller) { Find-InnoCompiler $InnoCompiler }
$applicationName = 'HM50xx-Spectral-Monitor'
$repositoryRoot = $PSScriptRoot
$buildDirectory = Join-Path $repositoryRoot '.build'
$distributionDirectory = Join-Path $repositoryRoot 'dist'
$environmentDirectory = Join-Path $repositoryRoot '.build-venv'
$buildPython = Join-Path $environmentDirectory 'Scripts\python.exe'
$applicationIcon = Join-Path $repositoryRoot 'packaging\spectral-monitor.ico'
$previousPyInstallerCache = $env:PYINSTALLER_CONFIG_DIR
$env:PYINSTALLER_CONFIG_DIR = Join-Path $buildDirectory 'pyinstaller-cache'

Push-Location -LiteralPath $repositoryRoot
try {
    foreach ($requiredFile in @('LICENSE', 'README.md')) {
        if (-not (Test-Path -LiteralPath $requiredFile -PathType Leaf)) {
            throw "Missing release file: $requiredFile."
        }
    }

    if (-not (Test-Path -LiteralPath $buildPython -PathType Leaf)) {
        if (-not $PythonExecutable) {
            $pythonCommand = Get-Command python.exe -CommandType Application
            $PythonExecutable = $pythonCommand.Source
        }
        Invoke-BuildCommand $PythonExecutable @('-m', 'venv', $environmentDirectory)
    }

    $platform = Invoke-BuildCommand $buildPython @(
        '-c', 'import sysconfig; import tkinter; tkinter.Tcl(); print(sysconfig.get_platform())'
    )
    if ($platform -ne 'win-amd64') {
        throw "The installer targets x64 Windows. Use a Windows x64 Python interpreter. Detected $platform."
    }

    Invoke-BuildCommand $buildPython @(
        '-m', 'pip', '--disable-pip-version-check', 'install',
        '--cache-dir', (Join-Path $buildDirectory 'pip-cache'),
        '-r', 'requirements.txt', '-r', 'requirements-build.txt'
    )

    Invoke-BuildCommand $buildPython @(
        '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
        '--name', $applicationName,
        '--icon', $applicationIcon,
        '--distpath', $distributionDirectory,
        '--workpath', (Join-Path $buildDirectory 'pyinstaller'),
        '--specpath', $buildDirectory,
        'main.py'
    )

    $applicationDirectory = Join-Path $distributionDirectory $applicationName
    $licenseDirectory = Join-Path $applicationDirectory 'licenses'
    New-Item -ItemType Directory -Path $licenseDirectory -Force | Out-Null
    Copy-Item -Path 'packaging\licenses\*.txt' -Destination $licenseDirectory

    $pythonLicense = Invoke-BuildCommand $buildPython @(
        '-c', "from pathlib import Path; import sys; print(Path(sys.base_prefix) / 'LICENSE.txt')"
    )
    Copy-Item -LiteralPath $pythonLicense -Destination (Join-Path $licenseDirectory 'Python.txt')

    $pyInstallerLicense = Invoke-BuildCommand $buildPython @(
        '-c', "from importlib.metadata import files; print(next(path.locate() for path in files('PyInstaller') if path.name == 'COPYING.txt'))"
    )
    Copy-Item -LiteralPath $pyInstallerLicense -Destination (Join-Path $licenseDirectory 'PyInstaller.txt')

    Copy-Item -LiteralPath 'LICENSE', 'README.md' -Destination $applicationDirectory

    $sourceArchive = Join-Path $distributionDirectory "$applicationName-Source.zip"
    Invoke-BuildCommand $buildPython @('packaging\create_source_archive.py', $sourceArchive)
    Copy-Item -LiteralPath $sourceArchive -Destination $applicationDirectory

    if (-not $SkipInstaller) {
        Invoke-BuildCommand $compilerPath @("/DAppVersion=$Version", 'packaging\SpectralMonitor.iss')
        Write-Host "Installer: $(Join-Path $distributionDirectory "$applicationName-Setup.exe")"
    }
    Write-Host "Portable app: $(Join-Path $applicationDirectory "$applicationName.exe")"
    Write-Host "Source archive: $sourceArchive"
}
finally {
    $env:PYINSTALLER_CONFIG_DIR = $previousPyInstallerCache
    Pop-Location
}
