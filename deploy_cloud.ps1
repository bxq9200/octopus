# Octopus 云端网关部署脚本
#
# 用法：  .\deploy_cloud.ps1            # 同步代码 + 重启服务
#         .\deploy_cloud.ps1 -NoRestart # 只同步不重启
#
# 同步内容：backend/app（agent 代码在这里）+ cloud/
# 目标：<服务器>:/octopus/app/   （用 -Server root@你的服务器 或环境变量 OCTOPUS_DEPLOY_SERVER 指定）
#
# 注意：Windows 没有 rsync，用 tar 打包 + ssh 管道传输（跟空山 Mac 构建桥同一套路数）。

param([switch]$NoRestart, [string]$Server = $env:OCTOPUS_DEPLOY_SERVER)

$ErrorActionPreference = "Stop"
if (-not $Server) { throw "请用 -Server root@你的服务器 指定部署目标，或设置环境变量 OCTOPUS_DEPLOY_SERVER" }
$SERVER = $Server
$REMOTE = "/octopus/app"
$LOCAL  = $PSScriptRoot

Write-Host "=== 打包本地代码 ===" -ForegroundColor Cyan
Push-Location $LOCAL

# 排除：缓存、日志、本地产物、venv、数据库
$excludes = @(
    "--exclude=__pycache__", "--exclude=*.pyc", "--exclude=.pytest_cache",
    "--exclude=output", "--exclude=*.db", "--exclude=*.log", "--exclude=*.log.err",
    "--exclude=venv", "--exclude=node_modules"
)

$tarArgs = @("-czf", "-") + $excludes + @("backend/app", "backend/requirements.txt", "cloud")
& tar $tarArgs | & ssh -o BatchMode=yes $SERVER "mkdir -p $REMOTE && tar -xzf - -C $REMOTE"
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "同步失败" }

Pop-Location
Write-Host "代码已同步到 ${SERVER}:$REMOTE" -ForegroundColor Green

if (-not $NoRestart) {
    Write-Host "=== 重启服务 ===" -ForegroundColor Cyan
    & ssh -o BatchMode=yes $SERVER @"
find /octopus/app -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null
systemctl restart octopus-cloud 2>/dev/null || echo '(systemd 服务还没配，跳过重启)'
sleep 2
systemctl is-active octopus-cloud 2>/dev/null || true
curl -s --max-time 5 http://127.0.0.1:9000/health || echo '(健康检查失败)'
"@
    Write-Host ""
}
Write-Host "完成" -ForegroundColor Green
