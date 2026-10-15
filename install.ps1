# Installs the telegram skill, hooks and device config for Claude Code on this PC.
# Example: .\install.ps1 -Relay https://example.com/tg -Name laptop -HostLabel Laptop
param(
    [Parameter(Mandatory = $true)][string]$Relay,
    [string]$Name = $env:COMPUTERNAME.ToLower(),
    [string]$HostLabel = $env:COMPUTERNAME
)
py -3 "$PSScriptRoot\client\tg.py" install --relay $Relay --name $Name --host-label $HostLabel
