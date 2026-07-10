param(
    [int[]]$WaitPids = @(),
    [int]$RuntimeSeconds = 32400,
    [int]$SampleIntervalMs = 500,
    [int]$MaxOutputMb = 300,
    [string]$OutputDir = "data\market_features",
    [string]$SlugPrefix = "btc-updown-5m",
    [int]$WindowSeconds = 300
)

$ErrorActionPreference = "Stop"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$resolvedOutputDir = Join-Path $RepoRoot $OutputDir
$supervisorLog = Join-Path $resolvedOutputDir "queued_recorder_$stamp.log"

function Write-QueueLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $supervisorLog -Value $line
}

Write-QueueLog "Queued recorder waiting for pids=$($WaitPids -join ',') slug_prefix=$SlugPrefix window_seconds=$WindowSeconds."

foreach ($waitPid in $WaitPids) {
    if ($waitPid -le 0) {
        continue
    }
    $process = Get-Process -Id $waitPid -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        Write-QueueLog "Waiting for pid=$waitPid to exit."
        Wait-Process -Id $waitPid
    }
}

$recordOut = Join-Path $resolvedOutputDir "record_${SlugPrefix}_queued_$stamp.out.log"
$recordErr = Join-Path $resolvedOutputDir "record_${SlugPrefix}_queued_$stamp.err.log"
Write-QueueLog "Starting recorder runtime=$RuntimeSeconds sample_ms=$SampleIntervalMs max_mb=$MaxOutputMb output=$OutputDir."

Start-Process -FilePath ".\.venv\Scripts\python.exe" `
    -ArgumentList @(
        "research\record_market_features.py",
        "--output-dir", "$OutputDir",
        "--slug-prefix", "$SlugPrefix",
        "--window-seconds", "$WindowSeconds",
        "--max-runtime-seconds", "$RuntimeSeconds",
        "--sample-interval-ms", "$SampleIntervalMs",
        "--max-output-mb", "$MaxOutputMb"
    ) `
    -WorkingDirectory $RepoRoot `
    -RedirectStandardOutput $recordOut `
    -RedirectStandardError $recordErr `
    -WindowStyle Hidden

Write-QueueLog "Recorder launched out=$recordOut err=$recordErr."
