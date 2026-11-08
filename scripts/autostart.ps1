# Start FLINS when you log in to Windows (or stop doing that).
#   .\scripts\autostart.ps1            # on
#   .\scripts\autostart.ps1 -Voice     # on, with push-to-talk / wake word
#   .\scripts\autostart.ps1 -Off       # off
param([switch]$Off, [switch]$Voice)
Set-Location (Split-Path -Parent $PSScriptRoot)
if ($Off) { uv run flins autostart off }
elseif ($Voice) { uv run flins autostart on --voice }
else { uv run flins autostart on }
