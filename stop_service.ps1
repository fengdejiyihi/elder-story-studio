$ErrorActionPreference = 'Stop'
try {
    $taskStorage = if ($env:SERVICE_DATA_DIR) { $env:SERVICE_DATA_DIR } else { $PSScriptRoot }
    $taskStateFile = Join-Path $taskStorage '.service-state.json'
    if (-not (Test-Path -LiteralPath $taskStateFile -PathType Leaf)) { exit 0 }
    $taskState = Get-Content -LiteralPath $taskStateFile -Raw | ConvertFrom-Json
    $taskUrl = "http://127.0.0.1:$($taskState.port)"
    $taskHealth = Invoke-RestMethod -Uri ($taskUrl + '/api/health') -TimeoutSec 3
    if ($taskHealth.service -ne 'elder-audio-service' -or $taskHealth.app_id -ne $taskState.app_id -or $taskHealth.instance_id -ne $taskState.instance_id -or $taskHealth.pid -ne $taskState.pid) {
        throw '此端口不属于记录中的本机服务，未执行停止操作。'
    }
    $taskHeaders = @{'X-CSRF-Token'=[string]$taskState.token}
    if ($env:SERVICE_API_TOKEN) { $taskHeaders['Authorization'] = 'Bearer ' + $env:SERVICE_API_TOKEN }
    Invoke-RestMethod -Uri ($taskUrl + '/api/shutdown') -Method Post -ContentType 'application/json' -Headers $taskHeaders -Body '{}' -TimeoutSec 5 | Out-Null
} catch {
    $taskMessage = '服务未停止。若仍有处理或排队中的任务，请等待完成后再停止；也可检查服务是否已关闭。'
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($taskMessage, '老人录音服务', 'OK', 'Information') | Out-Null
    exit 1
}
