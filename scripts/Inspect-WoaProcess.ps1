[CmdletBinding()]
param(
    [Parameter(Mandatory)][int]$ProcessId,
    [Parameter(Mandatory)][string]$ExpectedExecutable,
    [Parameter(Mandatory)][string]$ApplicationRoot,
    [Parameter(Mandatory)][ValidateSet("x64", "arm64")][string]$ExpectedArchitecture,
    [Parameter(Mandatory)][string]$ReportPath,
    [string[]]$RequiredPackageModule = @("coreclr.dll")
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Add-Type -AssemblyName System.Reflection.Metadata

if (-not ("Woa.ProcessMachineInformation" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace Woa
{
    public static class ProcessMachineInformation
    {
        // SDK processthreadsapi.h, from Microsoft's win32metadata SDK headers:
        // https://github.com/microsoft/win32metadata/blob/main/generation/WinSDK/RecompiledIdlHeaders/um/processthreadsapi.h
        private enum ProcessInformationClass
        {
            ProcessMemoryPriority, ProcessMemoryExhaustionInfo, ProcessAppMemoryInfo,
            ProcessInPrivateInfo, ProcessPowerThrottling, ProcessReservedValue1,
            ProcessTelemetryCoverageInfo, ProcessProtectionLevelInfo,
            ProcessLeapSecondInfo, ProcessMachineTypeInfo
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct NativeMachineInformation
        {
            public ushort ProcessMachine;
            public ushort Reserved;
            public uint MachineAttributes;
        }

        [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetProcessInformation(
            SafeProcessHandle process, ProcessInformationClass informationClass,
            out NativeMachineInformation information, uint informationSize);

        // SDK wow64apiset.h: https://learn.microsoft.com/windows/win32/api/wow64apiset/nf-wow64apiset-iswow64process2
        [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool IsWow64Process2(
            SafeProcessHandle process, out ushort processMachine, out ushort nativeMachine);

        public static uint[] Query(Process process)
        {
            if (!GetProcessInformation(process.SafeHandle, ProcessInformationClass.ProcessMachineTypeInfo,
                out var information, (uint)Marshal.SizeOf<NativeMachineInformation>()))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            if (!IsWow64Process2(process.SafeHandle, out var guest, out var native))
                throw new Win32Exception(Marshal.GetLastWin32Error());
            return new uint[] { information.ProcessMachine, guest, native, information.MachineAttributes };
        }
    }
}
'@
}

$root = [IO.Path]::GetFullPath($ApplicationRoot).TrimEnd('\')
$expectedPath = [IO.Path]::GetFullPath($ExpectedExecutable)
if (-not $expectedPath.StartsWith("$root\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "The expected executable must be inside the approved application root."
}
foreach ($name in $RequiredPackageModule) {
    if ([IO.Path]::GetFileName($name) -ne $name -or $name -notmatch "\.dll$") {
        throw "Required package modules must be DLL file names: $name"
    }
}

$process = Get-Process -Id $ProcessId -ErrorAction Stop
try {
    $startedAt = $process.StartTime.ToUniversalTime().ToString("o")
    $image = [IO.Path]::GetFullPath($process.MainModule.FileName)
    $machines = [Woa.ProcessMachineInformation]::Query($process)
    $machine = [System.Reflection.PortableExecutable.Machine]$machines[0]
    $guest = [System.Reflection.PortableExecutable.Machine]$machines[1]
    $native = [System.Reflection.PortableExecutable.Machine]$machines[2]
    $expected = if ($ExpectedArchitecture -eq "arm64") { "Arm64" } else { "Amd64" }
    $errors = @()
    if (-not $image.Equals($expectedPath, [StringComparison]::OrdinalIgnoreCase)) {
        $errors += "The running image is not the approved executable."
    }
    if ($machine.ToString() -ne $expected) {
        $errors += "Expected $expected process, observed $machine."
    }
    $imageStream = [IO.File]::OpenRead($image)
    $imageReader = $null
    try {
        $imageReader = [System.Reflection.PortableExecutable.PEReader]::new($imageStream)
        $imageMachine = $imageReader.PEHeaders.CoffHeader.Machine.ToString()
        if ($imageMachine -ne $expected) {
            $errors += "Expected $expected executable image, observed $imageMachine."
        }
    } finally {
        if ($imageReader) { $imageReader.Dispose() }
        $imageStream.Dispose()
    }

    $modules = @($process.Modules | ForEach-Object {
        $path = [IO.Path]::GetFullPath($_.FileName)
        $inPackage = $path.StartsWith("$root\", [StringComparison]::OrdinalIgnoreCase)
        [pscustomobject]@{
            name = $_.ModuleName
            path = $path
            packageRelativePath = if ($inPackage) { [IO.Path]::GetRelativePath($root, $path).Replace('\', '/') } else { $null }
            sha256 = if ($inPackage) { (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant() } else { $null }
        }
    })
    foreach ($name in $RequiredPackageModule) {
        $matches = @($modules | Where-Object { $_.name -ieq $name -and $_.packageRelativePath })
        if ($matches.Count -ne 1) {
            $errors += "Expected exactly one package-loaded $name; found $($matches.Count)."
        }
    }
    $process.Refresh()
    if ($process.HasExited) {
        $errors += "The inspected process exited before collection finished."
    }
    $report = [pscustomobject]@{
        schemaVersion = 1
        collectedAt = [DateTimeOffset]::Now.ToString("o")
        processId = $ProcessId
        processStartedAt = $startedAt
        executable = $image
        executableSha256 = (Get-FileHash -LiteralPath $image -Algorithm SHA256).Hash.ToLowerInvariant()
        osArchitecture = [Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
        processArchitecture = $machine.ToString()
        guestMachine = $guest.ToString()
        nativeMachine = $native.ToString()
        imageArchitecture = $imageMachine
        machineAttributes = $machines[3]
        architectureApi = "GetProcessInformation(ProcessMachineTypeInfo)"
        hostArchitectureApi = "IsWow64Process2"
        requiredPackageModules = $RequiredPackageModule
        modules = $modules
        passed = $errors.Count -eq 0
        errors = $errors
    }
    [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($ReportPath)))
    $report | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $ReportPath -Encoding utf8
    if (-not $report.passed) {
        throw ($errors -join " ")
    }
    $report | Select-Object processId, processArchitecture, nativeMachine, executable, passed | ConvertTo-Json
} finally {
    $process.Dispose()
}
