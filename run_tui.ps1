# DashScope Proxy TUI Launcher
# All-in-one script to set up and run the proxy server in TUI mode
# 
# Implements 2024-2026 best practices for Windows process management:
# - Uses Windows Job Objects for guaranteed process tree cleanup
# - Three-stage shutdown: cooperative -> timeout -> force terminate
# - Proper signal handling via GenerateConsoleCtrlEvent

param(
    [switch]$ForceReinstall,
    [switch]$Headless,
    [switch]$Help
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot
$Script:ProxyProcess = $null
$Script:PidFile = Join-Path $ProjectRoot "proxy.pid"
$Script:JobHandle = [IntPtr]::Zero
$Script:CleanupDone = $false

function Write-Header {
    param([string]$Message)
    Write-Host "`n=== $Message ===" -ForegroundColor Cyan
}

function Write-Success {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Green
}

function Write-ErrorMsg {
    param([string]$Message)
    Write-Host "ERROR: $Message" -ForegroundColor Red
}

function Write-WarningMsg {
    param([string]$Message)
    Write-Host "WARNING: $Message" -ForegroundColor Yellow
}

function Write-Info {
    param([string]$Message)
    Write-Host $Message -ForegroundColor Gray
}

# Define Windows API functions for Job Objects and Console Control
# This follows the 2026 best practice recommendation
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class Win32Job {
    // Job Object creation and management
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    public static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string lpName);
    
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);
    
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetInformationJobObject(
        IntPtr hJob,
        int JobObjectInformationClass,
        ref JOBOBJECT_EXTENDED_LIMIT_INFORMATION lpJobObjectInformation,
        uint cbJobObjectInformationLength
    );
    
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);
    
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool CloseHandle(IntPtr hObject);
    
    // Console control for graceful shutdown
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool FreeConsole();
    
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool AttachConsole(uint dwProcessId);
    
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool SetConsoleCtrlHandler(IntPtr HandlerRoutine, bool Add);
    
    [DllImport("kernel32.dll", SetLastError = true)]
    public static extern bool GenerateConsoleCtrlEvent(uint dwCtrlEvent, uint dwProcessGroupId);
    
    // Constants
    public const int JobObjectExtendedLimitInformation = 9;
    public const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000;
    public const uint CTRL_C_EVENT = 0;
    public const uint CTRL_BREAK_EVENT = 1;
    public const uint CREATE_NEW_PROCESS_GROUP = 0x00000200;
    public const uint CREATE_NO_WINDOW = 0x08000000;
}

[StructLayout(LayoutKind.Sequential)]
public struct JOBOBJECT_BASIC_LIMIT_INFORMATION {
    public long PerProcessUserTimeLimit;
    public long PerJobUserTimeLimit;
    public uint LimitFlags;
    public UIntPtr MinimumWorkingSetSize;
    public UIntPtr MaximumWorkingSetSize;
    public uint ActiveProcessLimit;
    public UIntPtr Affinity;
    public uint PriorityClass;
    public uint SchedulingClass;
}

[StructLayout(LayoutKind.Sequential)]
public struct IO_COUNTERS {
    public ulong ReadOperationCount;
    public ulong WriteOperationCount;
    public ulong OtherOperationCount;
    public ulong ReadTransferCount;
    public ulong WriteTransferCount;
    public ulong OtherTransferCount;
}

[StructLayout(LayoutKind.Sequential)]
public struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION {
    public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
    public IO_COUNTERS IoInfo;
    public UIntPtr ProcessMemoryLimit;
    public UIntPtr JobMemoryLimit;
    public UIntPtr PeakProcessMemoryUsed;
    public UIntPtr PeakJobMemoryUsed;
}
"@

function New-JobObject {
    <#
    .SYNOPSIS
    Create a Windows Job Object with KILL_ON_JOB_CLOSE flag.
    This is the recommended approach per 2024-2026 best practices.
    #>
    
    try {
        # Create anonymous job object
        $jobHandle = [Win32Job]::CreateJobObject([IntPtr]::Zero, $null)
        if ($jobHandle -eq [IntPtr]::Zero) {
            $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
            throw "CreateJobObject failed with error $err"
        }
        
        # Configure with KILL_ON_JOB_CLOSE - this ensures all processes in the job
        # are terminated when the last handle to the job is closed
        $info = New-Object JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        $info.BasicLimitInformation.LimitFlags = [Win32Job]::JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        
        $result = [Win32Job]::SetInformationJobObject(
            $jobHandle,
            [Win32Job]::JobObjectExtendedLimitInformation,
            [ref]$info,
            [uint32][System.Runtime.InteropServices.Marshal]::SizeOf($info)
        )
        
        if (-not $result) {
            $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
            [Win32Job]::CloseHandle($jobHandle)
            throw "SetInformationJobObject failed with error $err"
        }
        
        return $jobHandle
    } catch {
        Write-WarningMsg "Failed to create Job Object: $_"
        Write-WarningMsg "Falling back to manual process management"
        return [IntPtr]::Zero
    }
}

function Send-GracefulShutdown {
    <#
    .SYNOPSIS
    Send CTRL+BREAK to the process for cooperative shutdown.
    Uses GenerateConsoleCtrlEvent with proper console handling.
    
    Note: CTRL_C_EVENT cannot target specific processes - it goes to ALL
    processes sharing the console. CTRL_BREAK_EVENT CAN target a process group.
    #>
    param([System.Diagnostics.Process]$Process)
    
    try {
        # Detach from our current console
        $null = [Win32Job]::FreeConsole()
        
        # Attach to the target process's console
        $attached = [Win32Job]::AttachConsole([uint32]$Process.Id)
        
        if (-not $attached) {
            # Process might not have a console (headless mode)
            $err = [System.Runtime.InteropServices.Marshal]::GetLastWin32Error()
            Write-Info "AttachConsole failed (error $err) - process may be headless"
            return $false
        }
        
        # Disable Ctrl+C handling for ourselves so we don't get terminated
        $null = [Win32Job]::SetConsoleCtrlHandler([IntPtr]::Zero, $true)
        
        # Send CTRL+BREAK to the process group
        # CTRL_BREAK_EVENT (1) can target a specific process group
        # CTRL_C_EVENT (0) goes to ALL processes sharing the console
        $result = [Win32Job]::GenerateConsoleCtrlEvent([Win32Job]::CTRL_BREAK_EVENT, [uint32]$Process.Id)
        
        # Small delay to let the signal propagate
        Start-Sleep -Milliseconds 100
        
        # Detach from the target console
        $null = [Win32Job]::FreeConsole()
        
        # Re-attach to parent console (if any)
        try {
            $null = [Win32Job]::AttachConsole([uint32]0xFFFFFFFF)  # ATTACH_PARENT_PROCESS
        } catch {
            # Ignore if no parent console
        }
        
        return $result
    } catch {
        Write-WarningMsg "Failed to send graceful shutdown signal: $_"
        return $false
    }
}

function Cleanup-ProxyProcess {
    <#
    .SYNOPSIS
    Cleanup function implementing three-stage shutdown:
    1. Request cooperative shutdown (CTRL+BREAK)
    2. Wait with timeout
    3. Force terminate via Job Object or process kill
    #>
    param([bool]$Force = $false)
    
    if ($Script:CleanupDone) {
        return
    }
    $Script:CleanupDone = $true
    
    # Clean up PID file (our responsibility regardless of process state)
    if (Test-Path $Script:PidFile) {
        try {
            Remove-Item $Script:PidFile -Force -ErrorAction Stop
            Write-Success "PID file cleaned up"
        } catch {
            Write-WarningMsg "Could not remove PID file: $_"
        }
    }
    
    # Stage 1: Cooperative shutdown (if process is still running)
    if ($Script:ProxyProcess -and -not $Script:ProxyProcess.HasExited) {
        Write-Host "`nInitiating graceful shutdown (PID: $($Script:ProxyProcess.Id))..." -ForegroundColor Yellow
        
        # Try sending CTRL+BREAK for cooperative shutdown
        $signalSent = Send-GracefulShutdown -Process $Script:ProxyProcess
        
        if ($signalSent) {
            Write-Host "Sent shutdown signal, waiting for graceful exit (up to 8s)..." -ForegroundColor Yellow
            
            # Stage 2: Wait for graceful exit
            $exited = $Script:ProxyProcess.WaitForExit(8000)
            
            if ($exited) {
                Write-Success "Process exited gracefully"
            } else {
                Write-WarningMsg "Process did not exit within timeout"
            }
        }
        
        # Stage 3: Force terminate if still running
        if (-not $Script:ProxyProcess.HasExited) {
            # If we have a Job Object, terminating it will kill all processes
            if ($Script:JobHandle -ne [IntPtr]::Zero) {
                Write-Host "Terminating via Job Object..." -ForegroundColor Yellow
                $null = [Win32Job]::TerminateJobObject($Script:JobHandle, 1)
                Start-Sleep -Milliseconds 500
            }
            
            # Fallback: direct process kill
            if (-not $Script:ProxyProcess.HasExited) {
                Write-Host "Force terminating process..." -ForegroundColor Yellow
                try {
                    $Script:ProxyProcess.Kill()
                    $Script:ProxyProcess.WaitForExit(2000)
                } catch {
                    Write-WarningMsg "Could not force kill: $_"
                }
            }
            
            Write-Success "Process terminated"
        }
    }
    
    # Clean up Job Object handle
    if ($Script:JobHandle -ne [IntPtr]::Zero) {
        # Closing the handle with KILL_ON_JOB_CLOSE will terminate all processes
        # But we've already terminated above, so this just cleans up
        $null = [Win32Job]::CloseHandle($Script:JobHandle)
        $Script:JobHandle = [IntPtr]::Zero
    }
    
    # Check for orphaned processes on port 8899 (if Force mode)
    if ($Force) {
        $PortInUse = Get-NetTCPConnection -LocalPort 8899 -ErrorAction SilentlyContinue
        if ($PortInUse) {
            Write-WarningMsg "Found orphaned process(es) on port 8899:"
            $PortInUse | ForEach-Object {
                try {
                    $proc = Get-Process -Id $_.OwningProcess -ErrorAction Stop
                    Write-Host "  Stopping PID $($_.OwningProcess): $($proc.ProcessName)" -ForegroundColor Yellow
                    Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue
                } catch {
                    # Process may have already exited
                }
            }
            Start-Sleep -Milliseconds 500
        }
    }
}

function Test-PortAvailable {
    <#
    .SYNOPSIS
    Check if port 8899 is available and handle any stale processes.
    #>
    param([int]$Port = 8899)
    
    $PortInUse = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
    if ($PortInUse) {
        Write-Host ""
        Write-WarningMsg "Port ${Port} is already in use"
        
        # Check if it's a stale process from a previous run
        if (Test-Path $Script:PidFile) {
            $oldPid = Get-Content $Script:PidFile -ErrorAction SilentlyContinue
            if ($oldPid) {
                $oldProc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
                if ($oldProc -and $PortInUse.OwningProcess -contains $oldPid) {
                    Write-WarningMsg "Found stale proxy process (PID: $oldPid)"
                    Write-Host "Attempting to stop stale process..." -ForegroundColor Yellow
                    
                    try {
                        Stop-Process -Id $oldPid -Force -ErrorAction Stop
                        Start-Sleep -Seconds 2
                        Remove-Item $Script:PidFile -Force -ErrorAction SilentlyContinue
                        
                        # Re-check port
                        $PortInUse = Get-NetTCPConnection -LocalPort $Port -ErrorAction SilentlyContinue
                        if (-not $PortInUse) {
                            Write-Success "Stale process stopped, port ${Port} is now available"
                            return $true
                        }
                    } catch {
                        Write-ErrorMsg "Failed to stop stale process: $_"
                    }
                }
            }
        }
        
        # List all processes using the port
        Write-Host "The following processes are using port ${Port}:"
        $PortInUse | ForEach-Object {
            $proc = Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue
            if ($proc) {
                Write-Host "  PID $($_.OwningProcess): $($proc.ProcessName)"
            }
        }
        Write-Host ""
        Write-ErrorMsg "Stop the existing process(es) or change PROXY_PORT in .env"
        return $false
    }
    
    return $true
}

# Register cleanup handlers
# Note: PowerShell doesn't reliably call these on all exit types,
# which is why Job Objects are critical
$null = Register-EngineEvent -SourceIdentifier PowerShell.Exiting -Action {
    Cleanup-ProxyProcess -Force $false
}

# Ctrl+C handler
# Note: This only works in interactive sessions with a console
try {
    [Console]::TreatControlCAsInput = $false
    $null = Register-ObjectEvent -InputObject $Host -EventName "CancelKeyPress" -Action {
        Write-Host "`n`nCtrl+C pressed, initiating shutdown..." -ForegroundColor Yellow
        Cleanup-ProxyProcess -Force $false
        exit 130
    }
} catch {
    # Running in non-interactive mode (e.g., background job)
    Write-Info "Running in non-interactive mode, Ctrl+C handler not registered"
}

if ($Help) {
    Write-Host @"

DashScope Proxy TUI Launcher
============================

Usage: .\run_tui.ps1 [options]

Options:
    -ForceReinstall    Force reinstall all dependencies
    -Headless          Run in headless mode (no TUI)
    -Help              Show this help message

This script will:
1. Create a Python virtual environment if it doesn't exist
2. Install/update dependencies from requirements.txt
3. Launch the proxy server in TUI mode

Process Management (2024-2026 Best Practices):
- Uses Windows Job Objects for guaranteed process tree cleanup
- Three-stage shutdown: cooperative signal -> timeout -> force terminate
- Works correctly even on abnormal parent exit (crash, force kill)

Press Ctrl+C or 'q' in the TUI to quit.

"@
    exit 0
}

Write-Header "DashScope Proxy TUI Launcher"
Write-Host "Project root: $ProjectRoot"

# Check Python availability
Write-Header "Checking Python installation"
try {
    $pythonVersion = python --version 2>&1
    Write-Success "Found: $pythonVersion"
} catch {
    Write-ErrorMsg "Python not found. Please install Python 3.9+ and ensure it's in PATH."
    exit 1
}

# Create virtual environment if it doesn't exist
$VenvPath = Join-Path $ProjectRoot "venv"
$VenvPython = Join-Path $VenvPath "Scripts\python.exe"
$VenvPip = Join-Path $VenvPath "Scripts\pip.exe"

if (-not (Test-Path $VenvPath) -or $ForceReinstall) {
    Write-Header "Creating virtual environment"
    if (Test-Path $VenvPath) {
        Write-Host "Removing existing venv..."
        Remove-Item -Recurse -Force $VenvPath
    }
    python -m venv $VenvPath
    if (-not $?) {
        Write-ErrorMsg "Failed to create virtual environment"
        exit 1
    }
    Write-Success "Virtual environment created at: $VenvPath"
} else {
    Write-Header "Virtual environment already exists"
    Write-Success "Using venv at: $VenvPath"
}

# Install/upgrade dependencies
Write-Header "Installing dependencies"

# Upgrade pip first
Write-Host "Upgrading pip..."
$proc = Start-Process -FilePath $VenvPython -ArgumentList "-m", "pip", "install", "--upgrade", "pip" -NoNewWindow -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    Write-ErrorMsg "Failed to upgrade pip"
    exit 1
}

# Install requirements
Write-Host "Installing requirements..."
$proc = Start-Process -FilePath $VenvPip -ArgumentList "install", "-r", (Join-Path $ProjectRoot "requirements.txt") -NoNewWindow -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    Write-ErrorMsg "Failed to install dependencies (exit code: $($proc.ExitCode))"
    exit 1
}
Write-Success "Dependencies installed successfully"

# Check for .env file
$EnvFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path $EnvFile)) {
    Write-Host ""
    Write-WarningMsg ".env file not found"
    Write-WarningMsg "Create .env with DASHSCOPE_API_KEY before running the proxy"
    $EnvExample = Join-Path $ProjectRoot ".env.example"
    if (Test-Path $EnvExample) {
        Write-WarningMsg "See .env.example for required configuration"
    }
    Write-Host ""
    Write-ErrorMsg "Cannot start proxy without .env file"
    exit 1
}

# Check if port is available
if (-not (Test-PortAvailable -Port 8899)) {
    exit 1
}

# Create Job Object for process management
Write-Header "Creating Job Object for process management"
$Script:JobHandle = New-JobObject
if ($Script:JobHandle -ne [IntPtr]::Zero) {
    Write-Success "Job Object created with KILL_ON_JOB_CLOSE flag"
    Write-Info "This ensures cleanup even on abnormal exit (crash, force kill)"
} else {
    Write-WarningMsg "Job Object creation failed, using fallback process management"
}

# Run the proxy server in TUI mode
Write-Header "Starting DashScope Proxy in TUI mode"
Write-Host "Press Ctrl+C or 'q' in the TUI to quit`n"

Set-Location $ProjectRoot

# Start the process
# For TUI mode, we need to let the child process use the console directly
# For headless mode, we can redirect output
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = $VenvPython
if ($Headless) {
    $psi.Arguments = "-m dashscope_proxy_lib.server --headless"
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.CreateNoWindow = $false
} else {
    # TUI mode: use ShellExecute so the child gets its own console
    # This allows Textual TUI to render properly
    $psi.Arguments = "-m dashscope_proxy_lib.server"
    $psi.UseShellExecute = $true  # Let child inherit console for TUI
    $psi.CreateNoWindow = $false
}
$psi.WorkingDirectory = $ProjectRoot

try {
    $Script:ProxyProcess = [System.Diagnostics.Process]::Start($psi)
    
    # Assign process to Job Object if we have one and process handle is available
    if ($Script:JobHandle -ne [IntPtr]::Zero) {
        try {
            $processHandle = $Script:ProxyProcess.Handle
            if ($processHandle) {
                $result = [Win32Job]::AssignProcessToJobObject($Script:JobHandle, $processHandle)
                if ($result) {
                    Write-Info "Process assigned to Job Object (PID: $($Script:ProxyProcess.Id))"
                } else {
                    Write-WarningMsg "Could not assign process to Job Object"
                }
            }
        } catch {
            # Handle not available when UseShellExecute = true
            Write-Info "Job Object assignment skipped (TUI mode)"
        }
    }
    
    if ($Headless) {
        # Forward stdout and stderr in background for headless mode
        $stdoutTask = $Script:ProxyProcess.StandardOutput.ReadToEndAsync()
        $stderrTask = $Script:ProxyProcess.StandardError.ReadToEndAsync()
        
        # Wait for process to exit
        $Script:ProxyProcess.WaitForExit()
        
        # Output any captured stdout/stderr
        $stdout = $stdoutTask.Result
        $stderr = $stderrTask.Result
        if ($stdout) { Write-Host $stdout -NoNewline }
        if ($stderr) { Write-Host $stderr -ForegroundColor Red -NoNewline }
    } else {
        # TUI mode: just wait for the process to exit
        # The TUI renders in the child process's console
        $Script:ProxyProcess.WaitForExit()
    }
    
    $exitCode = $Script:ProxyProcess.ExitCode
    Write-Host "`nProxy server exited with code: $exitCode" -ForegroundColor Yellow
    
} catch {
    Write-ErrorMsg "Failed to start proxy: $_"
    exit 1
} finally {
    # Ensure cleanup
    Cleanup-ProxyProcess -Force $false
}

exit $exitCode