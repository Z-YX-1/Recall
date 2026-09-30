# 启动 Qdrant 原生服务（127.0.0.1:6333）
#
# ⚠️ 本机 ExecutionPolicy 为 Restricted，.ps1 直接运行会报 UnauthorizedAccess。
#    请这样调用（**不需要**改系统策略）：
#        powershell -NoProfile -ExecutionPolicy Bypass -File tools\start-qdrant.ps1
#
# 想**看见它的日志**（排查时有用）：加 -Visible，去掉隐藏窗口。
#
# 当前版本：Qdrant v1.19.1（Windows x86_64）。
#   - v1.19.0 有"建 payload 索引前未 flush CoW 段"的缺陷，会让建索引时报
#     `IO Error: 拒绝访问 (os error 5)` 并把服务打进降级态（详见 spec/tech.md §12.3）；
#   - 旧版二进制留档在 tools/qdrant/qdrant-1.19.0.exe.bak，换回该文件即回退。
param(
    [switch] $Visible
)

$qdrantDir = Join-Path $PSScriptRoot "qdrant"
$qdrantExe = Join-Path $qdrantDir "qdrant.exe"

if (-not (Test-Path $qdrantExe)) {
    Write-Error "qdrant.exe not found. 从 https://github.com/qdrant/qdrant/releases 下载（当前应为 v1.19.1，资产 qdrant-x86_64-pc-windows-msvc.zip）"
    exit 1
}

# 已在运行就不重复起（重复启动会因端口被占而失败）
$running = Get-Process qdrant -ErrorAction SilentlyContinue
if ($running) {
    Write-Host "Qdrant 已在运行（PID $($running.Id)），未重复启动。"
    exit 0
}

# ⚠️ 只绑回环（安全，2026-09-30 实测补上）：
# Qdrant 默认 service.host = 0.0.0.0 且**本身没有 API key**，等于把整库（可读可删）交给同网段；
# 本机 WLAN 恰是 Public 配置、防火墙又有一条 qdrant.exe 入站放行规则 ⇒ 实测 192.168.0.3:6333 可连。
# 这里用官方支持的环境变量覆盖（等价于 config/config.yaml 里的 service.host），
# 子进程会继承本进程环境。真正兜底还应在防火墙里删掉那条放行规则（见 spec/runbook.md §7）。
$env:QDRANT__SERVICE__HOST = '127.0.0.1'

$options = @{ FilePath = $qdrantExe; WorkingDirectory = $qdrantDir }
if (-not $Visible) { $options.WindowStyle = 'Hidden' }
Start-Process @options

# 等它就绪再报成功 —— 避免"命令返回了、服务其实还没起来"的假绿
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 1
    try {
        $health = Invoke-RestMethod 'http://127.0.0.1:6333/healthz' -TimeoutSec 3
        if ($health) {
            $version = (Invoke-RestMethod 'http://127.0.0.1:6333/' -TimeoutSec 3).version
            Write-Host "Qdrant $version 已就绪： http://127.0.0.1:6333  ($health)"
            exit 0
        }
    }
    catch { }
}
Write-Warning "Qdrant 启动后 20 秒内未就绪，请查看它的窗口/日志。"
exit 1
