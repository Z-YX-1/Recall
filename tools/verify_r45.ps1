#requires -Version 5.1
<#
.SYNOPSIS
    R-45 验收脚本：GET /kb/stats。

.DESCRIPTION
    对照 spec/roadmap.md 的 R-45 与 spec/tech.md §8 / §17 决策记录 14，
    验收三条不变量：

      A. 同源同形 —— REST 与 MCP 工具 kb_stats 逐字段相等
      B. 只读     —— 反复调用不改变任何状态（payload 稳定 + registry 库文件不动）
      C. 未破坏   —— 既有四个端点与 MCP 工具名不受影响

    只读操作：脚本自身不写任何文件、不改任何状态。$SkipPytest 可跳过第 7 项。

.PARAMETER Base
    recall.api 服务地址，默认 http://127.0.0.1:8000 。

.PARAMETER Python
    recall conda 环境的解释器路径。

.PARAMETER RegistryDb
    SQLite 注册表文件路径（用于"只读性"取证）。

.PARAMETER SkipPytest
    跳过第 7 项（MCP 同源同形回归测试）。

.EXAMPLE
    powershell -NoProfile -File tools/verify_r45.ps1

.NOTES
    本文件必须保存为 **UTF-8 with BOM**：Windows PowerShell 5.1 在缺少 BOM 时
    会按系统 ANSI 代码页读取 .ps1，中文提示会全部变成乱码。
#>
[CmdletBinding()]
param(
    [string] $Base = 'http://127.0.0.1:8000',
    [string] $Python = 'D:\miniconda\envs\recall\python.exe',
    [string] $RegistryDb = 'D:\Project\Recall\data\registry.db',
    [switch] $SkipPytest
)

$ErrorActionPreference = 'Stop'
$script:Pass = 0
$script:Fail = 0

function Write-Check {
    param([string] $Name, [bool] $Ok, [string] $Detail = '')
    if ($Ok) {
        $script:Pass++
        Write-Host ('  [ OK ] ' + $Name) -ForegroundColor Green
    }
    else {
        $script:Fail++
        Write-Host ('  [FAIL] ' + $Name) -ForegroundColor Red
        if ($Detail) { Write-Host ('         -> ' + $Detail) -ForegroundColor Yellow }
    }
}

function Write-Step {
    param([string] $Text)
    Write-Host ''
    Write-Host $Text -ForegroundColor Cyan
}

Write-Host ''
Write-Host '========================================================' -ForegroundColor White
Write-Host ' R-45 验收：GET /kb/stats' -ForegroundColor White
Write-Host (' 目标服务：' + $Base) -ForegroundColor White
Write-Host '========================================================' -ForegroundColor White

# ---------------------------------------------------------------------------
Write-Step '步骤 1/7  服务可达性（/health）'
$health = $null
try {
    $health = Invoke-RestMethod -Uri ($Base + '/health') -Method Get -UseBasicParsing
    Write-Host ('         /health -> ' + ($health | ConvertTo-Json -Compress))
}
catch {
    Write-Check '服务可达' $false $_.Exception.Message
    Write-Host ''
    Write-Host '  服务没起来。先执行（另开一个终端，保持不关）：' -ForegroundColor Yellow
    Write-Host ('      ' + $Python + ' -m recall.api') -ForegroundColor Yellow
    Write-Host '  等它打印 api.service_ready 后再跑本脚本。' -ForegroundColor Yellow
    exit 1
}
Write-Check '服务可达（/health 返回 200）' $true

# ---------------------------------------------------------------------------
Write-Step '步骤 2/7  GET /kb/stats 字段完整性'
$stats = Invoke-RestMethod -Uri ($Base + '/kb/stats') -Method Get -UseBasicParsing
Write-Host '         响应体：'
($stats | ConvertTo-Json -Depth 4) -split "`n" | ForEach-Object { Write-Host ('           ' + $_.TrimEnd()) }

$expected = @(
    'collection', 'collections', 'qdrant', 'collection_ready', 'points_count',
    'documents', 'failed_documents', 'embedding_model', 'embedding_version',
    'chunker', 'created_at'
)
$actual = @($stats.PSObject.Properties.Name)
$missing = @($expected | Where-Object { $actual -notcontains $_ })
Write-Check ('11 个契约字段齐全（缺失：' + $(if ($missing.Count) { $missing -join ',' } else { '无' }) + '）') ($missing.Count -eq 0)
Write-Check ('qdrant=true 且 collection_ready=true（实得 qdrant=' + $stats.qdrant + ' ready=' + $stats.collection_ready + '）') ($stats.qdrant -and $stats.collection_ready)
Write-Check ('points_count > 0（实得 ' + $stats.points_count + '）') ($stats.points_count -gt 0)
Write-Check ('documents > 0（实得 ' + $stats.documents + '）') ($stats.documents -gt 0)
Write-Check ('failed_documents = 0（实得 ' + $stats.failed_documents + '）') ($stats.failed_documents -eq 0)
Write-Check ('chunker = md-heading-v1（实得 ' + $stats.chunker + '）') ($stats.chunker -eq 'md-heading-v1')

# ---------------------------------------------------------------------------
Write-Step '步骤 3/7  与 /health 交叉一致（两份口径来自不同代码路径）'
Write-Host ('         /health.documents    = ' + $health.documents + '   /kb/stats.documents    = ' + $stats.documents)
Write-Host ('         /health.points_count = ' + $health.points_count + '   /kb/stats.points_count = ' + $stats.points_count)
Write-Check 'documents 两处相等' ($health.documents -eq $stats.documents) ('health=' + $health.documents + ' stats=' + $stats.documents)
Write-Check 'points_count 两处相等' ($health.points_count -eq $stats.points_count) ('health=' + $health.points_count + ' stats=' + $stats.points_count)

# ---------------------------------------------------------------------------
Write-Step '步骤 4/7  只读性（判据 B）'
$dbBefore = $null
if (Test-Path -LiteralPath $RegistryDb) { $dbBefore = (Get-Item -LiteralPath $RegistryDb).LastWriteTimeUtc }
$reference = $stats | ConvertTo-Json -Compress -Depth 4
1..5 | ForEach-Object { Invoke-RestMethod -Uri ($Base + '/kb/stats') -Method Get -UseBasicParsing | Out-Null }
$after = Invoke-RestMethod -Uri ($Base + '/kb/stats') -Method Get -UseBasicParsing
$repeat = $after | ConvertTo-Json -Compress -Depth 4
Write-Check '连调 6 次 payload 逐位相同' ($reference -eq $repeat)
if ($null -ne $dbBefore) {
    $dbAfter = (Get-Item -LiteralPath $RegistryDb).LastWriteTimeUtc
    Write-Host ('         registry.db mtime 调用前 = ' + $dbBefore + '   调用后 = ' + $dbAfter)
    Write-Check 'registry.db 未被写入（mtime 不变）' ($dbBefore -eq $dbAfter)
}
else {
    Write-Host ('         （未找到 ' + $RegistryDb + '，跳过 mtime 取证）') -ForegroundColor Yellow
}

# ---------------------------------------------------------------------------
Write-Step '步骤 5/7  方法约束：只接受 GET'
$postStatus = 0
try {
    Invoke-WebRequest -Uri ($Base + '/kb/stats') -Method Post -UseBasicParsing | Out-Null
    $postStatus = 200
}
catch { $postStatus = [int]$_.Exception.Response.StatusCode }
Write-Host ('         POST /kb/stats -> HTTP ' + $postStatus)
Write-Check 'POST 被拒（期望 405）' ($postStatus -eq 405) ('实得 ' + $postStatus)

# ---------------------------------------------------------------------------
Write-Step '步骤 6/7  OpenAPI 已登记该路由'
$openapi = Invoke-RestMethod -Uri ($Base + '/openapi.json') -Method Get -UseBasicParsing
$paths = @($openapi.paths.PSObject.Properties.Name)
$paths | ForEach-Object { Write-Host ('           ' + $_) }
$statsMethods = if ($paths -contains '/kb/stats') { @($openapi.paths.'/kb/stats'.PSObject.Properties.Name) } else { @() }
Write-Check '/kb/stats 已登记且仅 get' (($paths -contains '/kb/stats') -and ($statsMethods -contains 'get') -and ($statsMethods -notcontains 'post'))
$four = @('/health', '/kb/search', '/kb/answer', '/kb/ingest')
$lost = @($four | Where-Object { $paths -notcontains $_ })
Write-Check ('既有四个端点仍在（缺失：' + $(if ($lost.Count) { $lost -join ',' } else { '无' }) + '）') ($lost.Count -eq 0)

# ---------------------------------------------------------------------------
Write-Step '步骤 7/7  同源同形回归测试（判据 A：REST == MCP structuredContent）'
if ($SkipPytest) {
    Write-Host '         （已用 -SkipPytest 跳过）' -ForegroundColor Yellow
}
else {
    Write-Host '         执行 pytest（约需 10~30 秒）...' -ForegroundColor DarkGray
    $testOut = & $Python -m pytest 'tests/test_search.py::test_rest_stats_endpoint_matches_the_mcp_tool' -q 2>&1
    $testOut | Select-Object -Last 4 | ForEach-Object { Write-Host ('           ' + $_) -ForegroundColor DarkGray }
    Write-Check 'REST 与 MCP 逐字段相等（pytest 通过）' ($LASTEXITCODE -eq 0) ('pytest 退出码 ' + $LASTEXITCODE)
}

# ---------------------------------------------------------------------------
Write-Host ''
Write-Host '========================================================' -ForegroundColor White
if ($script:Fail -eq 0) {
    Write-Host (' 验收结论：通过   （' + $script:Pass + ' 项检查全绿）') -ForegroundColor Green
}
else {
    Write-Host (' 验收结论：不通过 （通过 ' + $script:Pass + ' 项，失败 ' + $script:Fail + ' 项）') -ForegroundColor Red
}
Write-Host '========================================================' -ForegroundColor White
Write-Host ''

exit $script:Fail
