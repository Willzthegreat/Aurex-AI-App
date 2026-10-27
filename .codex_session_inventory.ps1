$all = @(Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name,ExecutablePath,CreationDate)
$all | Where-Object {
  $_.Name -match '^(terminal64|metatester64|python|pythonw)\.exe$' -or
  ($_.ExecutablePath -and $_.ExecutablePath -match '(?i)Aurex|MetaTrader|worker-data')
} | Sort-Object ProcessId | Format-Table -AutoSize
Write-Output 'LISTENERS'
netstat -ano | findstr 57762
netstat -ano | findstr 57763
netstat -ano | findstr 8010
