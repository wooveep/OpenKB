[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $ExecutablePath,
    [ValidateRange(1, 20)] [int] $Iterations = 5,
    [ValidateRange(250, 10000)] [int] $MinimumVisibleMilliseconds = 1000,
    [ValidateRange(10, 500)] [int] $PollMilliseconds = 25,
    [string] $ResultPath = ""
)

$ErrorActionPreference = "Stop"

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class OpenKBTrayNativeMethods
{
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll")]
    public static extern bool IsWindowVisible(IntPtr hWnd);

    [DllImport("user32.dll")]
    public static extern bool SetCursorPos(int x, int y);

    [DllImport("user32.dll")]
    public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extraInfo);

    [DllImport("user32.dll")]
    public static extern void keybd_event(byte virtualKey, byte scanCode, uint flags, UIntPtr extraInfo);
}
"@

$WM_CLOSE = 0x0010
$MOUSEEVENTF_RIGHTDOWN = 0x0008
$MOUSEEVENTF_RIGHTUP = 0x0010
$KEYEVENTF_KEYUP = 0x0002
$VK_ESCAPE = 0x1B
$ShowHiddenIconsZh = -join ([char[]] @(0x663E, 0x793A, 0x9690, 0x85CF, 0x7684, 0x56FE, 0x6807))
$HiddenIconsZh = -join ([char[]] @(0x9690, 0x85CF, 0x7684, 0x56FE, 0x6807))
$QuitOpenKBZh = (-join ([char[]] @(0x9000, 0x51FA))) + " OpenKB"
$transcriptStarted = $false

if ($ResultPath) {
    $ResultPath = [System.IO.Path]::GetFullPath($ResultPath)
    Start-Transcript -LiteralPath $ResultPath -Force | Out-Null
    $transcriptStarted = $true
}

trap {
    Write-Error ("TRAY_MENU_ACCEPTANCE failed: " + $_.Exception.Message)
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
    exit 1
}

function Assert-That {
    param(
        [Parameter(Mandatory = $true)] [bool] $Condition,
        [Parameter(Mandatory = $true)] [string] $Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Get-ElementsByControlType {
    param([Parameter(Mandatory = $true)] $ControlType)

    $condition = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        $ControlType
    )
    return @(
        [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
            [System.Windows.Automation.TreeScope]::Descendants,
            $condition
        )
    )
}

function Find-TrayIcon {
    foreach ($element in @(Get-ElementsByControlType -ControlType ([System.Windows.Automation.ControlType]::Button))) {
        try {
            $name = $element.Current.Name
            if (
                $name -match "(?i)OpenKB" -and
                $name -notmatch "(?i)running windows?"
            ) {
                return $element
            }
        }
        catch [System.Windows.Automation.ElementNotAvailableException] {
            continue
        }
    }
    return $null
}

function Invoke-UiElement {
    param([Parameter(Mandatory = $true)] $Element)

    $pattern = $null
    if ($Element.TryGetCurrentPattern(
        [System.Windows.Automation.InvokePattern]::Pattern,
        [ref] $pattern
    )) {
        ([System.Windows.Automation.InvokePattern] $pattern).Invoke()
        return $true
    }
    return $false
}

function Show-TrayOverflow {
    foreach ($element in @(Get-ElementsByControlType -ControlType ([System.Windows.Automation.ControlType]::Button))) {
        try {
            $name = $element.Current.Name
            if (
                $name -match "(?i)(show hidden icons|hidden icons|notification chevron)" -or
                $name -in @($ShowHiddenIconsZh, $HiddenIconsZh)
            ) {
                if (Invoke-UiElement -Element $element) {
                    Start-Sleep -Milliseconds 200
                    return
                }
            }
        }
        catch [System.Windows.Automation.ElementNotAvailableException] {
            continue
        }
    }
}

function Wait-ForTrayIcon {
    param([int] $TimeoutSeconds = 20)

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        $element = Find-TrayIcon
        if ($null -ne $element) {
            return $element
        }
        Show-TrayOverflow
        $element = Find-TrayIcon
        if ($null -ne $element) {
            return $element
        }
        Start-Sleep -Milliseconds 100
    }
    throw "TRAY_ICON_NOT_FOUND: OpenKB notification-area icon was not visible to UI Automation."
}

function Send-RightClick {
    param([Parameter(Mandatory = $true)] $Element)

    $bounds = $Element.Current.BoundingRectangle
    Assert-That -Condition (-not $bounds.IsEmpty) -Message "TRAY_ICON_BOUNDS_EMPTY: OpenKB tray icon has no clickable bounds."
    $x = [int] [Math]::Round($bounds.X + ($bounds.Width / 2))
    $y = [int] [Math]::Round($bounds.Y + ($bounds.Height / 2))
    Write-Host ("TRAY_CLICK_TARGET name='{0}' x={1} y={2} width={3} height={4}" -f $Element.Current.Name, $x, $y, [int] $bounds.Width, [int] $bounds.Height)
    Assert-That -Condition ([OpenKBTrayNativeMethods]::SetCursorPos($x, $y)) -Message "TRAY_CURSOR_FAILED: Could not move the cursor to the OpenKB tray icon."
    [OpenKBTrayNativeMethods]::mouse_event($MOUSEEVENTF_RIGHTDOWN, 0, 0, 0, [UIntPtr]::Zero)
    Start-Sleep -Milliseconds 40
    [OpenKBTrayNativeMethods]::mouse_event($MOUSEEVENTF_RIGHTUP, 0, 0, 0, [UIntPtr]::Zero)
}

function Find-QuitMenuItem {
    foreach ($element in @(Get-ElementsByControlType -ControlType ([System.Windows.Automation.ControlType]::MenuItem))) {
        try {
            if ($element.Current.Name -in @("Quit OpenKB", $QuitOpenKBZh)) {
                return $element
            }
        }
        catch [System.Windows.Automation.ElementNotAvailableException] {
            continue
        }
    }
    return $null
}

function Wait-ForQuitMenuItem {
    param(
        [int] $TimeoutMilliseconds = 2000,
        [IntPtr] $WindowHandle = [IntPtr]::Zero
    )

    $started = [DateTime]::UtcNow
    $windowRestoreReported = $false
    while (([DateTime]::UtcNow - $started).TotalMilliseconds -lt $TimeoutMilliseconds) {
        if (
            -not $windowRestoreReported -and
            $WindowHandle -ne [IntPtr]::Zero -and
            [OpenKBTrayNativeMethods]::IsWindowVisible($WindowHandle)
        ) {
            $elapsedMilliseconds = [int] ([DateTime]::UtcNow - $started).TotalMilliseconds
            Write-Host "TRAY_RIGHT_CLICK_RESTORED_WINDOW elapsed_ms=$elapsedMilliseconds"
            $windowRestoreReported = $true
        }
        $element = Find-QuitMenuItem
        if ($null -ne $element) {
            return $element
        }
        Start-Sleep -Milliseconds $PollMilliseconds
    }
    return $null
}

function Assert-QuitMenuRemainsVisible {
    param(
        [Parameter(Mandatory = $true)] [int] $Iteration,
        [Parameter(Mandatory = $true)] [IntPtr] $WindowHandle
    )

    $menuItem = Wait-ForQuitMenuItem -WindowHandle $WindowHandle
    if ($null -eq $menuItem) {
        $menuItems = @()
        foreach ($element in @(Get-ElementsByControlType -ControlType ([System.Windows.Automation.ControlType]::MenuItem))) {
            try {
                $menuItems += "'{0}'(offscreen={1},enabled={2})" -f $element.Current.Name, $element.Current.IsOffscreen, $element.Current.IsEnabled
            }
            catch [System.Windows.Automation.ElementNotAvailableException] {
                continue
            }
        }
        if ($menuItems.Count -eq 0) {
            Write-Host "TRAY_UIA_MENU_ITEMS none"
        }
        else {
            Write-Host ("TRAY_UIA_MENU_ITEMS " + ($menuItems -join "; "))
        }
        Write-Host "TRAY_WINDOW_VISIBLE_AFTER_RIGHT_CLICK=$([OpenKBTrayNativeMethods]::IsWindowVisible($WindowHandle))"
    }
    Assert-That -Condition ($null -ne $menuItem) -Message "TRAY_MENU_NOT_SHOWN: Quit OpenKB did not appear on iteration $Iteration."
    $visibleStarted = [DateTime]::UtcNow
    while (([DateTime]::UtcNow - $visibleStarted).TotalMilliseconds -lt $MinimumVisibleMilliseconds) {
        $menuItem = Find-QuitMenuItem
        $visible = $false
        if ($null -ne $menuItem) {
            try {
                $visible = -not $menuItem.Current.IsOffscreen -and $menuItem.Current.IsEnabled
            }
            catch [System.Windows.Automation.ElementNotAvailableException] {
                $visible = $false
            }
        }
        if (-not $visible) {
            $visibleMilliseconds = [int] ([DateTime]::UtcNow - $visibleStarted).TotalMilliseconds
            throw "TRAY_MENU_DISMISSED: Quit OpenKB disappeared after ${visibleMilliseconds}ms on iteration $Iteration."
        }
        Start-Sleep -Milliseconds $PollMilliseconds
    }
    return $menuItem
}

function Send-Escape {
    [OpenKBTrayNativeMethods]::keybd_event($VK_ESCAPE, 0, 0, [UIntPtr]::Zero)
    [OpenKBTrayNativeMethods]::keybd_event($VK_ESCAPE, 0, $KEYEVENTF_KEYUP, [UIntPtr]::Zero)
}

function Save-DesktopScreenshot {
    param([Parameter(Mandatory = $true)] [string] $Path)

    $bounds = [System.Windows.Forms.SystemInformation]::VirtualScreen
    $bitmap = New-Object System.Drawing.Bitmap($bounds.Width, $bounds.Height)
    $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
    try {
        $graphics.CopyFromScreen($bounds.Left, $bounds.Top, 0, 0, $bounds.Size)
        $bitmap.Save($Path, [System.Drawing.Imaging.ImageFormat]::Png)
    }
    finally {
        $graphics.Dispose()
        $bitmap.Dispose()
    }
    Write-Host "TRAY_SCREENSHOT path='$Path'"
}

function Wait-ForMainWindow {
    param(
        [Parameter(Mandatory = $true)] [System.Diagnostics.Process] $Process,
        [int] $TimeoutSeconds = 30
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($Process.HasExited) {
            throw "OPENKB_EXITED_EARLY: OpenKB.exe exited before its main window appeared."
        }
        $Process.Refresh()
        if ($Process.MainWindowHandle -ne [IntPtr]::Zero) {
            return $Process.MainWindowHandle
        }
        Start-Sleep -Milliseconds 100
    }
    throw "OPENKB_WINDOW_TIMEOUT: OpenKB.exe did not create its main window."
}

function Wait-ForWindowHidden {
    param(
        [Parameter(Mandatory = $true)] [IntPtr] $WindowHandle,
        [int] $TimeoutSeconds = 10
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not [OpenKBTrayNativeMethods]::IsWindowVisible($WindowHandle)) {
            return
        }
        Start-Sleep -Milliseconds 50
    }
    throw "OPENKB_WINDOW_NOT_HIDDEN: Closing the main window did not hide it to the tray."
}

function Get-DescendantProcessIds {
    param([Parameter(Mandatory = $true)] [int] $RootProcessId)

    $processes = @(Get-CimInstance Win32_Process)
    $pending = New-Object "System.Collections.Generic.Queue[int]"
    $descendants = New-Object "System.Collections.Generic.List[int]"
    $pending.Enqueue($RootProcessId)
    while ($pending.Count -gt 0) {
        $parentProcessId = $pending.Dequeue()
        foreach ($process in @($processes | Where-Object { [int] $_.ParentProcessId -eq $parentProcessId })) {
            $processId = [int] $process.ProcessId
            $descendants.Add($processId)
            $pending.Enqueue($processId)
        }
    }
    return @($descendants)
}

$ExecutablePath = [System.IO.Path]::GetFullPath($ExecutablePath)
Assert-That -Condition (Test-Path -LiteralPath $ExecutablePath -PathType Leaf) -Message "OpenKB executable does not exist: $ExecutablePath"
$currentSessionId = (Get-Process -Id $PID).SessionId
$interactiveExplorer = Get-Process explorer -ErrorAction SilentlyContinue |
    Where-Object { $_.SessionId -eq $currentSessionId } |
    Select-Object -First 1
Assert-That -Condition ($null -ne $interactiveExplorer) -Message "INTERACTIVE_SESSION_REQUIRED: Run this test in the signed-in Windows desktop session."

$existingProcesses = @(
    Get-Process OpenKB, OpenKBEngine -ErrorAction SilentlyContinue
)
Assert-That -Condition ($existingProcesses.Count -eq 0) -Message "OPENKB_ALREADY_RUNNING: Close the existing OpenKB instance before running the tray test."
Send-Escape
Start-Sleep -Milliseconds 100

$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $ExecutablePath
$startInfo.WorkingDirectory = Split-Path -Parent $ExecutablePath
$startInfo.UseShellExecute = $false
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo
Assert-That -Condition ($process.Start()) -Message "Could not start OpenKB.exe."

try {
    $windowHandle = Wait-ForMainWindow -Process $process
    Assert-That -Condition ([OpenKBTrayNativeMethods]::PostMessage($windowHandle, $WM_CLOSE, [IntPtr]::Zero, [IntPtr]::Zero)) -Message "Could not request the OpenKB main window to close."
    Wait-ForWindowHidden -WindowHandle $windowHandle
    Wait-ForTrayIcon | Out-Null

    for ($iteration = 1; $iteration -le $Iterations; $iteration++) {
        $trayIcon = Wait-ForTrayIcon
        Send-RightClick -Element $trayIcon
        if ($ResultPath) {
            Save-DesktopScreenshot -Path ("{0}.iteration-{1}-immediate.png" -f $ResultPath, $iteration)
        }
        $quitMenuItem = Assert-QuitMenuRemainsVisible -Iteration $iteration -WindowHandle $windowHandle
        Write-Host "TRAY_MENU_VISIBLE iteration=$iteration minimum_ms=$MinimumVisibleMilliseconds"

        if ($iteration -lt $Iterations) {
            Send-Escape
            Start-Sleep -Milliseconds 150
            Assert-That -Condition ($null -eq (Find-QuitMenuItem)) -Message "TRAY_MENU_NOT_DISMISSED: Escape did not close the tray menu after iteration $iteration."
            continue
        }

        $trackedProcessIds = @($process.Id) + @(Get-DescendantProcessIds -RootProcessId $process.Id)
        Assert-That -Condition (Invoke-UiElement -Element $quitMenuItem) -Message "TRAY_QUIT_NOT_INVOKABLE: Quit OpenKB does not expose an invoke action."
        $deadline = [DateTime]::UtcNow.AddSeconds(15)
        $remainingProcessIds = $trackedProcessIds
        while ([DateTime]::UtcNow -lt $deadline) {
            $remainingProcessIds = @(
                $trackedProcessIds | Where-Object {
                    $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue)
                }
            )
            if ($remainingProcessIds.Count -eq 0) {
                break
            }
            Start-Sleep -Milliseconds 100
        }
        Assert-That -Condition ($remainingProcessIds.Count -eq 0) -Message "TRAY_QUIT_PROCESS_LEAK: Processes remained after Quit OpenKB: $($remainingProcessIds -join ', ')."
    }

    Write-Host "TRAY_MENU_ACCEPTANCE passed iterations=$Iterations minimum_visible_ms=$MinimumVisibleMilliseconds"
}
finally {
    Send-Escape
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
    $process.Dispose()
}

if ($transcriptStarted) {
    Stop-Transcript | Out-Null
}
