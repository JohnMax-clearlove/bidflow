param([Parameter(Mandatory=$true)][string]$JobPath, [Parameter(Mandatory=$true)][string]$ResultPath)
$ErrorActionPreference = 'Stop'
$word = $null
$opened = @()
$result = $null

function Update-DocumentFields($document) {
    [void]$document.Fields.Update()
    foreach ($story in $document.StoryRanges) {
        $range = $story
        while ($null -ne $range) {
            [void]$range.Fields.Update()
            $range = $range.NextStoryRange
        }
    }
    foreach ($toc in $document.TablesOfContents) { [void]$toc.Update() }
    [void]$document.Repaginate()
}

function Read-Positions($document, $volume) {
    $rows = @()
    foreach ($target in $volume.bookmarks) {
        if (-not $document.Bookmarks.Exists([string]$target.bookmark)) { throw "组卷书签缺失：$($target.bookmark)" }
        $range = $document.Bookmarks.Item([string]$target.bookmark).Range.Duplicate
        [void]$range.Collapse(1)
        $rows += @{ target_id = [string]$target.target_id; bookmark = [string]$target.bookmark; printed_page = [int]$range.Information(1); pdf_page = [int]$range.Information(3) }
    }
    return ,$rows
}

try {
    $job = Get-Content -LiteralPath $JobPath -Raw -Encoding UTF8 | ConvertFrom-Json
    # 仅管理此脚本创建的 Word 实例和由它打开的组卷文件。
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    $word.AutomationSecurity = 3
    $word.Options.SaveNormalPrompt = $false
    foreach ($volume in $job.volumes) {
        $document = $word.Documents.Open([string]$volume.docx, $false, $false, $false)
        $opened += @{ document = $document; volume = $volume }
        foreach ($insertion in $volume.insertions) {
            if (-not $document.Bookmarks.Exists([string]$insertion.bookmark)) { throw '表单插入位置丢失' }
            $range = $document.Bookmarks.Item([string]$insertion.bookmark).Range
            $start = $range.Start
            $range.Text = ''
            $range = $document.Range($start, $start)
            [void]$range.InsertFile([string]$insertion.path)
        }
        foreach ($cross in ($volume.crossrefs | Sort-Object -Property variable -Unique)) {
            [void]$document.Variables.Add([string]$cross.variable, '待定位')
        }
        Update-DocumentFields $document
    }
    $previous = ''
    $stable = $false
    for ($iteration = 0; $iteration -lt 8; $iteration++) {
        $allPositions = @{}
        foreach ($entry in $opened) {
            foreach ($position in (Read-Positions $entry.document $entry.volume)) {
                $allPositions[([string]$entry.volume.volume + ':' + $position.target_id)] = $position
            }
        }
        foreach ($entry in $opened) {
            foreach ($cross in $entry.volume.crossrefs) {
                $key = [string]$cross.target_volume + ':' + [string]$cross.target_id
                if (-not $allPositions.ContainsKey($key)) { throw "跨册目标丢失：$key" }
                $value = [string]$cross.target_volume + ' 第 ' + $allPositions[$key].printed_page + ' 页'
                $entry.document.Variables.Item([string]$cross.variable).Value = $value
            }
            Update-DocumentFields $entry.document
        }
        $snapshot = @()
        foreach ($entry in $opened) {
            $snapshot += @{ volume = [string]$entry.volume.volume; pages = [int]$entry.document.ComputeStatistics(2); positions = (Read-Positions $entry.document $entry.volume) }
        }
        $serialized = $snapshot | ConvertTo-Json -Depth 12 -Compress
        if ($serialized -eq $previous) { $stable = $true; break }
        $previous = $serialized
    }
    if (-not $stable) { throw '八轮更新后分页仍未稳定，需人工检查模板和跨册索引' }
    $volumes = @()
    foreach ($entry in $opened) {
        $document = $entry.document
        $fields = @()
        foreach ($field in $document.Fields) {
            $row = @{code = [string]$field.Code.Text; result = [string]$field.Result.Text}
            if ($row.code -match '\bPAGEREF\s+(\S+)') {
                $name = $Matches[1].Trim('"')
                if (-not $document.Bookmarks.Exists($name)) { throw "页码引用书签丢失：$name" }
                $destination = $document.Bookmarks.Item($name).Range.Duplicate
                [void]$destination.Collapse(1)
                $row.bookmark = $name
                $row.printed_page = [int]$destination.Information(1)
                $row.pdf_page = [int]$destination.Information(3)
            }
            $fields += $row
        }
        [void]$document.Save()
        # 17 为 PDF，2 为 Word 书签；分页和跳转均来自 Word 实际输出。
        [void]$document.ExportAsFixedFormat([string]$entry.volume.pdf, 17, $false, 0, 0, 1, 1, 0, $true, $true, 2, $true, $true, $false)
        $volumes += @{ volume = [string]$entry.volume.volume; page_count = [int]$document.ComputeStatistics(2); positions = (Read-Positions $document $entry.volume); fields = $fields; toc_count = [int]$document.TablesOfContents.Count }
    }
    $result = @{status = 'ok'; iterations = $iteration + 1; volumes = $volumes}
} catch {
    $result = @{status = 'error'; error = $_.Exception.Message; detail = [string]$_.ScriptStackTrace}
} finally {
    foreach ($entry in $opened) {
        try { [void]$entry.document.Close(0) } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($entry.document) } catch {}
    }
    if ($null -ne $word) {
        try { [void]$word.Quit(0) } catch {}
        try { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($word) } catch {}
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}
$result | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $ResultPath -Encoding UTF8
if ($result.status -ne 'ok') { exit 1 }
