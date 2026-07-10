param(
    [int]$DelaySeconds = 32400,
    [switch]$RunT270
)

$ErrorActionPreference = "Continue"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = Join-Path $RepoRoot "data\post_away_analysis"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$supervisorLog = Join-Path $logDir "post_away_$stamp.log"

function Write-PostLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $supervisorLog -Value $line
}

Write-PostLog "Post-away analysis scheduled delay_seconds=$DelaySeconds run_t270=$RunT270."
Start-Sleep -Seconds ([Math]::Max($DelaySeconds, 0))

Write-PostLog "Running BTC 5m vs 15m microstructure comparison."
$compareLog = Join-Path $logDir "btc_window_compare_$stamp.log"
& ".\.venv\Scripts\python.exe" "research\compare_btc_window_microstructure.py" `
    "--output-dir" "data\btc_window_microstructure_compare_after_away" `
    *> $compareLog

Write-PostLog "Running market making rewards paper simulator."
$makerLog = Join-Path $logDir "maker_paper_$stamp.log"
& ".\.venv\Scripts\python.exe" "research\market_making_paper_simulator.py" `
    "--history-csv" "data\market_making\candidate_history.csv" `
    "--output-dir" "data\market_making_paper_after_away" `
    *> $makerLog

if ($RunT270) {
    Write-PostLog "Running one-shot strict t270 validator."
    $t270Log = Join-Path $logDir "t270_strict_$stamp.log"
    & ".\.venv\Scripts\python.exe" "research\t270_frozen_validator.py" `
        "--output-dir" "data\t270_frozen_validation_strict_fill" `
        "--fetch-binance-labels" `
        "--holdout-start-ts" "1783407300" `
        "--min-top-shares" "5" `
        "--require-uncrossed-books" `
        "--max-quote-age-ms" "2000" `
        *> $t270Log
}

Write-PostLog "Post-away analysis complete."
