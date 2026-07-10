param(
    [int[]]$WaitPids = @(),
    [string]$LogDir = "data\queued_python",
    [string]$Name = "queued_python",
    [string]$PythonArgsJson = "[]",
    [string]$PythonArgsText = ""
)

$ErrorActionPreference = "Continue"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$resolvedLogDir = (Resolve-Path -Path (New-Item -ItemType Directory -Force -Path $LogDir)).Path
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$supervisorLog = Join-Path $resolvedLogDir "$Name`_$stamp.supervisor.log"
$outLog = Join-Path $resolvedLogDir "$Name`_$stamp.out.log"
$errLog = Join-Path $resolvedLogDir "$Name`_$stamp.err.log"

function Write-QueueLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $supervisorLog -Value $line
}

if ($PythonArgsText) {
    $PythonArgs = @($PythonArgsText -split "\|" | Where-Object { $_ -ne "" })
} else {
    try {
        Write-QueueLog "Raw PythonArgsJson=$PythonArgsJson"
        $parsedArgs = ConvertFrom-Json -InputObject $PythonArgsJson
        $PythonArgs = @($parsedArgs | ForEach-Object { [string]$_ })
    } catch {
        Write-QueueLog "Could not parse PythonArgsJson: $($_.Exception.Message)"
        exit 2
    }
}

Write-QueueLog "Queued python command waiting for pids=$($WaitPids -join ',') args=$($PythonArgs -join ' ')"
foreach ($waitPid in $WaitPids) {
    try {
        $process = Get-Process -Id $waitPid -ErrorAction Stop
        Write-QueueLog "Waiting for pid=$waitPid to exit."
        Wait-Process -Id $waitPid
    } catch {
        Write-QueueLog "Pid=$waitPid is not running; continuing."
    }
}

Write-QueueLog "Starting python command."
& ".\.venv\Scripts\python.exe" @PythonArgs > $outLog 2> $errLog
$exitCode = $LASTEXITCODE
Write-QueueLog "Python command exited with code=$exitCode."
exit $exitCode
