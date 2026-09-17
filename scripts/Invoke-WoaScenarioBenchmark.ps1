[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [string]$ConfigurationPath,

    [Parameter(Mandatory)]
    [string]$OutputDirectory,

    [Parameter(Mandatory)]
    [string]$ArchitectureLabel,

    [Parameter(Mandatory)]
    [string]$ApplicationRoot,

    [Parameter(Mandatory)]
    [string]$EvidenceRoot,

    [Parameter(Mandatory)]
    [string]$ManifestPath,

    [Parameter(Mandatory)]
    [ValidatePattern('^[0-9a-fA-F]{64}$')]
    [string]$ApprovedManifestSha256,

    [ValidateSet("x64", "arm64")]
    [string]$ExpectedArchitecture,

    [AllowEmptyCollection()]
    [string[]]$RequiredPackageModule = @(),

    [ValidateRange(1, 1000)]
    [int]$Trials = 20,

    [switch]$Smoke,

    [switch]$ValidationOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes

function Get-FullPath {
    param([Parameter(Mandatory)][string]$Path)

    return [IO.Path]::GetFullPath($Path)
}

function Get-StringSha256 {
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Value
    )

    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    return [Convert]::ToHexString(
        [Security.Cryptography.SHA256]::HashData($bytes)).ToLowerInvariant()
}

function Get-DirectoryFingerprint {
    param([Parameter(Mandatory)][string]$Path)

    $root = Get-FullPath $Path
    $builder = [Text.StringBuilder]::new()
    foreach ($file in Get-ChildItem -LiteralPath $root -File -Force -Recurse | Sort-Object FullName) {
        $relativePath = [IO.Path]::GetRelativePath($root, $file.FullName)
        [void]$builder.AppendLine($relativePath)
        [void]$builder.AppendLine($file.Length)
        [void]$builder.AppendLine(
            (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant())
    }
    return Get-StringSha256 $builder.ToString()
}

function Get-OptionalPropertyValue {
    param(
        [Parameter(Mandatory)]$InputObject,
        [Parameter(Mandatory)][string]$Name,
        $DefaultValue = $null
    )

    $property = $InputObject.PSObject.Properties[$Name]
    if ($property) {
        return $property.Value
    }

    return $DefaultValue
}

function Assert-ObjectProperties {
    param(
        [Parameter(Mandatory)]$InputObject,
        [Parameter(Mandatory)][string[]]$Allowed,
        [string[]]$Required = @(),
        [Parameter(Mandatory)][string]$Context
    )

    if (-not $InputObject -or -not $InputObject.PSObject) {
        throw "$Context must be a JSON object."
    }
    $names = @($InputObject.PSObject.Properties.Name)
    $unexpected = @($names | Where-Object { $Allowed -notcontains $_ })
    if ($unexpected.Count -gt 0) {
        throw "$Context contains unsupported properties: $($unexpected -join ', ')"
    }
    $missing = @($Required | Where-Object { $names -notcontains $_ })
    if ($missing.Count -gt 0) {
        throw "$Context is missing required properties: $($missing -join ', ')"
    }
}

function Assert-SafeRelativePathSegments {
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Path,
        [Parameter(Mandatory)][string]$Context,
        [switch]$AllowEmpty
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        if ($AllowEmpty) {
            return
        }
        throw "$Context must not be empty."
    }
    if ($Path.IndexOfAny([IO.Path]::GetInvalidPathChars()) -ge 0) {
        throw "$Context contains invalid path characters: $Path"
    }
    if ($Path.Contains(':')) {
        throw "$Context must not contain alternate data stream or drive syntax: $Path"
    }

    $reservedNames = @("CON", "PRN", "AUX", "NUL", "CLOCK$",
        "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
        "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9")
    foreach ($segment in @($Path -split '[\\/]+')) {
        if ([string]::IsNullOrWhiteSpace($segment)) {
            throw "$Context contains an empty path segment: $Path"
        }
        if ($segment.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0) {
            throw "$Context contains invalid path characters: $Path"
        }
        if ($segment -eq "." -or $segment -eq "..") {
            throw "$Context must not contain traversal segments: $Path"
        }
        if ($segment.EndsWith(".", [StringComparison]::Ordinal) -or
            $segment.EndsWith(" ", [StringComparison]::Ordinal)) {
            throw "$Context contains a segment with an invalid trailing dot or space: $Path"
        }
        $stem = ($segment -split '\.')[0].ToUpperInvariant()
        if ($reservedNames -contains $stem) {
            throw "$Context contains reserved device name '$segment'."
        }
    }
}

function Assert-LocalAbsolutePath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Context
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Context must not be empty."
    }
    if ($Path.IndexOfAny([IO.Path]::GetInvalidPathChars()) -ge 0) {
        throw "$Context contains invalid path characters: $Path"
    }
    $pathRoot = [IO.Path]::GetPathRoot($Path)
    if ([string]::IsNullOrWhiteSpace($pathRoot)) {
        throw "$Context must be rooted: $Path"
    }
    if ($Path.StartsWith("\\", [StringComparison]::Ordinal) -or
        $pathRoot.StartsWith("\\", [StringComparison]::Ordinal)) {
        throw "$Context must not use a UNC or network root: $Path"
    }
    if (-not [IO.Path]::IsPathFullyQualified($Path)) {
        throw "$Context must be an absolute local path: $Path"
    }
    if (($Path -split '[\\/]+') -contains "..") {
        throw "$Context must not contain traversal segments: $Path"
    }

    if ($Path.IndexOf(':', 2) -ge 0) {
        throw "$Context must not contain alternate data stream syntax: $Path"
    }
    $rawRelative = $Path.Substring($pathRoot.Length)
    Assert-SafeRelativePathSegments -Path $rawRelative -Context $Context -AllowEmpty

    $fullPath = Get-FullPath $Path
    $fullRoot = [IO.Path]::GetPathRoot($fullPath)
    $relative = $fullPath.Substring($fullRoot.Length)
    Assert-SafeRelativePathSegments -Path $relative -Context $Context -AllowEmpty
    return $fullPath
}

function Assert-NotOneDrivePath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Context
    )

    $fullPath = Get-FullPath $Path
    $oneDriveRoots = @(
        $env:OneDrive,
        $env:OneDriveCommercial,
        $env:OneDriveConsumer
    ) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) } | ForEach-Object {
        (Get-FullPath $_).TrimEnd('\')
    }
    foreach ($root in $oneDriveRoots) {
        if ($fullPath.Equals($root, [StringComparison]::OrdinalIgnoreCase) -or
            $fullPath.StartsWith("$root\", [StringComparison]::OrdinalIgnoreCase)) {
            throw "$Context must stay outside OneDrive: $fullPath"
        }
    }
    if ($fullPath -match '\\OneDrive( - [^\\]+)?\\') {
        throw "$Context must stay outside OneDrive: $fullPath"
    }
}

function Assert-NoReparsePathToDrive {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Context
    )

    $fullPath = Get-FullPath $Path
    $driveRoot = ([IO.Path]::GetPathRoot($fullPath)).TrimEnd('\')
    $current = $fullPath
    while (-not [string]::IsNullOrWhiteSpace($current)) {
        if (-not (Test-Path -LiteralPath $current)) {
            $parent = [IO.Directory]::GetParent($current)
            $current = if ($parent) { $parent.FullName } else { $null }
            continue
        }
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "$Context traverses a reparse point ancestor: $current"
        }
        if ($current.TrimEnd('\').Equals($driveRoot, [StringComparison]::OrdinalIgnoreCase)) {
            break
        }
        $parent = [IO.Directory]::GetParent($current)
        $current = if ($parent) { $parent.FullName } else { $null }
    }
}

function Assert-TrustedRoot {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Context
    )

    $fullPath = Assert-LocalAbsolutePath -Path $Path -Context $Context
    Assert-NotOneDrivePath -Path $fullPath -Context $Context
    Assert-NoReparsePathToDrive -Path $fullPath -Context $Context
    $trimmedFullPath = $fullPath.TrimEnd('\')
    $driveRoot = ([IO.Path]::GetPathRoot($fullPath)).TrimEnd('\')
    if ($trimmedFullPath.Equals($driveRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Context must not be a drive root: $fullPath"
    }
    $userProfile = [Environment]::GetFolderPath([Environment+SpecialFolder]::UserProfile)
    if (-not [string]::IsNullOrWhiteSpace($userProfile) -and
        $trimmedFullPath.Equals((Get-FullPath $userProfile).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Context must not be a user profile root: $fullPath"
    }
    $localApplicationData = [Environment]::GetFolderPath([Environment+SpecialFolder]::LocalApplicationData)
    if (-not [string]::IsNullOrWhiteSpace($localApplicationData) -and
        $trimmedFullPath.Equals((Get-FullPath $localApplicationData).TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
        throw "$Context must not be the LocalApplicationData root: $fullPath"
    }
    if (-not (Test-Path -LiteralPath $fullPath -PathType Container)) {
        throw "$Context does not exist: $fullPath"
    }

    return $trimmedFullPath
}

function Assert-DescendantPath {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Context,
        [switch]$AllowRoot
    )

    $fullPath = Assert-LocalAbsolutePath -Path $Path -Context $Context
    Assert-NotOneDrivePath -Path $fullPath -Context $Context
    $fullRoot = (Get-FullPath $Root).TrimEnd('\')
    $isRoot = $fullPath.Equals($fullRoot, [StringComparison]::OrdinalIgnoreCase)
    if (($isRoot -and -not $AllowRoot) -or
        (-not $isRoot -and -not $fullPath.StartsWith("$fullRoot\", [StringComparison]::OrdinalIgnoreCase))) {
        throw "$Context is outside the trusted root '$fullRoot': $fullPath"
    }
    Assert-NoReparsePathToDrive -Path $fullPath -Context $Context

    return $fullPath
}

function Assert-RelativePath {
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Path,
        [Parameter(Mandatory)][string]$Context,
        [switch]$AllowEmpty
    )

    if ([string]::IsNullOrWhiteSpace($Path)) {
        if ($AllowEmpty) {
            return ""
        }
        throw "$Context must not be empty."
    }
    if ([IO.Path]::IsPathFullyQualified($Path) -or $Path.StartsWith("\", [StringComparison]::Ordinal) -or $Path -match '^[a-zA-Z]:') {
        throw "$Context must be relative: $Path"
    }
    Assert-SafeRelativePathSegments -Path $Path -Context $Context -AllowEmpty:$AllowEmpty

    return $Path.Replace('/', '\')
}

function Join-TrustedRelativePath {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$RelativePath,
        [Parameter(Mandatory)][string]$Context,
        [switch]$AllowRoot
    )

    $relative = Assert-RelativePath -Path $RelativePath -Context $Context -AllowEmpty:$AllowRoot
    $candidate = if ([string]::IsNullOrWhiteSpace($relative)) { $Root } else { Join-Path $Root $relative }
    return Assert-DescendantPath -Path $candidate -Root $Root -Context $Context -AllowRoot:$AllowRoot
}

function Assert-NoReparseTree {
    param(
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Context
    )

    foreach ($item in Get-ChildItem -LiteralPath $Root -Force -Recurse) {
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            throw "$Context contains a reparse point: $($item.FullName)"
        }
    }
}

function Test-SequenceEqual {
    param(
        [Parameter(Mandatory)][string[]]$Left,
        [Parameter(Mandatory)][string[]]$Right
    )

    if ($Left.Count -ne $Right.Count) {
        return $false
    }
    for ($index = 0; $index -lt $Left.Count; $index++) {
        if ($Left[$index] -ne $Right[$index]) {
            return $false
        }
    }
    return $true
}

function Read-ApprovedManifest {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$ExpectedSha256,
        [Parameter(Mandatory)][string]$ApplicationRoot
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Manifest file does not exist: $Path"
    }
    $actualManifestHash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualManifestHash -ne $ExpectedSha256.ToLowerInvariant()) {
        throw "Manifest SHA256 does not match the operator-approved hash."
    }
    $manifest = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    Assert-ObjectProperties `
        -InputObject $manifest `
        -Allowed @("schemaVersion", "files") `
        -Required @("schemaVersion", "files") `
        -Context "Manifest"
    if ([int]$manifest.schemaVersion -ne 1) {
        throw "Unsupported manifest schema version: $($manifest.schemaVersion)"
    }

    Assert-NoReparseTree -Root $ApplicationRoot -Context "Application package"
    $manifestMap = @{}
    foreach ($file in @($manifest.files)) {
        Assert-ObjectProperties `
            -InputObject $file `
            -Allowed @("relativePath", "sha256") `
            -Required @("relativePath", "sha256") `
            -Context "Manifest file"
        $relativePath = Assert-RelativePath -Path ([string]$file.relativePath) -Context "Manifest relativePath"
        $hash = ([string]$file.sha256).ToLowerInvariant()
        if ($hash -notmatch '^[0-9a-f]{64}$') {
            throw "Manifest file '$relativePath' has an invalid SHA256."
        }
        $normalized = $relativePath.ToLowerInvariant()
        if ($manifestMap.ContainsKey($normalized)) {
            throw "Manifest contains duplicate file entry: $relativePath"
        }
        $fullPath = Join-TrustedRelativePath `
            -Root $ApplicationRoot `
            -RelativePath $relativePath `
            -Context "Manifest package file"
        if (-not (Test-Path -LiteralPath $fullPath -PathType Leaf)) {
            throw "Manifest package file is missing: $relativePath"
        }
        $actualHash = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $hash) {
            throw "Manifest package file hash mismatch: $relativePath"
        }
        $manifestMap[$normalized] = [pscustomobject]@{
            RelativePath = $relativePath
            FullPath = $fullPath
            Sha256 = $hash
        }
    }

    $actualFiles = @(Get-ChildItem -LiteralPath $ApplicationRoot -File -Force -Recurse | ForEach-Object {
        ([IO.Path]::GetRelativePath($ApplicationRoot, $_.FullName)).Replace('/', '\').ToLowerInvariant()
    } | Sort-Object)
    $manifestFiles = @($manifestMap.Keys | Sort-Object)
    if (-not (Test-SequenceEqual -Left ([string[]]$actualFiles) -Right ([string[]]$manifestFiles))) {
        $missing = @($manifestFiles | Where-Object { $actualFiles -notcontains $_ })
        $extra = @($actualFiles | Where-Object { $manifestFiles -notcontains $_ })
        throw "Manifest does not match the complete application package closure. Missing: $($missing -join ', ') Extra: $($extra -join ', ')"
    }

    return [pscustomobject]@{
        Sha256 = $actualManifestHash
        Files = $manifestMap
    }
}

function Get-RequiredManifestFile {
    param(
        [Parameter(Mandatory)]$Manifest,
        [Parameter(Mandatory)][string]$RelativePath,
        [Parameter(Mandatory)][string]$Context
    )

    $key = (Assert-RelativePath -Path $RelativePath -Context $Context).ToLowerInvariant()
    if (-not $Manifest.Files.ContainsKey($key)) {
        throw "$Context is not listed in the approved manifest: $RelativePath"
    }
    return $Manifest.Files[$key]
}

function Assert-Selector {
    param(
        [Parameter(Mandatory)]$Selector,
        [Parameter(Mandatory)][string]$Context
    )

    Assert-ObjectProperties `
        -InputObject $Selector `
        -Allowed @("automationId", "name", "controlType") `
        -Context $Context
    $automationId = [string](Get-OptionalPropertyValue -InputObject $Selector -Name "automationId" -DefaultValue "")
    $name = [string](Get-OptionalPropertyValue -InputObject $Selector -Name "name" -DefaultValue "")
    $controlType = [string](Get-OptionalPropertyValue -InputObject $Selector -Name "controlType" -DefaultValue "")
    if ([string]::IsNullOrWhiteSpace($automationId) -and [string]::IsNullOrWhiteSpace($name)) {
        throw "$Context must specify automationId or exact name."
    }
    foreach ($value in @($automationId, $name, $controlType)) {
        if ($value.Length -gt 512) {
            throw "$Context values must be 512 characters or shorter."
        }
    }
    if (-not [string]::IsNullOrWhiteSpace($controlType)) {
        [void](Get-ControlType -Name $controlType -Context $Context)
    }
}

function Get-ControlType {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Context
    )

    switch ($Name) {
        "Button" { return [System.Windows.Automation.ControlType]::Button }
        "CheckBox" { return [System.Windows.Automation.ControlType]::CheckBox }
        "ComboBox" { return [System.Windows.Automation.ControlType]::ComboBox }
        "Custom" { return [System.Windows.Automation.ControlType]::Custom }
        "DataItem" { return [System.Windows.Automation.ControlType]::DataItem }
        "Edit" { return [System.Windows.Automation.ControlType]::Edit }
        "Hyperlink" { return [System.Windows.Automation.ControlType]::Hyperlink }
        "List" { return [System.Windows.Automation.ControlType]::List }
        "ListItem" { return [System.Windows.Automation.ControlType]::ListItem }
        "MenuItem" { return [System.Windows.Automation.ControlType]::MenuItem }
        "Pane" { return [System.Windows.Automation.ControlType]::Pane }
        "RadioButton" { return [System.Windows.Automation.ControlType]::RadioButton }
        "TabItem" { return [System.Windows.Automation.ControlType]::TabItem }
        "Text" { return [System.Windows.Automation.ControlType]::Text }
        "TreeItem" { return [System.Windows.Automation.ControlType]::TreeItem }
        "Window" { return [System.Windows.Automation.ControlType]::Window }
        default { throw "$Context uses unsupported controlType '$Name'." }
    }
}

function Assert-Step {
    param(
        [Parameter(Mandatory)]$Step,
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][hashtable]$FixtureIds,
        [switch]$ReadOnly
    )

    $allowedTypes = @(
        "invoke",
        "toggle",
        "select",
        "set-value",
        "wait-element",
        "assert-element",
        "wait-property",
        "assert-property",
        "wait-text",
        "assert-text"
    )
    $readOnlyTypes = @(
        "wait-element",
        "assert-element",
        "wait-property",
        "assert-property",
        "wait-text",
        "assert-text"
    )
    Assert-ObjectProperties `
        -InputObject $Step `
        -Allowed @("id", "type", "selector", "timeoutMilliseconds", "value", "property", "expected", "expectedText") `
        -Required @("type") `
        -Context $Context
    $type = [string]$Step.type
    if ($allowedTypes -notcontains $type) {
        throw "$Context uses unsupported step type '$type'."
    }
    if ($ReadOnly -and $readOnlyTypes -notcontains $type) {
        throw "$Context launch-ready steps must be wait/assert steps, not '$type'."
    }
    $timeout = Get-OptionalPropertyValue -InputObject $Step -Name "timeoutMilliseconds" -DefaultValue 0
    if ($timeout -and ([int]$timeout -lt 10 -or [int]$timeout -gt 300000)) {
        throw "$Context timeoutMilliseconds is outside the supported range."
    }
    if ($type -in @("invoke", "toggle", "select", "set-value", "wait-element", "assert-element", "wait-property", "assert-property")) {
        $selector = Get-OptionalPropertyValue -InputObject $Step -Name "selector"
        if (-not $selector) {
            throw "$Context requires selector."
        }
        Assert-Selector -Selector $selector -Context "$Context selector"
    }
    if ($type -eq "set-value") {
        $value = Get-OptionalPropertyValue -InputObject $Step -Name "value"
        if ($null -eq $value -or ([string]$value).Length -gt 4096) {
            throw "$Context requires value with at most 4096 characters."
        }
        Assert-PlaceholderUse -Argument ([string]$value) -FixtureIds $FixtureIds -Context "$Context value"
    }
    if ($type -in @("wait-property", "assert-property")) {
        $property = [string](Get-OptionalPropertyValue -InputObject $Step -Name "property" -DefaultValue "")
        $supported = @("AutomationId", "Name", "ClassName", "ControlType", "HelpText", "Value", "ToggleState", "IsEnabled", "IsOffscreen")
        if ($supported -notcontains $property) {
            throw "$Context uses unsupported property '$property'."
        }
        if ($null -eq (Get-OptionalPropertyValue -InputObject $Step -Name "expected")) {
            throw "$Context requires expected."
        }
        $expected = Get-OptionalPropertyValue -InputObject $Step -Name "expected"
        if ($expected -is [string]) {
            Assert-PlaceholderUse -Argument ([string]$expected) -FixtureIds $FixtureIds -Context "$Context expected"
        }
    }
    if ($type -in @("wait-text", "assert-text")) {
        $expectedText = [string](Get-OptionalPropertyValue -InputObject $Step -Name "expectedText" -DefaultValue "")
        if ([string]::IsNullOrWhiteSpace($expectedText) -or $expectedText.Length -gt 4096) {
            throw "$Context requires exact expectedText with at most 4096 characters."
        }
        Assert-PlaceholderUse -Argument $expectedText -FixtureIds $FixtureIds -Context "$Context expectedText"
    }
}

function Assert-StepSequence {
    param(
        [Parameter(Mandatory)]$Steps,
        [Parameter(Mandatory)][string]$Context,
        [Parameter(Mandatory)][hashtable]$FixtureIds,
        [switch]$ReadOnly
    )

    $stepArray = @($Steps)
    if ($stepArray.Count -lt 1 -or $stepArray.Count -gt 100) {
        throw "$Context must contain 1 to 100 steps."
    }
    for ($index = 0; $index -lt $stepArray.Count; $index++) {
        Assert-Step `
            -Step $stepArray[$index] `
            -Context "$Context step $($index + 1)" `
            -FixtureIds $FixtureIds `
            -ReadOnly:$ReadOnly
    }
    $lastType = [string]$stepArray[$stepArray.Count - 1].type
    if ($lastType -notin @("wait-element", "assert-element", "wait-property", "assert-property", "wait-text", "assert-text")) {
        throw "$Context must end with a bounded wait/assert completion step."
    }
}

function Assert-PlaceholderUse {
    param(
        [Parameter(Mandatory)][string]$Argument,
        [Parameter(Mandatory)][hashtable]$FixtureIds,
        [Parameter(Mandatory)][string]$Context
    )

    foreach ($match in [regex]::Matches($Argument, '\$\{([^}]+)\}')) {
        $token = $match.Groups[1].Value
        if ($token -eq "trialRoot" -or $token -eq "profileDir") {
            continue
        }
        if ($token.StartsWith("fixture:", [StringComparison]::Ordinal)) {
            $fixtureId = $token.Substring("fixture:".Length)
            if ($FixtureIds.ContainsKey($fixtureId)) {
                continue
            }
        }
        throw "$Context uses unsupported placeholder '$($match.Value)'."
    }
}

function Read-ScenarioConfiguration {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$ApplicationRoot,
        [Parameter(Mandatory)][string]$EvidenceRoot,
        [Parameter(Mandatory)]$Manifest
    )

    $config = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    Assert-ObjectProperties `
        -InputObject $config `
        -Allowed @("schemaVersion", "application", "defaults", "fixtures", "scenarios") `
        -Required @("schemaVersion", "application", "scenarios") `
        -Context "Scenario configuration"
    if ([int]$config.schemaVersion -ne 1) {
        throw "Unsupported scenario configuration schema version: $($config.schemaVersion)"
    }
    Assert-ObjectProperties `
        -InputObject $config.application `
        -Allowed @("executableRelativePath", "workingDirectoryRelativePath", "arguments", "mainWindowSelector", "launchReadySteps", "requiredPackageModules") `
        -Required @("executableRelativePath", "workingDirectoryRelativePath", "arguments", "mainWindowSelector", "launchReadySteps") `
        -Context "application"

    $executableRelativePath = Assert-RelativePath -Path ([string]$config.application.executableRelativePath) -Context "application.executableRelativePath"
    $workingDirectoryRelativePath = Assert-RelativePath -Path ([string]$config.application.workingDirectoryRelativePath) -Context "application.workingDirectoryRelativePath" -AllowEmpty
    $executablePath = Join-TrustedRelativePath -Root $ApplicationRoot -RelativePath $executableRelativePath -Context "Application executable"
    $workingDirectory = Join-TrustedRelativePath -Root $ApplicationRoot -RelativePath $workingDirectoryRelativePath -Context "Application working directory" -AllowRoot
    if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
        throw "Application executable does not exist: $executablePath"
    }
    if (-not (Test-Path -LiteralPath $workingDirectory -PathType Container)) {
        throw "Application working directory does not exist: $workingDirectory"
    }
    [void](Get-RequiredManifestFile -Manifest $Manifest -RelativePath $executableRelativePath -Context "Application executable")
    Assert-Selector -Selector $config.application.mainWindowSelector -Context "application.mainWindowSelector"

    $defaults = Get-OptionalPropertyValue -InputObject $config -Name "defaults" -DefaultValue ([pscustomobject]@{})
    Assert-ObjectProperties `
        -InputObject $defaults `
        -Allowed @("actionTimeoutMilliseconds", "pollingIntervalMilliseconds", "processExitTimeoutMilliseconds") `
        -Context "defaults"
    $actionTimeout = [int](Get-OptionalPropertyValue -InputObject $defaults -Name "actionTimeoutMilliseconds" -DefaultValue 30000)
    $pollingInterval = [int](Get-OptionalPropertyValue -InputObject $defaults -Name "pollingIntervalMilliseconds" -DefaultValue 25)
    $processExitTimeout = [int](Get-OptionalPropertyValue -InputObject $defaults -Name "processExitTimeoutMilliseconds" -DefaultValue 15000)
    if ($actionTimeout -lt 10 -or $actionTimeout -gt 300000 -or
        $pollingInterval -lt 5 -or $pollingInterval -gt 1000 -or
        $processExitTimeout -lt 100 -or $processExitTimeout -gt 300000) {
        throw "defaults timeouts are outside the supported range."
    }

    $fixtureIds = @{}
    $fixtures = @()
    foreach ($fixture in @((Get-OptionalPropertyValue -InputObject $config -Name "fixtures" -DefaultValue @()))) {
        Assert-ObjectProperties `
            -InputObject $fixture `
            -Allowed @("id", "templatePath") `
            -Required @("id") `
            -Context "fixture"
        $fixtureId = [string]$fixture.id
        if ($fixtureId -notmatch '^[a-z0-9]+(?:-[a-z0-9]+)*$') {
            throw "Fixture id must be kebab-case: $fixtureId"
        }
        if ($fixtureIds.ContainsKey($fixtureId)) {
            throw "Fixture id is duplicated: $fixtureId"
        }
        $fixtureIds[$fixtureId] = $true
        $templatePath = Get-OptionalPropertyValue -InputObject $fixture -Name "templatePath"
        $resolvedTemplate = $null
        if ($templatePath) {
            $resolvedTemplate = Assert-DescendantPath -Path ([string]$templatePath) -Root $EvidenceRoot -Context "Fixture template"
            if (-not (Test-Path -LiteralPath $resolvedTemplate -PathType Container)) {
                throw "Fixture template does not exist: $resolvedTemplate"
            }
            Assert-NoReparseTree -Root $resolvedTemplate -Context "Fixture template"
        }
        $fixtures += [pscustomobject]@{
            Id = $fixtureId
            TemplatePath = $resolvedTemplate
            TemplateFingerprint = if ($resolvedTemplate) { Get-DirectoryFingerprint $resolvedTemplate } else { $null }
        }
    }
    Assert-StepSequence `
        -Steps $config.application.launchReadySteps `
        -Context "application.launchReadySteps" `
        -FixtureIds $fixtureIds `
        -ReadOnly

    $requiredPackageModules = @((Get-OptionalPropertyValue -InputObject $config.application -Name "requiredPackageModules" -DefaultValue @()) | ForEach-Object { [string]$_ })
    foreach ($module in $requiredPackageModules) {
        if ([IO.Path]::GetFileName($module) -ne $module -or $module -notmatch '\.dll$') {
            throw "application.requiredPackageModules entries must be DLL file names: $module"
        }
    }

    if ($null -eq $config.application.arguments -or
        $config.application.arguments -is [string] -or
        $config.application.arguments -is [System.Management.Automation.PSCustomObject]) {
        throw "application.arguments must be a JSON array."
    }
    $arguments = @($config.application.arguments | ForEach-Object { [string]$_ })
    foreach ($argument in $arguments) {
        if ($argument.Length -gt 4096) {
            throw "application.arguments entries must be 4096 characters or shorter."
        }
        Assert-PlaceholderUse -Argument $argument -FixtureIds $fixtureIds -Context "application.arguments"
    }

    $scenarioIds = @{}
    $scenarios = @()
    foreach ($scenario in @($config.scenarios)) {
        Assert-ObjectProperties `
            -InputObject $scenario `
            -Allowed @("id", "name", "timeoutMilliseconds", "steps") `
            -Required @("id", "steps") `
            -Context "scenario"
        $scenarioId = [string]$scenario.id
        if ($scenarioId -notmatch '^[a-z0-9]+(?:-[a-z0-9]+)*$') {
            throw "Scenario id must be kebab-case: $scenarioId"
        }
        if ($scenarioIds.ContainsKey($scenarioId)) {
            throw "Scenario id is duplicated: $scenarioId"
        }
        $scenarioIds[$scenarioId] = $true
        $scenarioTimeout = [int](Get-OptionalPropertyValue -InputObject $scenario -Name "timeoutMilliseconds" -DefaultValue $actionTimeout)
        if ($scenarioTimeout -lt 10 -or $scenarioTimeout -gt 300000) {
            throw "Scenario '$scenarioId' timeoutMilliseconds is outside the supported range."
        }
        Assert-StepSequence `
            -Steps $scenario.steps `
            -Context "scenario '$scenarioId'" `
            -FixtureIds $fixtureIds
        $scenarios += [pscustomobject]@{
            Id = $scenarioId
            Name = [string](Get-OptionalPropertyValue -InputObject $scenario -Name "name" -DefaultValue $scenarioId)
            TimeoutMilliseconds = $scenarioTimeout
            Steps = @($scenario.steps)
        }
    }
    if ($scenarios.Count -lt 1 -or $scenarios.Count -gt 100) {
        throw "Scenario configuration must contain 1 to 100 scenarios."
    }

    return [pscustomobject]@{
        Raw = $config
        Application = [pscustomobject]@{
            ExecutableRelativePath = $executableRelativePath
            ExecutablePath = $executablePath
            WorkingDirectoryRelativePath = $workingDirectoryRelativePath
            WorkingDirectory = $workingDirectory
            Arguments = $arguments
            MainWindowSelector = $config.application.mainWindowSelector
            LaunchReadySteps = @($config.application.launchReadySteps)
            RequiredPackageModules = $requiredPackageModules
        }
        Defaults = [pscustomobject]@{
            ActionTimeoutMilliseconds = $actionTimeout
            PollingIntervalMilliseconds = $pollingInterval
            ProcessExitTimeoutMilliseconds = $processExitTimeout
        }
        Fixtures = $fixtures
        Scenarios = $scenarios
    }
}

function New-RunIdentity {
    param(
        [Parameter(Mandatory)][string]$Architecture,
        [Parameter(Mandatory)][int]$TrialCount,
        [Parameter(Mandatory)][string]$ConfigurationPath,
        [Parameter(Mandatory)][string]$ManifestSha256,
        [Parameter(Mandatory)][string]$ApplicationRoot,
        [Parameter(Mandatory)][string]$EvidenceRoot,
        [Parameter(Mandatory)]$Configuration
    )

    $details = [ordered]@{
        ArchitectureLabel = $Architecture
        TargetTrials = $TrialCount
        ConfigurationSha256 = (Get-FileHash -LiteralPath $ConfigurationPath -Algorithm SHA256).Hash.ToLowerInvariant()
        ManifestSha256 = $ManifestSha256
        ApplicationRoot = $ApplicationRoot
        EvidenceRoot = $EvidenceRoot
        ApplicationExecutableRelativePath = $Configuration.Application.ExecutableRelativePath
        ApplicationArguments = $Configuration.Application.Arguments
        ScenarioIds = @($Configuration.Scenarios | ForEach-Object { $_.Id })
        FixtureIds = @($Configuration.Fixtures | ForEach-Object { $_.Id })
    }
    $json = $details | ConvertTo-Json -Depth 8 -Compress
    return [pscustomobject]@{
        Sha256 = Get-StringSha256 $json
        Details = $details
    }
}

function Assert-ScenarioInputsUnchanged {
    param(
        [Parameter(Mandatory)][string]$ConfigurationPath,
        [Parameter(Mandatory)][string]$ExpectedConfigurationSha256,
        [Parameter(Mandatory)]$Configuration
    )

    $actualConfigurationSha256 = (Get-FileHash -LiteralPath $ConfigurationPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualConfigurationSha256 -ne $ExpectedConfigurationSha256) {
        throw "Scenario configuration changed after validation."
    }
    foreach ($fixture in @($Configuration.Fixtures)) {
        if (-not $fixture.TemplatePath) {
            continue
        }
        $actualFingerprint = Get-DirectoryFingerprint $fixture.TemplatePath
        if ($actualFingerprint -ne $fixture.TemplateFingerprint) {
            throw "Fixture template '$($fixture.Id)' changed after validation."
        }
    }
}

function Assert-FreshOutputDirectory {
    param(
        [Parameter(Mandatory)][string]$Path
    )

    if (Test-Path -LiteralPath $Path) {
        throw "Output directory already exists and will not be overwritten: $Path"
    }
}

function New-StepCondition {
    param(
        [Parameter(Mandatory)]$Selector,
        [Parameter(Mandatory)][int]$ProcessId
    )

    $conditions = @(
        [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
            $ProcessId)
    )
    $automationId = [string](Get-OptionalPropertyValue -InputObject $Selector -Name "automationId" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($automationId)) {
        $conditions += [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::AutomationIdProperty,
            $automationId)
    }
    $name = [string](Get-OptionalPropertyValue -InputObject $Selector -Name "name" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($name)) {
        $conditions += [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::NameProperty,
            $name)
    }
    $controlType = [string](Get-OptionalPropertyValue -InputObject $Selector -Name "controlType" -DefaultValue "")
    if (-not [string]::IsNullOrWhiteSpace($controlType)) {
        $conditions += [System.Windows.Automation.PropertyCondition]::new(
            [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
            (Get-ControlType -Name $controlType -Context "selector"))
    }

    if ($conditions.Count -eq 1) {
        return $conditions[0]
    }
    return [System.Windows.Automation.AndCondition]::new([System.Windows.Automation.Condition[]]$conditions)
}

function Wait-Element {
    param(
        [Parameter(Mandatory)][System.Windows.Automation.AutomationElement]$Root,
        [Parameter(Mandatory)]$Selector,
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds,
        [Parameter(Mandatory)][int]$PollingIntervalMilliseconds
    )

    $condition = New-StepCondition -Selector $Selector -ProcessId $ProcessId
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        $element = $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
        if ($element) {
            return $element
        }
        Start-Sleep -Milliseconds $PollingIntervalMilliseconds
    } while ([DateTime]::UtcNow -lt $deadline)

    return $null
}

function Wait-MainWindow {
    param(
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)]$Selector,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds,
        [Parameter(Mandatory)][int]$PollingIntervalMilliseconds
    )

    $condition = New-StepCondition -Selector $Selector -ProcessId $ProcessId
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)
    do {
        $window = [System.Windows.Automation.AutomationElement]::RootElement.FindFirst(
            [System.Windows.Automation.TreeScope]::Children,
            $condition)
        if ($window) {
            return $window
        }
        Start-Sleep -Milliseconds $PollingIntervalMilliseconds
    } while ([DateTime]::UtcNow -lt $deadline)

    return $null
}

function Wait-ExactText {
    param(
        [Parameter(Mandatory)][System.Windows.Automation.AutomationElement]$Root,
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)][string]$ExpectedText,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds,
        [Parameter(Mandatory)][int]$PollingIntervalMilliseconds
    )

    $processCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::ProcessIdProperty,
        $ProcessId)
    $nameCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.AutomationElement]::NameProperty,
        $ExpectedText)
    $valueCondition = [System.Windows.Automation.PropertyCondition]::new(
        [System.Windows.Automation.ValuePattern]::ValueProperty,
        $ExpectedText)
    $textCondition = [System.Windows.Automation.OrCondition]::new(
        [System.Windows.Automation.Condition[]]@($nameCondition, $valueCondition))
    $condition = [System.Windows.Automation.AndCondition]::new(
        [System.Windows.Automation.Condition[]]@($processCondition, $textCondition))
    $deadline = [DateTime]::UtcNow.AddMilliseconds($TimeoutMilliseconds)

    do {
        $element = $Root.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $condition)
        if ($element) {
            return $element
        }
        Start-Sleep -Milliseconds $PollingIntervalMilliseconds
    } while ([DateTime]::UtcNow -lt $deadline)

    return $null
}

function Get-AutomationPropertyValue {
    param(
        [Parameter(Mandatory)][System.Windows.Automation.AutomationElement]$Element,
        [Parameter(Mandatory)][string]$Property
    )

    switch ($Property) {
        "AutomationId" { return $Element.Current.AutomationId }
        "Name" { return $Element.Current.Name }
        "ClassName" { return $Element.Current.ClassName }
        "ControlType" { return ($Element.Current.ControlType.ProgrammaticName -replace '^ControlType\.', '') }
        "HelpText" { return $Element.Current.HelpText }
        "IsEnabled" { return [bool]$Element.Current.IsEnabled }
        "IsOffscreen" { return [bool]$Element.Current.IsOffscreen }
        "Value" {
            $pattern = $Element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            return ([System.Windows.Automation.ValuePattern]$pattern).Current.Value
        }
        "ToggleState" {
            $pattern = $Element.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
            return ([System.Windows.Automation.TogglePattern]$pattern).Current.ToggleState.ToString()
        }
        default { throw "Unsupported property '$Property'." }
    }
}

function Test-ExpectedValue {
    param(
        $Actual,
        $Expected
    )

    if ($Expected -is [bool]) {
        return ($Actual -is [bool]) -and ([bool]$Actual -eq [bool]$Expected)
    }
    if ($Expected -is [int] -or $Expected -is [long] -or $Expected -is [double]) {
        if (-not ($Actual -is [int] -or $Actual -is [long] -or $Actual -is [double])) {
            return $false
        }
        return [double]$Actual -eq [double]$Expected
    }
    if (-not ($Actual -is [string])) {
        return $false
    }
    return [string]::Equals([string]$Actual, [string]$Expected, [StringComparison]::Ordinal)
}

function Resolve-PlaceholdersInString {
    param(
        [Parameter(Mandatory)]
        [AllowEmptyString()]
        [string]$Value,
        [Parameter(Mandatory)][hashtable]$Placeholders
    )

    return [regex]::Replace($Value, '\$\{([^}]+)\}', {
            param($Match)
            $token = $Match.Groups[1].Value
            if (-not $Placeholders.ContainsKey($token)) {
                throw "Unsupported placeholder '$($Match.Value)'."
            }
            return [string]$Placeholders[$token]
        })
}

function Invoke-ScenarioStep {
    param(
        [Parameter(Mandatory)][System.Windows.Automation.AutomationElement]$Window,
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)]$Step,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds,
        [Parameter(Mandatory)][int]$PollingIntervalMilliseconds,
        [Parameter(Mandatory)][hashtable]$Placeholders
    )

    $type = [string]$Step.type
    $stepId = [string](Get-OptionalPropertyValue -InputObject $Step -Name "id" -DefaultValue $type)
    $timeout = $TimeoutMilliseconds
    $stepDeadline = [DateTime]::UtcNow.AddMilliseconds($timeout)

    if ($type -in @("wait-text", "assert-text")) {
        $expectedText = Resolve-PlaceholdersInString -Value ([string]$Step.expectedText) -Placeholders $Placeholders
        $textElement = Wait-ExactText `
            -Root $Window `
            -ProcessId $ProcessId `
            -ExpectedText $expectedText `
            -TimeoutMilliseconds $timeout `
            -PollingIntervalMilliseconds $PollingIntervalMilliseconds
        if (-not $textElement) {
            throw "Step '$stepId' did not find exact text '$expectedText'."
        }
        return
    }

    $element = Wait-Element `
        -Root $Window `
        -Selector $Step.selector `
        -ProcessId $ProcessId `
        -TimeoutMilliseconds $timeout `
        -PollingIntervalMilliseconds $PollingIntervalMilliseconds
    if (-not $element) {
        throw "Step '$stepId' did not find the requested element."
    }

    switch ($type) {
        "wait-element" { return }
        "assert-element" { return }
        "invoke" {
            $pattern = $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern)
            ([System.Windows.Automation.InvokePattern]$pattern).Invoke()
            return
        }
        "toggle" {
            $pattern = $element.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
            ([System.Windows.Automation.TogglePattern]$pattern).Toggle()
            return
        }
        "select" {
            $pattern = $element.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern)
            ([System.Windows.Automation.SelectionItemPattern]$pattern).Select()
            return
        }
        "set-value" {
            $pattern = $element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $value = Resolve-PlaceholdersInString -Value ([string]$Step.value) -Placeholders $Placeholders
            ([System.Windows.Automation.ValuePattern]$pattern).SetValue($value)
            return
        }
        "wait-property" {
            $expected = if ($Step.expected -is [string]) {
                Resolve-PlaceholdersInString -Value ([string]$Step.expected) -Placeholders $Placeholders
            } else {
                $Step.expected
            }
            $actual = Get-AutomationPropertyValue -Element $element -Property ([string]$Step.property)
            if (-not (Test-ExpectedValue -Actual $actual -Expected $expected)) {
                do {
                    $remaining = [math]::Ceiling(($stepDeadline - [DateTime]::UtcNow).TotalMilliseconds)
                    if ($remaining -lt 1) {
                        break
                    }
                    Start-Sleep -Milliseconds ([int][math]::Min($PollingIntervalMilliseconds, $remaining))
                    $actual = Get-AutomationPropertyValue -Element $element -Property ([string]$Step.property)
                } while (-not (Test-ExpectedValue -Actual $actual -Expected $expected) -and [DateTime]::UtcNow -lt $stepDeadline)
            }
            if (-not (Test-ExpectedValue -Actual $actual -Expected $expected)) {
                throw "Step '$stepId' expected $($Step.property) '$expected' but observed '$actual'."
            }
            return
        }
        "assert-property" {
            $expected = if ($Step.expected -is [string]) {
                Resolve-PlaceholdersInString -Value ([string]$Step.expected) -Placeholders $Placeholders
            } else {
                $Step.expected
            }
            $actual = Get-AutomationPropertyValue -Element $element -Property ([string]$Step.property)
            if (-not (Test-ExpectedValue -Actual $actual -Expected $expected)) {
                throw "Step '$stepId' expected $($Step.property) '$expected' but observed '$actual'."
            }
            return
        }
        default {
            throw "Unsupported step type '$type'."
        }
    }
}

function Invoke-StepSequence {
    param(
        [Parameter(Mandatory)][System.Windows.Automation.AutomationElement]$Window,
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)]$Steps,
        [Parameter(Mandatory)][int]$DefaultTimeoutMilliseconds,
        [Parameter(Mandatory)][int]$PollingIntervalMilliseconds,
        [Parameter(Mandatory)][hashtable]$Placeholders
    )

    $sequenceDeadline = [DateTime]::UtcNow.AddMilliseconds($DefaultTimeoutMilliseconds)
    foreach ($step in @($Steps)) {
        $remainingMilliseconds = [math]::Ceiling(($sequenceDeadline - [DateTime]::UtcNow).TotalMilliseconds)
        if ($remainingMilliseconds -lt 1) {
            throw "Step sequence exceeded its $DefaultTimeoutMilliseconds ms budget."
        }
        $configuredStepTimeout = [int](Get-OptionalPropertyValue `
                -InputObject $step `
                -Name "timeoutMilliseconds" `
                -DefaultValue $remainingMilliseconds)
        $effectiveTimeout = [int][math]::Min($configuredStepTimeout, $remainingMilliseconds)
        Invoke-ScenarioStep `
            -Window $Window `
            -ProcessId $ProcessId `
            -Step $step `
            -TimeoutMilliseconds $effectiveTimeout `
            -PollingIntervalMilliseconds $PollingIntervalMilliseconds `
            -Placeholders $Placeholders
    }
}

function New-TrialWorkspace {
    param(
        [Parameter(Mandatory)][string]$OutputPath,
        [Parameter(Mandatory)][int]$Trial,
        [Parameter(Mandatory)]$Fixtures
    )

    $trialRoot = Join-Path $OutputPath ("trials\trial-{0:D3}" -f $Trial)
    if (Test-Path -LiteralPath $trialRoot) {
        throw "Trial workspace already exists and will not be reused: $trialRoot"
    }
    [void][IO.Directory]::CreateDirectory($trialRoot)
    $fixturePaths = @{
        trialRoot = $trialRoot
        profileDir = (Join-Path $trialRoot "profile")
    }
    [void][IO.Directory]::CreateDirectory($fixturePaths.profileDir)
    foreach ($fixture in @($Fixtures)) {
        $destination = Join-Path $trialRoot ("fixtures\$($fixture.Id)")
        [void][IO.Directory]::CreateDirectory((Split-Path -Parent $destination))
        if ($fixture.TemplatePath) {
            Copy-Item -LiteralPath $fixture.TemplatePath -Destination $destination -Recurse
        } else {
            [void][IO.Directory]::CreateDirectory($destination)
        }
        $fixturePaths["fixture:$($fixture.Id)"] = $destination
        if ($fixture.Id -eq "profile") {
            $fixturePaths.profileDir = $destination
        }
    }

    return [pscustomobject]@{
        TrialRoot = $trialRoot
        Placeholders = $fixturePaths
    }
}

function Resolve-ArgumentPlaceholders {
    param(
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]]$Arguments,
        [Parameter(Mandatory)][hashtable]$Placeholders
    )

    $resolved = @()
    foreach ($argument in $Arguments) {
        $resolvedArgument = Resolve-PlaceholdersInString -Value $argument -Placeholders $Placeholders
        $resolved += $resolvedArgument
    }
    return $resolved
}

function Start-ApplicationProcess {
    param(
        [Parameter(Mandatory)][string]$ExecutablePath,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]]$Arguments
    )

    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $ExecutablePath
    $startInfo.WorkingDirectory = $WorkingDirectory
    $startInfo.UseShellExecute = $false
    foreach ($argument in $Arguments) {
        [void]$startInfo.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Failed to start application process."
    }
    return $process
}

function Get-DescendantProcessIds {
    param([Parameter(Mandatory)][int]$RootProcessId)

    $descendantIds = @()
    $frontier = @($RootProcessId)
    $visited = [Collections.Generic.HashSet[int]]::new()
    [void]$visited.Add($RootProcessId)
    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($parentId in $frontier) {
            $children = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentId" | Select-Object -ExpandProperty ProcessId)
            foreach ($childId in $children) {
                if ($visited.Add([int]$childId)) {
                    $next += $childId
                }
            }
        }
        $descendantIds += $next
        $frontier = $next
    }
    return @($descendantIds | Select-Object -Unique)
}

function Stop-OwnedProcess {
    param(
        [System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)][int]$TimeoutMilliseconds
    )

    if (-not $Process) {
        return
    }
    try {
        $rootProcessId = $Process.Id
        $descendantIds = @(Get-DescendantProcessIds -RootProcessId $rootProcessId)

        $Process.Refresh()
        if (-not $Process.HasExited) {
            if ($Process.MainWindowHandle -ne [IntPtr]::Zero) {
                [void]$Process.CloseMainWindow()
                [void]$Process.WaitForExit($TimeoutMilliseconds)
            }
            $Process.Refresh()
            if (-not $Process.HasExited) {
                $descendantIds = @($descendantIds + (Get-DescendantProcessIds -RootProcessId $rootProcessId) | Select-Object -Unique)
                $Process.Kill($true)
                if (-not $Process.WaitForExit($TimeoutMilliseconds)) {
                    throw "Owned process $rootProcessId did not exit within $TimeoutMilliseconds ms after termination."
                }
            }
        }

        $remaining = @($descendantIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($remaining.Count -gt 0) {
            throw "Owned child processes remained after cleanup: $($remaining -join ', ')"
        }
    } finally {
        $Process.Dispose()
    }
}

function Invoke-ProcessInspector {
    param(
        [Parameter(Mandatory)][int]$ProcessId,
        [Parameter(Mandatory)][string]$ExpectedExecutable,
        [Parameter(Mandatory)][string]$ApplicationRoot,
        [Parameter(Mandatory)][string]$ExpectedArchitecture,
        [Parameter(Mandatory)][string]$ReportPath,
        [Parameter(Mandatory)]
        [AllowEmptyCollection()]
        [string[]]$RequiredPackageModule
    )

    $inspector = Join-Path $PSScriptRoot "Inspect-WoaProcess.ps1"
    if (-not (Test-Path -LiteralPath $inspector -PathType Leaf)) {
        throw "Process inspector was not found: $inspector"
    }
    $parameters = @{
        ProcessId = $ProcessId
        ExpectedExecutable = $ExpectedExecutable
        ApplicationRoot = $ApplicationRoot
        ExpectedArchitecture = $ExpectedArchitecture
        ReportPath = $ReportPath
        RequiredPackageModule = $RequiredPackageModule
    }
    & $inspector @parameters | Out-Null
}

function Invoke-PeInspector {
    param(
        [Parameter(Mandatory)][string]$ApplicationRoot,
        [Parameter(Mandatory)][string]$ExpectedArchitecture,
        [Parameter(Mandatory)][string]$ReportPath
    )

    $inspector = Join-Path $PSScriptRoot "Inspect-WoaPe.ps1"
    if (-not (Test-Path -LiteralPath $inspector -PathType Leaf)) {
        throw "PE inspector was not found: $inspector"
    }
    & $inspector `
        -RootPath $ApplicationRoot `
        -ExpectedArchitecture $ExpectedArchitecture `
        -ReportPath $ReportPath | Out-Null
    if ($ExpectedArchitecture -eq "arm64") {
        $report = Get-Content -LiteralPath $ReportPath -Raw | ConvertFrom-Json
        $arm64EcFiles = @($report.Files | Where-Object { $_.Machine -eq "Arm64EC" -and -not $_.AllowedByPattern })
        if ($arm64EcFiles.Count -gt 0) {
            throw "Full arm64 validation rejects Arm64EC package files: $(@($arm64EcFiles.Path) -join ', ')"
        }
    }
}

function Get-Median {
    param([Parameter(Mandatory)][double[]]$Values)

    $sorted = @($Values | Sort-Object)
    if ($sorted.Count -eq 0) {
        throw "Cannot summarize an empty sample set."
    }
    if ($sorted.Count % 2 -eq 1) {
        return $sorted[[int][math]::Floor($sorted.Count / 2)]
    }
    $upper = $sorted.Count / 2
    return ($sorted[$upper - 1] + $sorted[$upper]) / 2
}

function Get-NearestRankPercentile {
    param(
        [Parameter(Mandatory)][double[]]$Values,
        [Parameter(Mandatory)][ValidateRange(0.01, 1.0)][double]$Percentile
    )

    $sorted = @($Values | Sort-Object)
    if ($sorted.Count -eq 0) {
        throw "Cannot summarize an empty sample set."
    }
    $index = [math]::Max(0, [math]::Ceiling($Percentile * $sorted.Count) - 1)
    return $sorted[$index]
}

function Get-MetricSummary {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][double[]]$Values
    )

    return [pscustomobject]@{
        Metric = $Name
        Count = $Values.Count
        Median = [math]::Round((Get-Median -Values $Values), 3)
        P95 = [math]::Round((Get-NearestRankPercentile -Values $Values -Percentile 0.95), 3)
        Minimum = [math]::Round(($Values | Measure-Object -Minimum).Minimum, 3)
        Maximum = [math]::Round(($Values | Measure-Object -Maximum).Maximum, 3)
        Raw = $Values
    }
}

function Write-ScenarioBenchmarkArtifacts {
    param(
        [Parameter(Mandatory)][string]$OutputPath,
        [Parameter(Mandatory)]$Run
    )

    [void][IO.Directory]::CreateDirectory($OutputPath)
    $Run | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath (Join-Path $OutputPath "benchmark-results.json") -Encoding utf8
    @($Run.TrialResults) | Export-Csv -LiteralPath (Join-Path $OutputPath "trial-samples.csv") -NoTypeInformation -Encoding utf8
    @($Run.ScenarioResults) | Export-Csv -LiteralPath (Join-Path $OutputPath "scenario-samples.csv") -NoTypeInformation -Encoding utf8
    @($Run.TrialErrors) | Export-Csv -LiteralPath (Join-Path $OutputPath "trial-errors.csv") -NoTypeInformation -Encoding utf8
    @($Run.Summaries) |
        Select-Object Metric, Count, Median, P95, Minimum, Maximum |
        Export-Csv -LiteralPath (Join-Path $OutputPath "summary.csv") -NoTypeInformation -Encoding utf8
}

if ($env:WOA_SCENARIO_BENCHMARK_DOT_SOURCE_ONLY -eq "1") {
    return
}

if (-not $Smoke -and -not $ValidationOnly -and $Trials -lt 10) {
    throw "At least 10 trials are required for performance benchmark claims. Use -Smoke to run fewer trials without benchmark confidence."
}
foreach ($module in $RequiredPackageModule) {
    if ([IO.Path]::GetFileName($module) -ne $module -or $module -notmatch '\.dll$') {
        throw "RequiredPackageModule entries must be DLL file names: $module"
    }
}

$trustedApplicationRoot = Assert-TrustedRoot -Path $ApplicationRoot -Context "ApplicationRoot"
$trustedEvidenceRoot = Assert-TrustedRoot -Path $EvidenceRoot -Context "EvidenceRoot"
$applicationPrefix = "$trustedApplicationRoot\"
$evidencePrefix = "$trustedEvidenceRoot\"
if ($trustedApplicationRoot.Equals($trustedEvidenceRoot, [StringComparison]::OrdinalIgnoreCase) -or
    $trustedApplicationRoot.StartsWith($evidencePrefix, [StringComparison]::OrdinalIgnoreCase) -or
    $trustedEvidenceRoot.StartsWith($applicationPrefix, [StringComparison]::OrdinalIgnoreCase)) {
    throw "ApplicationRoot and EvidenceRoot must be disjoint directories."
}

$configPath = Assert-DescendantPath -Path $ConfigurationPath -Root $trustedEvidenceRoot -Context "Configuration"
$manifestFilePath = Assert-DescendantPath -Path $ManifestPath -Root $trustedEvidenceRoot -Context "Manifest"
$outputPath = Assert-DescendantPath -Path $OutputDirectory -Root $trustedEvidenceRoot -Context "Output directory"
Assert-FreshOutputDirectory -Path $outputPath
if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Configuration file does not exist: $configPath"
}
$validatedConfigurationSha256 = (Get-FileHash -LiteralPath $configPath -Algorithm SHA256).Hash.ToLowerInvariant()

$manifestInfo = Read-ApprovedManifest `
    -Path $manifestFilePath `
    -ExpectedSha256 $ApprovedManifestSha256 `
    -ApplicationRoot $trustedApplicationRoot
$configuration = Read-ScenarioConfiguration `
    -Path $configPath `
    -ApplicationRoot $trustedApplicationRoot `
    -EvidenceRoot $trustedEvidenceRoot `
    -Manifest $manifestInfo
$runIdentity = New-RunIdentity `
    -Architecture $ArchitectureLabel `
    -TrialCount $Trials `
    -ConfigurationPath $configPath `
    -ManifestSha256 $manifestInfo.Sha256 `
    -ApplicationRoot $trustedApplicationRoot `
    -EvidenceRoot $trustedEvidenceRoot `
    -Configuration $configuration

$confidence = if ($ValidationOnly) {
    "validation-only-not-device-proof"
} elseif ($Smoke) {
    "smoke-only-not-benchmark-confidence"
} else {
    "benchmark"
}

if (-not $ValidationOnly -and [string]::IsNullOrWhiteSpace($ExpectedArchitecture)) {
    throw "ExpectedArchitecture is required for actual execution."
}

[void][IO.Directory]::CreateDirectory($outputPath)
if ($ValidationOnly) {
    $report = [pscustomobject]@{
        SchemaVersion = 1
        GeneratedAtEt = [DateTimeOffset]::Now.ToString("o")
        ValidationOnly = $true
        Confidence = $confidence
        Trials = $Trials
        ConfigurationPath = $configPath
        ManifestPath = $manifestFilePath
        RunIdentity = $runIdentity
        ApplicationExecutable = $configuration.Application.ExecutablePath
        ScenarioCount = $configuration.Scenarios.Count
        FixtureCount = $configuration.Fixtures.Count
        Message = "Validation only. No process was launched and no device or UI scenario pass is claimed."
    }
    $report | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath (Join-Path $outputPath "validation-only.json") -Encoding utf8
    $report | Select-Object ValidationOnly, Confidence, ScenarioCount, FixtureCount, Message | ConvertTo-Json
    return
}

$trialResults = @()
$scenarioResults = @()
$trialErrors = @()

for ($trial = 1; $trial -le $Trials; $trial++) {
    $applicationProcess = $null
    try {
        $workspace = New-TrialWorkspace -OutputPath $outputPath -Trial $trial -Fixtures $configuration.Fixtures
        $resolvedArguments = Resolve-ArgumentPlaceholders `
            -Arguments ([string[]]$configuration.Application.Arguments) `
            -Placeholders $workspace.Placeholders

        [void](Read-ApprovedManifest `
                -Path $manifestFilePath `
                -ExpectedSha256 $ApprovedManifestSha256 `
                -ApplicationRoot $trustedApplicationRoot)
        Assert-ScenarioInputsUnchanged `
            -ConfigurationPath $configPath `
            -ExpectedConfigurationSha256 $validatedConfigurationSha256 `
            -Configuration $configuration
        Invoke-PeInspector `
            -ApplicationRoot $trustedApplicationRoot `
            -ExpectedArchitecture $ExpectedArchitecture `
            -ReportPath (Join-Path $workspace.TrialRoot "pe-inspection.json")

        $coldWatch = [Diagnostics.Stopwatch]::StartNew()
        $applicationProcess = Start-ApplicationProcess `
            -ExecutablePath $configuration.Application.ExecutablePath `
            -WorkingDirectory $configuration.Application.WorkingDirectory `
            -Arguments $resolvedArguments
        $window = Wait-MainWindow `
            -ProcessId $applicationProcess.Id `
            -Selector $configuration.Application.MainWindowSelector `
            -TimeoutMilliseconds $configuration.Defaults.ActionTimeoutMilliseconds `
            -PollingIntervalMilliseconds $configuration.Defaults.PollingIntervalMilliseconds
        if (-not $window) {
            throw "Trial $trial did not expose the configured main window."
        }
        Invoke-StepSequence `
            -Window $window `
            -ProcessId $applicationProcess.Id `
            -Steps $configuration.Application.LaunchReadySteps `
            -DefaultTimeoutMilliseconds $configuration.Defaults.ActionTimeoutMilliseconds `
            -PollingIntervalMilliseconds $configuration.Defaults.PollingIntervalMilliseconds `
            -Placeholders $workspace.Placeholders
        $coldWatch.Stop()

        $requiredModules = [string[]]@($RequiredPackageModule + $configuration.Application.RequiredPackageModules | Select-Object -Unique)
        Invoke-ProcessInspector `
            -ProcessId $applicationProcess.Id `
            -ExpectedExecutable $configuration.Application.ExecutablePath `
            -ApplicationRoot $trustedApplicationRoot `
            -ExpectedArchitecture $ExpectedArchitecture `
            -ReportPath (Join-Path $workspace.TrialRoot "process-inspection.json") `
            -RequiredPackageModule $requiredModules

        foreach ($scenario in @($configuration.Scenarios)) {
            $applicationProcess.Refresh()
            $cpuBefore = $applicationProcess.TotalProcessorTime.TotalMilliseconds
            $scenarioWatch = [Diagnostics.Stopwatch]::StartNew()
            $scenarioError = $null
            try {
                Invoke-StepSequence `
                    -Window $window `
                    -ProcessId $applicationProcess.Id `
                    -Steps $scenario.Steps `
                    -DefaultTimeoutMilliseconds $scenario.TimeoutMilliseconds `
                    -PollingIntervalMilliseconds $configuration.Defaults.PollingIntervalMilliseconds `
                    -Placeholders $workspace.Placeholders
            } catch {
                $scenarioError = $_.Exception.Message
            } finally {
                $scenarioWatch.Stop()
            }
            $applicationProcess.Refresh()
            $scenarioSample = [pscustomobject]@{
                Trial = $trial
                Architecture = $ArchitectureLabel
                ScenarioId = $scenario.Id
                ScenarioName = $scenario.Name
                Passed = [string]::IsNullOrWhiteSpace($scenarioError)
                ElapsedMilliseconds = [math]::Round($scenarioWatch.Elapsed.TotalMilliseconds, 3)
                CpuMilliseconds = [math]::Round(($applicationProcess.TotalProcessorTime.TotalMilliseconds - $cpuBefore), 3)
                Error = $scenarioError
                CompletedAtEt = [DateTimeOffset]::Now.ToString("o")
            }
            $scenarioResults += $scenarioSample
            if ($scenarioError) {
                throw "Trial $trial scenario '$($scenario.Id)' failed: $scenarioError"
            }
        }

        $applicationProcess.Refresh()
        $trialResults += [pscustomobject]@{
            Trial = $trial
            Architecture = $ArchitectureLabel
            ProcessId = $applicationProcess.Id
            ProcessColdLaunchMilliseconds = [math]::Round($coldWatch.Elapsed.TotalMilliseconds, 3)
            WorkingSetBytes = $applicationProcess.WorkingSet64
            PrivateMemoryBytes = $applicationProcess.PrivateMemorySize64
            CpuMilliseconds = [math]::Round($applicationProcess.TotalProcessorTime.TotalMilliseconds, 3)
            Arguments = ($resolvedArguments | ConvertTo-Json -Compress)
            TrialRoot = $workspace.TrialRoot
            Error = $null
        }
        Write-Host "Completed trial $trial of $Trials"
    } catch {
        $trialErrors += [pscustomobject]@{
            Trial = $trial
            Architecture = $ArchitectureLabel
            Error = $_.Exception.Message
            CapturedAtEt = [DateTimeOffset]::Now.ToString("o")
        }
        $partialRun = [pscustomobject]@{
            SchemaVersion = 1
            CapturedAtEt = [DateTimeOffset]::Now.ToString("o")
            Architecture = $ArchitectureLabel
            Trials = $Trials
            CompletedTrials = $trialResults.Count
            Confidence = $confidence
            RunIdentity = $runIdentity
            PercentileMethod = "nearest-rank"
            TrialResults = $trialResults
            ScenarioResults = $scenarioResults
            TrialErrors = $trialErrors
            Summaries = @()
        }
        Write-ScenarioBenchmarkArtifacts -OutputPath $outputPath -Run $partialRun
        throw "Benchmark failed during trial $trial. Evidence was retained under $outputPath. $($_.Exception.Message)"
    } finally {
        Stop-OwnedProcess -Process $applicationProcess -TimeoutMilliseconds $configuration.Defaults.ProcessExitTimeoutMilliseconds
    }
}

$summaries = @()
$summaries += Get-MetricSummary `
    -Name "process-cold-launch-ms" `
    -Values ([double[]]@($trialResults.ProcessColdLaunchMilliseconds))
$summaries += Get-MetricSummary `
    -Name "working-set-bytes" `
    -Values ([double[]]@($trialResults.WorkingSetBytes))
$summaries += Get-MetricSummary `
    -Name "private-memory-bytes" `
    -Values ([double[]]@($trialResults.PrivateMemoryBytes))
$summaries += Get-MetricSummary `
    -Name "process-cpu-ms" `
    -Values ([double[]]@($trialResults.CpuMilliseconds))
foreach ($scenario in @($configuration.Scenarios)) {
    $samples = @($scenarioResults | Where-Object { $_.ScenarioId -eq $scenario.Id -and $_.Passed })
    $summaries += Get-MetricSummary `
        -Name "scenario-$($scenario.Id)-ms" `
        -Values ([double[]]@($samples.ElapsedMilliseconds))
    $summaries += Get-MetricSummary `
        -Name "scenario-$($scenario.Id)-cpu-ms" `
        -Values ([double[]]@($samples.CpuMilliseconds))
}

$run = [pscustomobject]@{
    SchemaVersion = 1
    CapturedAtEt = [DateTimeOffset]::Now.ToString("o")
    Architecture = $ArchitectureLabel
    Trials = $Trials
    CompletedTrials = $trialResults.Count
    Confidence = $confidence
    ConfigurationPath = $configPath
    ManifestPath = $manifestFilePath
    RunIdentity = $runIdentity
    PercentileMethod = "nearest-rank"
    TrialResults = $trialResults
    ScenarioResults = $scenarioResults
    TrialErrors = $trialErrors
    Summaries = $summaries
}
Write-ScenarioBenchmarkArtifacts -OutputPath $outputPath -Run $run

if ($trialErrors.Count -gt 0) {
    throw "$($trialErrors.Count) trial errors were found. Evidence was retained under $outputPath."
}

$summaries | Select-Object Metric, Count, Median, P95, Minimum, Maximum | Format-Table -AutoSize
