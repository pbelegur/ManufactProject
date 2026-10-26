[CmdletBinding(DefaultParameterSetName = 'Quick')]
param(
    [Parameter(ParameterSetName = 'Quick')][switch]$Quick,
    [Parameter(Mandatory = $true, ParameterSetName = 'Full')][switch]$Full,
    [string]$Kubeconfig = '.local/floci.kubeconfig',
    [string]$OutputDirectory
)

$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    $python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        throw 'Missing .venv. Follow the existing Python setup instructions in README.md.'
    }
    $mode = 'Quick'
    if ($Full) { $mode = 'Full' }
    $demoArgs = @('scripts/demo.py', '--mode', $mode, '--kubeconfig', $Kubeconfig)
    if ($OutputDirectory) { $demoArgs += @('--output-directory', $OutputDirectory) }
    & $python @demoArgs
    $demoExitCode = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $demoExitCode
