# 校园网自连 · 一行命令安装（Windows）
# 用法（在 PowerShell 里粘贴这一行）：
#   iwr -useb https://raw.githubusercontent.com/enmingxiong821-cloud/campusnet/main/install.ps1 | iex
#
# 也可以下载后双击运行；支持 -DryRun（只打印将要做什么，不实际执行）。
# 设计原则：幂等（重复运行安全）、断点续跑（已有安装目录走升级分支）、
# 密码不进命令行、全程中文输出、出错时告诉你下一步该干什么。

[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [switch]$DryRun,
    [string]$Branch = "main"
)

$ErrorActionPreference = "Stop"
$Repo = "https://github.com/enmingxiong821-cloud/campusnet.git"
# 没有 git 时的兜底走分支归档。raw.githubusercontent.com 只发单个文件，
# 拿 "$RawBase/$Branch" 去请求等于要仓库根目录下一个叫 main 的文件——404。
$ZipBase = "https://github.com/enmingxiong821-cloud/campusnet/archive/refs/heads"

function Say($msg) { Write-Host $msg }
function Die($msg) {
    Write-Host "[错误] $msg" -ForegroundColor Red
    Write-Host "        手动兜底：先装 Python 3.10+，再从 $Repo 下载仓库，解压后双击 install.bat。"
    exit 1
}

# ---- 0. 目标目录 ----
if (-not $InstallDir) {
    $InstallDir = Join-Path $env:LOCALAPPDATA "campusnet"
}
if ($DryRun) {
    Say "[预演] 目标安装目录：$InstallDir"
}

# ---- 1. 找 Python ----
$Py = $null
foreach ($candidate in @("py", "python", "python3")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) { $Py = $candidate; break }
}
if (-not $Py) {
    Die "没找到 Python。请先安装 Python 3.10 或更高版本：https://www.python.org/downloads/（勾选 Add python.exe to PATH）"
}
$verOut = & $Py --version 2>$null
if ($LASTEXITCODE -ne 0) { Die "python --version 失败" }
if ($verOut -match "Python (\d+)\.(\d+)") {
    if ([int]$Matches[1] -lt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -lt 10)) {
        Die "Python 版本 $verOut 过低，需要 3.10 或更高"
    }
}
Say "[OK]  Python：$verOut"

# ---- 2. 取仓库（已有则升级）----
$RunPy = Join-Path $InstallDir "utils\run.py"
if (Test-Path $RunPy) {
    Say "[信息] 已存在安装目录 $InstallDir，走升级分支"
    if (Test-Path (Join-Path $InstallDir ".git")) {
        if ($DryRun) {
            Say "[预演] 会执行：git -C $InstallDir pull --ff-only"
        } else {
            git -C $InstallDir pull --ff-only
            if ($LASTEXITCODE -ne 0) { Die "git pull 失败，请手动处理 $InstallDir" }
            Say "[OK]  已升级到最新"
        }
    } else {
        Say "[信息] 目录存在但非 git 仓库，跳过升级，直接复用现有文件"
    }
} else {
    if ($DryRun) {
        Say "[预演] 会执行：git clone --depth 1 --branch $Branch $Repo $InstallDir"
    } else {
        $git = Get-Command git -ErrorAction SilentlyContinue
        if ($git) {
            Say "[信息] 正在 git clone --depth 1（约几 MB）……"
            git clone --depth 1 --branch $Branch $Repo $InstallDir
            if ($LASTEXITCODE -ne 0) { Die "git clone 失败，检查网络或直接下载仓库 zip" }
        } else {
            # 没有 git：下载 zip 兜底。main 分支 zip 没有随附校验和，
            # 完整性靠 HTTPS 传输本身；Release 分发包才有 sha256。
            Say "[信息] 没找到 git，改用 zip 下载……"
            $Zip = Join-Path $env:TEMP "campusnet-$Branch.zip"
            Invoke-WebRequest -Uri "$ZipBase/${Branch}.zip" -OutFile $Zip
            Expand-Archive -Path $Zip -DestinationPath $InstallDir -Force
            $inner = Get-ChildItem $InstallDir -Directory | Where-Object { $_.Name -like "campusnet-*" } | Select-Object -First 1
            if ($inner) {
                Get-ChildItem $inner.FullName | Move-Item -Destination $InstallDir -Force
                Remove-Item $inner.FullName -Force
            }
            Remove-Item $Zip -Force
            Say "[OK]  已解压到 $InstallDir"
        }
    }
}

# ---- 3. 委托给 setup（它做：快捷方式 + 浏览器依赖检查 + 自检 + 下一步指引）----
if ($DryRun) {
    Say "[预演] 会执行：& $Py `"$RunPy`" setup"
} else {
    Say "[信息] 正在运行一键配置……"
    & $Py $RunPy setup
    $code = $LASTEXITCODE
    if ($code -ne 0) { Die "一键配置返回退出码 $code，请阅读上面的输出" }
}
Say "[完成] 安装流程结束。桌面已生成「校园网自连」快捷方式；"
Say "        下一步建议：注册开机自启（菜单里有），并添加你的校园网账号。"
