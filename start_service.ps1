param([ValidateRange(1,65535)][int]$Port = 8765)
$ErrorActionPreference = 'Stop'
$taskRoot = $PSScriptRoot
$taskStorage = if ($env:SERVICE_DATA_DIR) { $env:SERVICE_DATA_DIR } else { $taskRoot }
$taskStateFile = Join-Path $taskStorage '.service-state.json'
function Get-LocalHealth([int]$TargetPort) {
    try { return Invoke-RestMethod -Uri "http://127.0.0.1:$TargetPort/api/health" -TimeoutSec 2 } catch { return $null }
}
function Get-ServicePython([string]$ProjectRoot) {
    $taskRuntimePath = Join-Path $ProjectRoot 'runtime.json'
    if (Test-Path -LiteralPath $taskRuntimePath -PathType Leaf) {
        $taskRuntime = Get-Content -LiteralPath $taskRuntimePath -Raw | ConvertFrom-Json
        $taskConfiguredPython = [string]$taskRuntime.python
        if (-not $taskConfiguredPython -or -not (Test-Path -LiteralPath $taskConfiguredPython -PathType Leaf)) {
            throw 'runtime.json 指定的 Python 不存在，请检查 python 路径；安装步骤见 README.md 的 Windows 使用部分。'
        }
        return $taskConfiguredPython
    }
    $taskVenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $taskVenvPython -PathType Leaf) {
        return $taskVenvPython
    }
    throw '未找到 runtime.json 或项目 .venv\Scripts\python.exe。请先按 README.md 的 Windows 使用部分创建 .venv 并安装依赖。'
}
try {
    $taskPython = Get-ServicePython $taskRoot
    $taskIdentityScript = "import hashlib,pathlib,sys; print(hashlib.sha256(str(pathlib.Path(sys.argv[1]).resolve()).casefold().encode('utf-8')).hexdigest()[:24])"
    $taskAppId = (& $taskPython -c $taskIdentityScript $taskRoot).Trim()
    if (Test-Path -LiteralPath $taskStateFile) {
        $taskPrevious = Get-Content -LiteralPath $taskStateFile -Raw | ConvertFrom-Json
        $taskHealth = Get-LocalHealth ([int]$taskPrevious.port)
        if ($taskHealth -and $taskHealth.service -eq 'elder-audio-service' -and $taskHealth.app_id -eq $taskAppId -and $taskHealth.instance_id -eq $taskPrevious.instance_id) {
            Start-Process "http://127.0.0.1:$($taskHealth.port)/"
            exit 0
        }
    }
    $taskHealth = Get-LocalHealth $Port
    if ($taskHealth -and $taskHealth.service -eq 'elder-audio-service' -and $taskHealth.app_id -eq $taskAppId) {
        Start-Process "http://127.0.0.1:$Port/"
        exit 0
    }
    $taskListener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    try { $taskListener.Start() } catch { throw "端口 $Port 已被其他程序占用。请使用 start_service.ps1 -Port 8766 指定另一端口。" } finally { $taskListener.Stop() }
    $taskLogs = Join-Path $taskStorage 'logs'
    New-Item -ItemType Directory -Path $taskLogs -Force | Out-Null
    $taskServicePath = Join-Path $taskRoot 'service.py'
    $taskProcess = Start-Process -FilePath $taskPython -ArgumentList @('-u', ('"' + $taskServicePath + '"'), '--port', [string]$Port) -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskLogs 'service.stdout.log') -RedirectStandardError (Join-Path $taskLogs 'service.stderr.log') -PassThru
    for ($taskAttempt = 0; $taskAttempt -lt 40; $taskAttempt++) {
        Start-Sleep -Milliseconds 250
        $taskHealth = Get-LocalHealth $Port
        if ($taskHealth -and $taskHealth.service -eq 'elder-audio-service' -and $taskHealth.app_id -eq $taskAppId) {
            Start-Process "http://127.0.0.1:$Port/"
            exit 0
        }
        if ($taskProcess.HasExited) { break }
    }
    throw '服务启动未完成，请检查 logs/service.stderr.log。'
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, '老人录音服务', 'OK', 'Error') | Out-Null
    exit 1
}
