param(
    [int[]]$WaitPids = @(),
    [string]$WaitPidCsv = "",
    [string]$LogDir = "data\reward_research_supervisor",
    [string]$Name = "reward_research_final",
    [double]$MaxSnapshotAgeHours = 12.0
)

$ErrorActionPreference = "Continue"
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$resolvedLogDir = (Resolve-Path -Path (New-Item -ItemType Directory -Force -Path $LogDir)).Path
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$supervisorLog = Join-Path $resolvedLogDir "$Name`_$stamp.supervisor.log"
$paperOut = Join-Path $resolvedLogDir "$Name`_$stamp.paper.out.log"
$paperErr = Join-Path $resolvedLogDir "$Name`_$stamp.paper.err.log"
$evalOut = Join-Path $resolvedLogDir "$Name`_$stamp.eval.out.log"
$evalErr = Join-Path $resolvedLogDir "$Name`_$stamp.eval.err.log"
$analysisOut = Join-Path $resolvedLogDir "$Name`_$stamp.shadow_analysis.out.log"
$analysisErr = Join-Path $resolvedLogDir "$Name`_$stamp.shadow_analysis.err.log"
$paperDir = "data\market_making_paper_$Name`_$stamp"
$evalDir = "data\reward_fair_value_evaluator_$Name`_$stamp"
$analysisDir = "data\shadow_action_analysis_$Name`_$stamp"

if ($WaitPidCsv) {
    $parsedWaitPids = @(
        $WaitPidCsv -split "," |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_ -match "^\d+$" } |
            ForEach-Object { [int]$_ }
    )
    if ($parsedWaitPids.Count -gt 0) {
        $WaitPids = $parsedWaitPids
    }
}

function Write-SupervisorLog {
    param([string]$Message)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message"
    Add-Content -Path $supervisorLog -Value $line
}

Write-SupervisorLog "Waiting for collector pids=$($WaitPids -join ',')."
foreach ($waitPid in $WaitPids) {
    try {
        $process = Get-Process -Id $waitPid -ErrorAction Stop
        Write-SupervisorLog "Waiting for pid=$waitPid to exit."
        Wait-Process -Id $waitPid
    } catch {
        Write-SupervisorLog "Pid=$waitPid is not running; continuing."
    }
}

Write-SupervisorLog "Running updated market making paper simulator."
& ".\.venv\Scripts\python.exe" "research\market_making_paper_simulator.py" `
    "--history-csv" "data\market_making\candidate_history.csv" `
    "--output-dir" $paperDir `
    > $paperOut 2> $paperErr
$paperExit = $LASTEXITCODE
Write-SupervisorLog "Paper simulator exited code=$paperExit output_dir=$paperDir."

Write-SupervisorLog "Running final reward/fair-value evaluator."
& ".\.venv\Scripts\python.exe" "research\reward_fair_value_evaluator.py" `
    "--maker-history-csv" "data\market_making\candidate_history.csv" `
    "--external-history-csv" "data\external_fair_value\candidate_history.csv" `
    "--maker-paper-events-csv" (Join-Path $paperDir "maker_paper_events.csv") `
    "--output-dir" $evalDir `
    "--max-snapshot-age-hours" "$MaxSnapshotAgeHours" `
    > $evalOut 2> $evalErr
$evalExit = $LASTEXITCODE
Write-SupervisorLog "Evaluator exited code=$evalExit output_dir=$evalDir."

Write-SupervisorLog "Running shadow action stability analysis."
& ".\.venv\Scripts\python.exe" "research\analyze_shadow_actions.py" `
    "--history-csv" "data\reward_fair_value_evaluator_live\shadow_action_history.csv" `
    "--output-dir" $analysisDir `
    > $analysisOut 2> $analysisErr
$analysisExit = $LASTEXITCODE
Write-SupervisorLog "Shadow action analysis exited code=$analysisExit output_dir=$analysisDir."

if ($paperExit -ne 0 -or $evalExit -ne 0 -or $analysisExit -ne 0) {
    exit 1
}
exit 0
