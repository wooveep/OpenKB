$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "PortableProcessTree.ps1")

function New-ProcessSnapshotEntry {
    param(
        [Parameter(Mandatory = $true)] [string] $Name,
        [Parameter(Mandatory = $true)] [int] $ProcessId,
        [Parameter(Mandatory = $true)] [int] $ParentProcessId,
        [Parameter(Mandatory = $true)] [DateTime] $CreationDate
    )

    return [pscustomobject] @{
        Name = $Name
        ProcessId = $ProcessId
        ParentProcessId = $ParentProcessId
        CreationDate = $CreationDate
    }
}

$boot = [DateTime]::Parse("2026-08-20T08:00:00Z").ToUniversalTime()
$shellStarted = $boot.AddHours(4)
$snapshot = @(
    New-ProcessSnapshotEntry -Name "OpenKB.exe" -ProcessId 100 -ParentProcessId 50 -CreationDate $shellStarted
    New-ProcessSnapshotEntry -Name "OpenKBEngine.exe" -ProcessId 200 -ParentProcessId 100 -CreationDate $shellStarted.AddMilliseconds(10)
    New-ProcessSnapshotEntry -Name "conhost.exe" -ProcessId 744 -ParentProcessId 200 -CreationDate $shellStarted.AddMilliseconds(20)
    New-ProcessSnapshotEntry -Name "renderer.exe" -ProcessId 300 -ParentProcessId 200 -CreationDate $shellStarted.AddMilliseconds(20)
    # Windows retains the creator's numeric PID after that process exits. PID
    # 744 belonged to a boot process before the new conhost reused it.
    New-ProcessSnapshotEntry -Name "csrss.exe" -ProcessId 756 -ParentProcessId 744 -CreationDate $boot.AddSeconds(1)
    New-ProcessSnapshotEntry -Name "wininit.exe" -ProcessId 832 -ParentProcessId 744 -CreationDate $boot.AddSeconds(2)
    New-ProcessSnapshotEntry -Name "services.exe" -ProcessId 976 -ParentProcessId 832 -CreationDate $boot.AddSeconds(3)
)

$descendants = @(
    Get-DescendantProcesses -RootProcessId 100 -ProcessSnapshot $snapshot
)
$actualIds = @($descendants | ForEach-Object { [int] $_.ProcessId } | Sort-Object)
$expectedIds = @(200, 300, 744)
if (($actualIds -join ",") -ne ($expectedIds -join ",")) {
    throw "PID reuse polluted the descendant tree. Expected $($expectedIds -join ', '); got $($actualIds -join ', ')."
}

Write-Host "Portable process-tree traversal tests passed."

