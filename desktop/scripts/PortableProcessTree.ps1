function Get-DescendantProcesses {
    param(
        [Parameter(Mandatory = $true)] [int] $RootProcessId,
        [AllowEmptyCollection()] [object[]] $ProcessSnapshot
    )

    if (-not $PSBoundParameters.ContainsKey("ProcessSnapshot")) {
        $ProcessSnapshot = @(Get-CimInstance Win32_Process)
    }

    $rootProcess = $ProcessSnapshot |
        Where-Object { [int] $_.ProcessId -eq $RootProcessId } |
        Select-Object -First 1
    if ($null -eq $rootProcess) {
        throw "Root process $RootProcessId was not present in the process snapshot."
    }

    $pendingProcesses = New-Object "System.Collections.Generic.Queue[object]"
    $descendants = New-Object "System.Collections.Generic.List[object]"
    $seenProcessIds = New-Object "System.Collections.Generic.HashSet[int]"
    $pendingProcesses.Enqueue($rootProcess)
    [void] $seenProcessIds.Add($RootProcessId)
    while ($pendingProcesses.Count -gt 0) {
        $parentProcess = $pendingProcesses.Dequeue()
        foreach ($process in @(
            $ProcessSnapshot |
                Where-Object {
                    [int] $_.ParentProcessId -eq [int] $parentProcess.ProcessId
                }
        )) {
            $processId = [int] $process.ProcessId
            if ($seenProcessIds.Contains($processId)) {
                continue
            }
            # ParentProcessId is only a numeric creator PID. Windows preserves
            # it after the creator exits, so a later process can reuse that PID.
            # A process created before the current parent cannot be its child.
            if (
                $null -eq $parentProcess.CreationDate -or
                $null -eq $process.CreationDate -or
                [DateTime] $process.CreationDate -lt [DateTime] $parentProcess.CreationDate
            ) {
                continue
            }
            [void] $seenProcessIds.Add($processId)
            $descendants.Add($process)
            $pendingProcesses.Enqueue($process)
        }
    }
    foreach ($descendant in $descendants) {
        Write-Output $descendant
    }
}
