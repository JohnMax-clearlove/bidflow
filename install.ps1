<#
.SYNOPSIS
    BidFlow Windows 一键安装器（Windows PowerShell 5.1 与 PowerShell 7 均可用）。

.DESCRIPTION
    从 GitHub 固定 commit 归档安装 bidflow-local，使用 uv 管理 Python 与隔离依赖。
    不需要管理员权限，不修改系统级 ExecutionPolicy，不重启、不安装 Office，也不配置模型 API。
    默认安装到 %LOCALAPPDATA%\BidFlow，与用户投标项目、公司资料完全分开。

.PARAMETER InstallRoot
    安装根目录，默认 %LOCALAPPDATA%\BidFlow。必须是本机绝对路径。

.PARAMETER Ref
    要安装的 GitHub 提交：40 位 SHA 直接使用；分支或标签经 GitHub API 解析为完整 SHA。
    默认 main。

.PARAMETER WithOcr
    安装可选 OCR 依赖。未显式传入时沿用上次选择；全新安装默认不装。

.PARAMETER NoOcr
    明确不安装 OCR 依赖，覆盖上次的 OCR 选择（与 -WithOcr 互斥）。

.PARAMETER NoPath
    不修改用户持久 PATH。当前进程仍临时可用，新终端不会自动获得命令。

.PARAMETER Rollback
    回滚到上一份 release。不联网、不重新下载，切换前先对新（旧）入口运行 doctor。

.PARAMETER Uninstall
    卸载本安装器管理的入口、release、runtime、cache 与状态，并移除它添加的用户 PATH 条目。
    不删除未知文件、投标项目、公司资料或共享的 uv/Python。

.EXAMPLE
    irm https://raw.githubusercontent.com/JohnMax-clearlove/bidflow/main/install.ps1 | iex

.EXAMPLE
    $code = Get-Content -Raw -Encoding UTF8 .\install.ps1
    & ([scriptblock]::Create($code)) -InstallRoot 'D:\BidFlow' -WithOcr

.NOTES
    本文件按 UTF-8 无 BOM 保存，确保 `irm | iex` 在 Windows PowerShell 5.1 与 PowerShell 7 中都能正确读取中文。
    Windows PowerShell 5.1 的 `-File` 会按本地代码页误读中文，本地执行请先按 UTF-8 读取再执行（见上例）。

    离线测试钩子（仅在设置对应环境变量时生效，生产环境不要设置）：
      BIDFLOW_INSTALLER_LIBRARY_ONLY      设为 1 时只加载函数、不执行主流程，便于测试分层。
      BIDFLOW_INSTALLER_TEST_COMMIT_JSON  用本地 JSON 文件（含 sha 字段）代替 GitHub API。
      BIDFLOW_INSTALLER_TEST_RESOLVE_URL_FILE  把本应请求的 API URL 写入指定文件。
      BIDFLOW_INSTALLER_TEST_UV_INSTALLER 用本地脚本代替下载官方 uv 安装器。
      BIDFLOW_INSTALLER_TEST_USER_PATH_FILE    用文件代替真实用户 PATH 读写。
#>
param(
    [string]$InstallRoot,
    [string]$Ref = 'main',
    [switch]$WithOcr,
    [switch]$NoOcr,
    [switch]$NoPath,
    [switch]$Rollback,
    [switch]$Uninstall
)

# ===================== 固定常量 =====================
$script:BidflowProduct = 'bidflow-local'
$script:BidflowRepo = 'JohnMax-clearlove/bidflow'
$script:BidflowUvVersion = '0.12.15'
$script:BidflowUvInstallerUrl = "https://astral.sh/uv/$($script:BidflowUvVersion)/install.ps1"
$script:BidflowCoreModules = @('pydantic', 'docx', 'pdfplumber', 'pypdfium2', 'pypdf', 'openpyxl', 'PIL')
$script:BidflowBackupEnvNames = @('PATH', 'UV_INSTALL_DIR', 'UV_NO_MODIFY_PATH', 'UV_UNMANAGED_INSTALL', 'UV_TOOL_DIR', 'UV_TOOL_BIN_DIR', 'UV_PYTHON_INSTALL_DIR', 'UV_CACHE_DIR')

# ===================== 输出辅助（中文提示） =====================
function Write-BidflowStep([string]$Message) { Write-Host "[BidFlow] $Message" -ForegroundColor Cyan }
function Write-BidflowNote([string]$Message) { Write-Host "          $Message" -ForegroundColor Gray }
function Write-BidflowWarn([string]$Message) { Write-Host "[BidFlow 警告] $Message" -ForegroundColor Yellow }
function Write-BidflowError([string]$Message) { Write-Host "[BidFlow 安装失败] $Message" -ForegroundColor Red }

# ===================== 路径与 JSON 基础 =====================
function Get-BidflowNormalizedPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    $expanded = [Environment]::ExpandEnvironmentVariables($Path)
    if ($expanded.StartsWith('~')) {
        $profileDir = [Environment]::GetFolderPath('UserProfile')
        $expanded = $profileDir + $expanded.Substring(1)
    }
    $full = [System.IO.Path]::GetFullPath($expanded)
    $rootPart = [System.IO.Path]::GetPathRoot($full)
    while ($full.Length -gt $rootPart.Length -and $full.EndsWith([System.IO.Path]::DirectorySeparatorChar)) {
        $full = $full.Substring(0, $full.Length - 1)
    }
    return $full
}

function Test-BidflowPathInside {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Root)
    $p = Get-BidflowNormalizedPath -Path $Path
    $r = Get-BidflowNormalizedPath -Path $Root
    if ($p.Equals($r, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    return $p.StartsWith($r + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)
}

function Read-BidflowJsonFile {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    $raw = Get-Content -LiteralPath $Path -Raw -Encoding UTF8
    if ([string]::IsNullOrWhiteSpace($raw)) { return $null }
    try {
        return ($raw | ConvertFrom-Json)
    } catch {
        throw "文件不是有效 JSON：$Path（$($_.Exception.Message)）"
    }
}

function Write-BidflowJsonFile {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)]$Object)
    $directory = Split-Path -Parent $Path
    if ($directory -and -not (Test-Path -LiteralPath $directory)) { New-Item -ItemType Directory -Force -Path $directory | Out-Null }
    $json = $Object | ConvertTo-Json -Depth 8
    $temp = "$Path.tmp-$PID"
    [System.IO.File]::WriteAllText($temp, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temp -Destination $Path -Force
}

# ===================== 安全校验 =====================
function Assert-BidflowNotReparseItem {
    param([Parameter(Mandatory = $true)][string]$Path)
    $item = Get-Item -LiteralPath $Path -Force
    if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "检测到目录联接或符号链接，拒绝继续：$Path"
    }
}

# 检查 Path 本身及其在 Root 下的每一级已存在目录都不经重解析点越界。
function Assert-BidflowNoReparse {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Root)
    $full = Get-BidflowNormalizedPath -Path $Path
    $rootFull = Get-BidflowNormalizedPath -Path $Root
    if (-not (Test-BidflowPathInside -Path $full -Root $rootFull)) { throw "路径越出安装根目录，拒绝操作：$full" }
    if (Test-Path -LiteralPath $rootFull) { Assert-BidflowNotReparseItem -Path $rootFull }
    $relative = $full.Substring($rootFull.Length).TrimStart([char]92, [char]47)
    $current = $rootFull
    foreach ($part in ($relative -split '[\\/]')) {
        if (-not $part) { continue }
        $current = Join-Path $current $part
        if (-not (Test-Path -LiteralPath $current)) { break }
        Assert-BidflowNotReparseItem -Path $current
    }
}

function Assert-BidflowInstallRootSafe {
    param([Parameter(Mandatory = $true)][string]$Path)
    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        throw 'install.ps1 目前仅支持 Windows。其它平台请参考 docs/安装与更新.md 使用 uv 命令安装。'
    }
    if ([string]::IsNullOrWhiteSpace($Path)) { throw 'InstallRoot 不能为空。' }
    if ($Path -match '^[A-Za-z]:[^\\/]') { throw "InstallRoot 必须是绝对路径，不接受盘符相对路径：$Path" }
    if ($Path -match '^\\\\') { throw "InstallRoot 不支持 UNC/网络路径，请使用本机绝对路径：$Path" }
    if (-not [System.IO.Path]::IsPathRooted($Path)) { throw "InstallRoot 必须是绝对路径：$Path" }
    $full = Get-BidflowNormalizedPath -Path $Path
    $badChars = @([char]37, [char]59, [char]34, [char]96, [char]38, [char]124, [char]60, [char]62, [char]94, [char]13, [char]10, [char]0)
    foreach ($ch in $badChars) {
        if ($full.IndexOf($ch) -ge 0) { throw "InstallRoot 含可能被命令行重解释的字符，拒绝使用：$full" }
    }
    foreach ($part in ($full -split '[\\/]')) {
        if ($part.Length -gt 0 -and ($part.EndsWith('.') -or $part.EndsWith(' '))) {
            throw "InstallRoot 目录名不能以点或空格结尾：$full"
        }
    }
    if ($full -eq [System.IO.Path]::GetPathRoot($full)) { throw "拒绝安装到盘根目录：$full" }
    foreach ($name in @('SystemRoot', 'windir', 'ProgramFiles', 'ProgramFiles(x86)', 'ProgramData')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value -and (Test-BidflowPathInside -Path $full -Root (Get-BidflowNormalizedPath -Path $value))) {
            throw "拒绝安装到系统目录内：$full"
        }
    }
    foreach ($name in @('USERPROFILE', 'APPDATA', 'LOCALAPPDATA')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if ($value -and $full -eq (Get-BidflowNormalizedPath -Path $value)) {
            throw "拒绝直接使用用户关键目录作为 InstallRoot：$full"
        }
    }
    if ($full -eq (Get-BidflowNormalizedPath -Path (Get-Location).Path)) {
        throw "InstallRoot 不能是当前工作目录，请指定独立目录：$full"
    }
    return $full
}

# 根目录占用规则：空目录可用；非空目录必须有本安装器写的产品标记。
function Assert-BidflowRootUsable {
    param([Parameter(Mandatory = $true)][string]$Root)
    $markerPath = Join-Path $Root '.bidflow-install.json'
    if (Test-Path -LiteralPath $Root) {
        $item = Get-Item -LiteralPath $Root -Force
        if (-not $item.PSIsContainer) { throw "InstallRoot 已存在且不是目录：$Root" }
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "InstallRoot 本身是目录联接或符号链接，拒绝使用：$Root" }
        $children = @(Get-ChildItem -LiteralPath $Root -Force)
        if ($children.Count -gt 0) {
            $marker = $null
            try { $marker = Read-BidflowJsonFile -Path $markerPath } catch { $marker = $null }
            if ($null -eq $marker -or $marker.product -ne $script:BidflowProduct) {
                throw "目录非空且缺少有效的 BidFlow 产品标记，拒绝占用：$Root。请改用空目录，或确认后手动清理。"
            }
        }
    } else {
        New-Item -ItemType Directory -Force -Path $Root | Out-Null
    }
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        Write-BidflowJsonFile -Path $markerPath -Object @{ schema_version = 1; product = $script:BidflowProduct; installer = 'install.ps1'; created_at = (Get-Date).ToString('s') }
    }
}

# ===================== Ref、uv 与 doctor =====================
function Assert-BidflowRefFormat {
    param([Parameter(Mandatory = $true)][string]$Value)
    if ([string]::IsNullOrWhiteSpace($Value)) { throw 'Ref 不能为空。' }
    if ($Value -match '[\s\x00-\x1f]') { throw "Ref 含空白或控制字符，拒绝使用：$Value" }
    if ($Value -match '\\|\.\.') { throw "Ref 含不安全片段，拒绝使用：$Value" }
}

function Resolve-BidflowRefToSha {
    param([Parameter(Mandatory = $true)][string]$Value)
    Assert-BidflowRefFormat -Value $Value
    if ($Value -match '^[0-9a-fA-F]{40}$') { return $Value.ToLowerInvariant() }
    $url = "https://api.github.com/repos/$($script:BidflowRepo)/commits/$([uri]::EscapeDataString($Value))"
    if ($env:BIDFLOW_INSTALLER_TEST_RESOLVE_URL_FILE) {
        [System.IO.File]::WriteAllText($env:BIDFLOW_INSTALLER_TEST_RESOLVE_URL_FILE, $url, (New-Object System.Text.UTF8Encoding($false)))
    }
    $hookJson = $env:BIDFLOW_INSTALLER_TEST_COMMIT_JSON
    if ($hookJson -and (Test-Path -LiteralPath $hookJson -PathType Leaf)) {
        $response = Read-BidflowJsonFile -Path $hookJson
    } else {
        Write-BidflowStep "经 GitHub API 解析 Ref '$Value' 为完整提交 SHA ..."
        $headers = @{ 'User-Agent' = 'BidFlow-Installer'; 'Accept' = 'application/vnd.github+json' }
        try {
            $response = Invoke-RestMethod -Uri $url -Headers $headers -Method Get -TimeoutSec 60
        } catch {
            throw "无法解析 Ref '$Value'：$($_.Exception.Message)。可改用完整 40 位提交 SHA 后重试。"
        }
    }
    $sha = [string]$response.sha
    if ($sha -notmatch '^[0-9a-fA-F]{40}$') { throw "GitHub 未返回有效提交 SHA（Ref=$Value）。" }
    return $sha.ToLowerInvariant()
}

function Get-BidflowUvCommand {
    $commands = @(Get-Command uv -CommandType Application -ErrorAction SilentlyContinue)
    if ($commands.Count -gt 0) { return $commands[0].Source }
    return $null
}

# uv 不存在时，只从官方 HTTPS 下载固定版本安装器，并安装到本次安装根目录内的专用 runtime。
function Install-BidflowUvRuntime {
    param([Parameter(Mandatory = $true)][string]$Root)
    $runtimeDir = Join-Path $Root 'runtime'
    $uvBinDir = Join-Path $runtimeDir 'bin'
    Assert-BidflowNoReparse -Path $runtimeDir -Root $Root
    New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
    $tempDir = Join-Path $Root ('.tmp-uv-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
    New-Item -ItemType Directory -Force -Path $tempDir | Out-Null
    try {
        $installerPath = Join-Path $tempDir 'uv-install.ps1'
        $testInstaller = $env:BIDFLOW_INSTALLER_TEST_UV_INSTALLER
        if ($testInstaller -and (Test-Path -LiteralPath $testInstaller -PathType Leaf)) {
            Copy-Item -LiteralPath $testInstaller -Destination $installerPath -Force
        } else {
            Write-BidflowStep "未检测到 uv，从官方地址下载固定版本 uv $($script:BidflowUvVersion) 安装器 ..."
            Invoke-WebRequest -Uri $script:BidflowUvInstallerUrl -OutFile $installerPath -UseBasicParsing
        }
        $env:UV_INSTALL_DIR = $uvBinDir
        $env:UV_NO_MODIFY_PATH = '1'
        if (Test-Path Env:UV_UNMANAGED_INSTALL) { Remove-Item Env:UV_UNMANAGED_INSTALL -ErrorAction SilentlyContinue }
        $psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
        $global:LASTEXITCODE = 0
        if (Test-Path -LiteralPath $psExe) {
            & $psExe -NoProfile -ExecutionPolicy Bypass -File $installerPath | Out-Host
        } else {
            & $installerPath | Out-Host
        }
        if ($LASTEXITCODE -ne 0) { throw "uv 安装器执行失败（退出码 $LASTEXITCODE）。" }
        $candidates = @(Get-ChildItem -LiteralPath $uvBinDir -Force -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^uv\.(exe|cmd|bat)$' })
        $uvCommand = $null
        foreach ($name in @('uv.exe', 'uv.cmd', 'uv.bat')) {
            $match = $candidates | Where-Object { $_.Name -eq $name } | Select-Object -First 1
            if ($match) { $uvCommand = $match.FullName; break }
        }
        if (-not $uvCommand) { throw "uv 安装完成后未在 $uvBinDir 找到 uv 可执行文件。" }
        Write-BidflowJsonFile -Path (Join-Path $runtimeDir '.bidflow-runtime.json') -Object @{ schema_version = 1; product = $script:BidflowProduct; kind = 'uv-runtime'; uv_version = $script:BidflowUvVersion; created_at = (Get-Date).ToString('s') }
        Write-BidflowStep "已安装独立 uv 到 $uvBinDir（不改系统 PATH，不升级或卸载共享 uv）。"
        return $uvCommand
    } finally {
        Remove-BidflowTreeQuiet -Path $tempDir -Root $Root
    }
}

function Find-BidflowEntry {
    param([Parameter(Mandatory = $true)][string]$ReleaseDir)
    $binDir = Join-Path $ReleaseDir 'bin'
    foreach ($name in @('bidflow.exe', 'bidflow.cmd', 'bidflow.bat')) {
        $candidate = Join-Path $binDir $name
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    return $null
}

# 运行新入口的 doctor，并严格检查 JSON ok、Python 3.12 与核心模块；Word/Pandoc/OCR 只提示不谎报成功。
function Invoke-BidflowDoctor {
    param([Parameter(Mandatory = $true)][string]$Entry, [bool]$Ocr)
    Write-BidflowStep '运行 bidflow doctor 检查新入口 ...'
    $savedEncoding = $null
    try { $savedEncoding = [Console]::OutputEncoding } catch { $savedEncoding = $null }
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
    try {
        $global:LASTEXITCODE = 0
        $output = & $Entry doctor | Out-String
    } finally {
        if ($savedEncoding) { try { [Console]::OutputEncoding = $savedEncoding } catch { } }
    }
    $report = $null
    try { $report = $output | ConvertFrom-Json } catch { $report = $null }
    if ($null -eq $report -or -not $report.ok) {
        $reason = 'doctor 未输出有效 JSON'
        if ($report -and $report.error) { $reason = [string]$report.error }
        throw "新入口未通过 doctor 检查：$reason"
    }
    $result = $report.result
    if ($null -eq $result) { throw 'doctor 报告缺少 result 字段。' }
    if (([string]$result.python) -notmatch '^3\.12\.') { throw "Python 版本不符合要求：$([string]$result.python)" }
    $missing = @()
    foreach ($moduleName in $script:BidflowCoreModules) {
        if (-not $result.modules.$moduleName) { $missing += $moduleName }
    }
    if ($missing.Count -gt 0) { throw "缺少核心依赖：$($missing -join '、')" }
    if ($Ocr) {
        foreach ($moduleName in @('rapidocr', 'onnxruntime')) {
            if (-not $result.modules.$moduleName) { throw "已选择 OCR，但缺少依赖：$moduleName" }
        }
        if ([string]$result.ocr -notmatch '可用') { Write-BidflowWarn "OCR 依赖已安装，但本地模型可能尚未就绪：$([string]$result.ocr)" }
    }
    if (-not $result.word_registered) { Write-BidflowWarn '未检测到桌面版 Microsoft Word：可生成未分页 DOCX，最终页码与链接验收需在装有 Word 的 Windows 上完成。' }
    if (-not $result.pandoc) { Write-BidflowNote '未检测到 Pandoc：Word/PDF 结构化转换能力受限，核心流程不受影响。' }
    return $result
}

# ===================== 安装、切换、回滚、卸载 =====================
function Initialize-BidflowCacheDir {
    param([Parameter(Mandatory = $true)][string]$Root)
    $cacheDir = Join-Path $Root 'cache'
    Assert-BidflowNoReparse -Path $cacheDir -Root $Root
    New-Item -ItemType Directory -Force -Path $cacheDir | Out-Null
    $markerPath = Join-Path $cacheDir '.bidflow-cache.json'
    if (-not (Test-Path -LiteralPath $markerPath -PathType Leaf)) {
        Write-BidflowJsonFile -Path $markerPath -Object @{ schema_version = 1; product = $script:BidflowProduct; kind = 'uv-cache'; created_at = (Get-Date).ToString('s') }
    }
    return $cacheDir
}

function Install-BidflowRelease {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$UvExe, [Parameter(Mandatory = $true)][string]$Sha, [bool]$Ocr)
    $releasesDir = Join-Path $Root 'releases'
    Assert-BidflowNoReparse -Path $releasesDir -Root $Root
    New-Item -ItemType Directory -Force -Path $releasesDir | Out-Null
    $kind = 'core'
    if ($Ocr) { $kind = 'ocr' }
    $releaseName = '{0}-{1}-{2}' -f $Sha.Substring(0, 12), $kind, ([guid]::NewGuid().ToString('N').Substring(0, 6))
    $releaseDir = Join-Path $releasesDir $releaseName
    Assert-BidflowNoReparse -Path $releaseDir -Root $Root
    New-Item -ItemType Directory -Force -Path $releaseDir | Out-Null
    try {
        Write-BidflowJsonFile -Path (Join-Path $releaseDir '.bidflow-release.json') -Object @{ schema_version = 1; product = $script:BidflowProduct; sha = $Sha; ocr = $Ocr; created_at = (Get-Date).ToString('s') }
        $cacheDir = Initialize-BidflowCacheDir -Root $Root
        $env:UV_TOOL_DIR = Join-Path $releaseDir 'tool'
        $env:UV_TOOL_BIN_DIR = Join-Path $releaseDir 'bin'
        $env:UV_PYTHON_INSTALL_DIR = Join-Path $releaseDir 'python'
        $env:UV_CACHE_DIR = $cacheDir
        $env:UV_NO_MODIFY_PATH = '1'
        $extra = ''
        if ($Ocr) { $extra = '[ocr]' }
        $archiveUrl = "https://codeload.github.com/$($script:BidflowRepo)/tar.gz/$Sha"
        $spec = "$($script:BidflowProduct)$extra @ $archiveUrl"
        Write-BidflowStep "安装 bidflow-local$extra（固定提交 $($Sha.Substring(0, 12))）..."
        Write-BidflowNote "uv tool install --python 3.12 `"$spec`""
        $global:LASTEXITCODE = 0
        & $UvExe tool install --python 3.12 $spec | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "uv tool install 失败（退出码 $LASTEXITCODE）。" }
        $entry = Find-BidflowEntry -ReleaseDir $releaseDir
        if (-not $entry) { throw "uv 安装完成后未找到 bidflow 入口：$releaseDir\bin" }
        $doctor = Invoke-BidflowDoctor -Entry $entry -Ocr $Ocr
        return [pscustomobject]@{ Name = $releaseName; Dir = $releaseDir; Entry = $entry; Sha = $Sha; Ocr = $Ocr; Version = [string]$doctor.version }
    } catch {
        Write-BidflowWarn '本次安装未通过，清理未启用的 release 目录。'
        Remove-BidflowTreeQuiet -Path $releaseDir -Root $Root
        throw
    }
}

# 稳定入口只用 %~dp0 与 ASCII 相对路径，避免把根路径写进 cmd 造成 %/引号注入。
function Set-BidflowStableEntry {
    param([Parameter(Mandatory = $true)][string]$Root, [Parameter(Mandatory = $true)][string]$Entry)
    $binDir = Join-Path $Root 'bin'
    Assert-BidflowNoReparse -Path $binDir -Root $Root
    New-Item -ItemType Directory -Force -Path $binDir | Out-Null
    $relative = $Entry.Substring($Root.Length).TrimStart([char]92, [char]47)
    $content = "@echo off`r`nrem BidFlow stable entry managed by install.ps1 - do not edit.`r`n`"%~dp0..\$relative`" %*`r`n"
    $cmdPath = Join-Path $binDir 'bidflow.cmd'
    $temp = "$cmdPath.tmp-$PID"
    [System.IO.File]::WriteAllText($temp, $content, [System.Text.Encoding]::ASCII)
    Move-Item -LiteralPath $temp -Destination $cmdPath -Force
}

function Get-BidflowUserPathValue {
    $hookFile = $env:BIDFLOW_INSTALLER_TEST_USER_PATH_FILE
    if ($hookFile) {
        if (Test-Path -LiteralPath $hookFile -PathType Leaf) { return (Get-Content -LiteralPath $hookFile -Raw -Encoding UTF8) }
        return ''
    }
    return [Environment]::GetEnvironmentVariable('PATH', 'User')
}

function Set-BidflowUserPathValue {
    param([string]$Value)
    $hookFile = $env:BIDFLOW_INSTALLER_TEST_USER_PATH_FILE
    if ($hookFile) {
        [System.IO.File]::WriteAllText($hookFile, $Value, (New-Object System.Text.UTF8Encoding($false)))
        return
    }
    [Environment]::SetEnvironmentVariable('PATH', $Value, 'User')
}

function Test-BidflowPathListHas {
    param([string]$List, [string]$Entry)
    if ([string]::IsNullOrWhiteSpace($List)) { return $false }
    $wanted = $Entry.TrimEnd([char]92, [char]47)
    foreach ($part in $List.Split(';')) {
        $value = $part.Trim().TrimEnd([char]92, [char]47)
        if ($value -and $value.Equals($wanted, [StringComparison]::OrdinalIgnoreCase)) { return $true }
    }
    return $false
}

function Add-BidflowPathListEntry {
    param([string]$List, [string]$Entry)
    if (Test-BidflowPathListHas -List $List -Entry $Entry) { return $List }
    if ([string]::IsNullOrWhiteSpace($List)) { return $Entry }
    return $List.TrimEnd(';') + ';' + $Entry
}

function Remove-BidflowPathListEntry {
    param([string]$List, [string]$Entry)
    if ([string]::IsNullOrWhiteSpace($List)) { return $List }
    $wanted = $Entry.TrimEnd([char]92, [char]47)
    $kept = @()
    foreach ($part in $List.Split(';')) {
        $value = $part.Trim().TrimEnd([char]92, [char]47)
        if ($value -and $value.Equals($wanted, [StringComparison]::OrdinalIgnoreCase)) { continue }
        if ($part) { $kept += $part }
    }
    return ($kept -join ';')
}

# 递归删除前逐项校验路径在根内且不是重解析点。
function Remove-BidflowTreeSafely {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Root)
    $full = Get-BidflowNormalizedPath -Path $Path
    $rootFull = Get-BidflowNormalizedPath -Path $Root
    if ($full -eq $rootFull) { throw "拒绝直接删除安装根目录：$full" }
    if (-not (Test-BidflowPathInside -Path $full -Root $rootFull)) { throw "拒绝删除安装根目录之外的路径：$full" }
    if (-not (Test-Path -LiteralPath $full)) { return }
    Assert-BidflowNotReparseItem -Path $full
    $item = Get-Item -LiteralPath $full -Force
    if ($item.PSIsContainer) {
        $stack = New-Object System.Collections.Stack
        $stack.Push($full)
        while ($stack.Count -gt 0) {
            $dir = [string]$stack.Pop()
            foreach ($child in @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction Stop)) {
                if (-not (Test-BidflowPathInside -Path $child.FullName -Root $rootFull)) { throw "拒绝删除安装根目录之外的路径：$($child.FullName)" }
                if (($child.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) { throw "检测到重解析点，拒绝递归删除：$($child.FullName)" }
                if ($child.PSIsContainer) { $stack.Push($child.FullName) }
            }
        }
    }
    Remove-Item -LiteralPath $full -Recurse -Force
}

function Remove-BidflowTreeQuiet {
    param([string]$Path, [string]$Root)
    try { Remove-BidflowTreeSafely -Path $Path -Root $Root } catch { Write-BidflowWarn "临时内容清理未完成：$($_.Exception.Message)" }
}

function Invoke-BidflowRollback {
    param([Parameter(Mandatory = $true)][string]$Root)
    if (-not (Test-Path -LiteralPath (Join-Path $Root '.bidflow-install.json') -PathType Leaf)) { throw "未找到 BidFlow 安装标记，无法回滚：$Root" }
    $statePath = Join-Path $Root 'state.json'
    $state = Read-BidflowJsonFile -Path $statePath
    if ($null -eq $state -or $null -eq $state.current) { throw '没有可回滚的安装状态。' }
    if ($null -eq $state.previous) { throw '当前没有上一份 release 可回滚。' }
    $previous = $state.previous
    $releaseDir = Join-Path (Join-Path $Root 'releases') ([string]$previous.release)
    Assert-BidflowNoReparse -Path $releaseDir -Root $Root
    $marker = Read-BidflowJsonFile -Path (Join-Path $releaseDir '.bidflow-release.json')
    if ($null -eq $marker -or $marker.product -ne $script:BidflowProduct -or [string]$marker.sha -ne [string]$previous.sha) {
        throw "上一份 release 标记校验失败，拒绝回滚：$releaseDir"
    }
    $entry = Find-BidflowEntry -ReleaseDir $releaseDir
    if (-not $entry) { throw "上一份 release 缺少 bidflow 入口：$releaseDir" }
    Write-BidflowStep '回滚：先验证上一份入口（不联网、不下载）...'
    $doctor = Invoke-BidflowDoctor -Entry $entry -Ocr ([bool]$previous.ocr)
    if ($null -eq $doctor) { throw '上一份入口未通过 doctor，回滚已取消。' }
    $newCurrent = @{
        release = [string]$previous.release
        entry = $entry.Substring($Root.Length).TrimStart([char]92, [char]47)
        sha = [string]$previous.sha
        ocr = [bool]$previous.ocr
        version = [string]$doctor.version
        installed_at = (Get-Date).ToString('s')
    }
    $newState = @{
        schema_version = 1
        product = $script:BidflowProduct
        install_root = $Root
        current = $newCurrent
        previous = $state.current
        path_entry = (Join-Path $Root 'bin')
        path_added = [bool]$state.path_added
    }
    Set-BidflowStableEntry -Root $Root -Entry $entry
    Write-BidflowJsonFile -Path $statePath -Object $newState
    $kindText = '核心依赖'
    if ($previous.ocr) { $kindText = '含 OCR' }
    Write-BidflowStep "已回滚到 $(([string]$previous.sha).Substring(0, 12))（版本 $($doctor.version)，$kindText）。"
}

function Invoke-BidflowUninstall {
    param([Parameter(Mandatory = $true)][string]$Root)
    if (-not (Test-Path -LiteralPath $Root)) { Write-BidflowStep "未发现安装目录，无需卸载：$Root"; return }
    Assert-BidflowNoReparse -Path $Root -Root $Root
    $rootMarker = Join-Path $Root '.bidflow-install.json'
    $marker = $null
    try { $marker = Read-BidflowJsonFile -Path $rootMarker } catch { $marker = $null }
    if ($null -eq $marker -or $marker.product -ne $script:BidflowProduct) { throw "缺少 BidFlow 产品标记，拒绝卸载：$Root" }
    $state = $null
    try { $state = Read-BidflowJsonFile -Path (Join-Path $Root 'state.json') } catch { Write-BidflowWarn '状态文件无法解析，将继续按产品标记清理。' }

    # 只移除安装器自己添加的用户 PATH 条目
    if ($state -and $state.path_added) {
        $entry = Join-Path $Root 'bin'
        $userPath = Get-BidflowUserPathValue
        $newUserPath = Remove-BidflowPathListEntry -List $userPath -Entry $entry
        if ($newUserPath -ne $userPath) {
            Set-BidflowUserPathValue -Value $newUserPath
            Write-BidflowStep '已移除用户 PATH 中的 BidFlow 入口（仅对新终端生效）。'
        }
    }

    # release：只删除带产品标记的目录
    $releasesDir = Join-Path $Root 'releases'
    if (Test-Path -LiteralPath $releasesDir) {
        foreach ($dir in @(Get-ChildItem -LiteralPath $releasesDir -Force -Directory)) {
            $releaseMarker = $null
            try { $releaseMarker = Read-BidflowJsonFile -Path (Join-Path $dir.FullName '.bidflow-release.json') } catch { $releaseMarker = $null }
            if ($null -eq $releaseMarker -or $releaseMarker.product -ne $script:BidflowProduct) {
                Write-BidflowWarn "保留缺少 release 标记的目录：$($dir.FullName)"
                continue
            }
            try { Remove-BidflowTreeSafely -Path $dir.FullName -Root $Root }
            catch { Write-BidflowWarn "release 目录未删除：$($_.Exception.Message)" }
        }
        if (@(Get-ChildItem -LiteralPath $releasesDir -Force).Count -eq 0) { Remove-Item -LiteralPath $releasesDir -Force }
    }

    # runtime / cache：同样只删除带产品标记的
    foreach ($target in @(@{ Name = 'runtime'; Marker = '.bidflow-runtime.json' }, @{ Name = 'cache'; Marker = '.bidflow-cache.json' })) {
        $targetDir = Join-Path $Root $target.Name
        if (-not (Test-Path -LiteralPath $targetDir)) { continue }
        $targetMarker = $null
        try { $targetMarker = Read-BidflowJsonFile -Path (Join-Path $targetDir $target.Marker) } catch { $targetMarker = $null }
        if ($null -eq $targetMarker -or $targetMarker.product -ne $script:BidflowProduct) {
            Write-BidflowWarn "保留缺少产品标记的目录：$targetDir"
            continue
        }
        try { Remove-BidflowTreeSafely -Path $targetDir -Root $Root }
        catch { Write-BidflowWarn "目录未删除：$($_.Exception.Message)" }
    }

    # 稳定入口：只删除带我们注释标记的 bidflow.cmd
    $binDir = Join-Path $Root 'bin'
    $stableCmd = Join-Path $binDir 'bidflow.cmd'
    if (Test-Path -LiteralPath $stableCmd -PathType Leaf) {
        $content = Get-Content -LiteralPath $stableCmd -Raw -Encoding ASCII -ErrorAction SilentlyContinue
        if ($content -and $content -match 'BidFlow stable entry') { Remove-Item -LiteralPath $stableCmd -Force }
        else { Write-BidflowWarn "保留不是本安装器生成的入口文件：$stableCmd" }
    }
    if (Test-Path -LiteralPath $binDir) {
        if (@(Get-ChildItem -LiteralPath $binDir -Force).Count -eq 0) { Remove-Item -LiteralPath $binDir -Force }
    }

    # 状态、根标记与本次安装的临时目录
    foreach ($file in @((Join-Path $Root 'state.json'), $rootMarker)) {
        if (Test-Path -LiteralPath $file -PathType Leaf) { Remove-Item -LiteralPath $file -Force }
    }
    foreach ($dir in @(Get-ChildItem -LiteralPath $Root -Force -Directory -Filter '.tmp-*')) {
        try { Remove-BidflowTreeSafely -Path $dir.FullName -Root $Root } catch { Write-BidflowWarn "临时目录未删除：$($_.Exception.Message)" }
    }

    $left = @(Get-ChildItem -LiteralPath $Root -Force)
    if ($left.Count -eq 0) {
        Remove-Item -LiteralPath $Root -Force
        Write-BidflowStep "已卸载并删除安装目录：$Root"
    } else {
        Write-BidflowStep '已卸载本安装器管理的内容。'
        Write-BidflowWarn '以下内容不属于 BidFlow 管理范围，已保留：'
        foreach ($item in $left) { Write-BidflowNote "  $($item.FullName)" }
        Write-BidflowNote '如需完全删除，请确认这些文件无用后手动清理该目录；再次安装前请改用空目录。'
    }
}

# ===================== 主流程 =====================
function Invoke-BidflowMain {
    param(
        [string]$InstallRoot,
        [string]$Ref,
        [bool]$WithOcr,
        [bool]$NoOcr,
        [bool]$NoPath,
        [bool]$Rollback,
        [bool]$Uninstall,
        [bool]$WithOcrSpecified,
        [bool]$NoOcrSpecified,
        [bool]$RefSpecified
    )
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'
    $backup = @{}
    foreach ($name in $script:BidflowBackupEnvNames) { $backup[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }
    try {
        # 1. 命令互斥与错误输入：必须在任何下载/修改之前拒绝
        if ($Rollback -and $Uninstall) { throw '-Rollback 与 -Uninstall 不能同时使用。' }
        if ($WithOcr -and $NoOcr) { throw '-WithOcr 与 -NoOcr 不能同时使用。' }
        if (($Rollback -or $Uninstall) -and ($WithOcrSpecified -or $NoOcrSpecified)) { throw '-WithOcr 或 -NoOcr 只能用于安装或升级。' }
        if (($Rollback -or $Uninstall) -and $RefSpecified) { throw '-Ref 只能用于安装或升级。' }
        if ([string]::IsNullOrWhiteSpace($InstallRoot)) {
            $localAppData = [Environment]::GetEnvironmentVariable('LOCALAPPDATA')
            if (-not $localAppData) { $localAppData = [Environment]::GetFolderPath('LocalApplicationData') }
            if (-not $localAppData) { throw '无法确定 LOCALAPPDATA，请显式传入 -InstallRoot。' }
            $InstallRoot = Join-Path $localAppData 'BidFlow'
        }
        $root = Assert-BidflowInstallRootSafe -Path $InstallRoot
        if (-not ($Uninstall -or $Rollback)) { Assert-BidflowRefFormat -Value $Ref }

        # 2. 卸载/回滚：不联网、不安装 uv
        if ($Uninstall) { Invoke-BidflowUninstall -Root $root; return 0 }
        if ($Rollback) { Invoke-BidflowRollback -Root $root; return 0 }

        # 3. 安装/升级
        Assert-BidflowRootUsable -Root $root
        $statePath = Join-Path $root 'state.json'
        $oldState = $null
        try { $oldState = Read-BidflowJsonFile -Path $statePath } catch { Write-BidflowWarn "忽略无法解析的旧状态文件：$($_.Exception.Message)" }

        $ocr = $false
        if ($WithOcrSpecified) { $ocr = $WithOcr }
        elseif ($NoOcrSpecified) { $ocr = $false }
        elseif ($oldState -and $oldState.current -and $oldState.current.ocr) {
            $ocr = $true
            Write-BidflowNote '未显式传入 OCR 选项，沿用上次选择（含 OCR）。'
        }

        $sha = Resolve-BidflowRefToSha -Value $Ref
        Write-BidflowStep "目标提交：$sha"

        $uv = Get-BidflowUvCommand
        if ($uv) {
            Write-BidflowStep "复用已有 uv：$uv"
        } else {
            $uv = Install-BidflowUvRuntime -Root $root
        }

        $release = Install-BidflowRelease -Root $root -UvExe $uv -Sha $sha -Ocr $ocr

        # 4. 通过 doctor 后才切换稳定入口与状态
        $binDir = Join-Path $root 'bin'
        if (-not (Test-BidflowPathListHas -List $env:PATH -Entry $binDir)) { $env:PATH = "$binDir;$env:PATH" }
        $pathAdded = $false
        if ($oldState -and $oldState.path_added) { $pathAdded = $true }
        if (-not $NoPath) {
            $userPath = Get-BidflowUserPathValue
            if (-not (Test-BidflowPathListHas -List $userPath -Entry $binDir)) {
                Set-BidflowUserPathValue -Value (Add-BidflowPathListEntry -List $userPath -Entry $binDir)
                $pathAdded = $true
                Write-BidflowStep "已把 $binDir 加入用户 PATH（仅对新终端生效）。"
            } else {
                Write-BidflowNote '用户 PATH 已包含 BidFlow 入口，未重复添加。'
            }
        }

        $current = @{
            release = $release.Name
            entry = $release.Entry.Substring($root.Length).TrimStart([char]92, [char]47)
            sha = $release.Sha
            ocr = [bool]$release.Ocr
            version = [string]$release.Version
            installed_at = (Get-Date).ToString('s')
        }
        $previous = $null
        if ($oldState -and $oldState.current) { $previous = $oldState.current }
        $newState = @{
            schema_version = 1
            product = $script:BidflowProduct
            install_root = $root
            current = $current
            previous = $previous
            path_entry = $binDir
            path_added = $pathAdded
        }
        Set-BidflowStableEntry -Root $root -Entry $release.Entry
        Write-BidflowJsonFile -Path $statePath -Object $newState

        $kindText = '核心依赖'
        if ($release.Ocr) { $kindText = '含 OCR' }
        Write-BidflowStep "安装完成，已通过新入口的 doctor 检查。"
        Write-BidflowNote "安装目录：$root"
        Write-BidflowNote "当前版本：$($release.Version)（提交 $($release.Sha.Substring(0, 12))，$kindText）"
        Write-BidflowNote '下一步（请新开一个终端，或重新打开 Agent，以继承新的 PATH）：'
        Write-BidflowNote '  bidflow doctor'
        Write-BidflowNote '  bidflow init "项目名" --path "D:\投标项目\项目名"'
        Write-BidflowNote '未开新终端时，当前窗口也可用完整路径调用入口。'
        Write-BidflowNote '更新：重复运行安装命令；回滚：install.ps1 -Rollback；卸载：install.ps1 -Uninstall。'
        return 0
    } catch {
        Write-BidflowError -Message $_.Exception.Message
        return 1
    } finally {
        foreach ($name in $script:BidflowBackupEnvNames) { [Environment]::SetEnvironmentVariable($name, $backup[$name], 'Process') }
    }
}

# ===================== 入口 =====================
if ($env:BIDFLOW_INSTALLER_LIBRARY_ONLY -ne '1') {
    $status = Invoke-BidflowMain `
        -InstallRoot $InstallRoot `
        -Ref $Ref `
        -WithOcr ([bool]$WithOcr.IsPresent) `
        -NoOcr ([bool]$NoOcr.IsPresent) `
        -NoPath ([bool]$NoPath.IsPresent) `
        -Rollback ([bool]$Rollback.IsPresent) `
        -Uninstall ([bool]$Uninstall.IsPresent) `
        -WithOcrSpecified $PSBoundParameters.ContainsKey('WithOcr') `
        -NoOcrSpecified $PSBoundParameters.ContainsKey('NoOcr') `
        -RefSpecified $PSBoundParameters.ContainsKey('Ref')
    if ($status -ne 0) {
        throw 'BidFlow 安装未完成，请根据上方错误信息处理后重试。'
    }
}
