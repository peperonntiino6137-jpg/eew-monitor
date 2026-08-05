# Windows 起動時に EEW モニタを自動常駐させる (スタートアップにショートカット作成)
$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent $PSScriptRoot
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "EEW Monitor.lnk"

# pythonw.exe (コンソールなし) を探す
$python = (Get-Command python).Source
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Error "pythonw.exe が見つかりません: $pythonw"
}

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($shortcutPath)
$sc.TargetPath = $pythonw
$sc.Arguments = "-m eew.main --headless"
$sc.WorkingDirectory = $projectDir
$sc.Description = "緊急地震速報マルチソース受信モニタ (常駐)"
$sc.Save()

Write-Host "登録完了: $shortcutPath"
Write-Host "  実行内容: $pythonw -m eew.main --headless"
Write-Host "  作業フォルダ: $projectDir"
Write-Host "  ログ: $projectDir\logs\eew.log"
Write-Host "解除するには uninstall_startup.ps1 を実行してください。"
