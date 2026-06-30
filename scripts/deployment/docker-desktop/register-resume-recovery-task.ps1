$ErrorActionPreference = 'Stop'

$taskName = 'Docker-WSL-ResumeRecovery'
$scriptPath = 'D:\src\trader\scripts\deployment\docker-desktop\fix-sleep-recovery.ps1'

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Recovers Docker Desktop / WSL2 when they hang after the laptop resumes from Modern Standby sleep.</Description>
  </RegistrationInfo>
  <Triggers>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Delay>PT15S</Delay>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="System"&gt;&lt;Select Path="System"&gt;*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
    </EventTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>S-1-5-18</UserId>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>false</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT5M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>powershell.exe</Command>
      <Arguments>-NoProfile -ExecutionPolicy Bypass -File "$scriptPath"</Arguments>
    </Exec>
  </Actions>
</Task>
"@

$tmpXmlPath = Join-Path $env:TEMP 'docker-wsl-resume-recovery-task.xml'
[System.IO.File]::WriteAllText($tmpXmlPath, $xml, [System.Text.Encoding]::Unicode)

Register-ScheduledTask -TaskName $taskName -Xml (Get-Content -Raw -Encoding Unicode $tmpXmlPath) -Force | Out-Null
Remove-Item $tmpXmlPath -Force

Write-Host "Registered scheduled task '$taskName'."
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State | Format-Table -AutoSize | Out-String
