# スタートアップ登録を解除する
$startupDir = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupDir "EEW Monitor.lnk"

if (Test-Path $shortcutPath) {
    Remove-Item $shortcutPath -Force
    Write-Host "解除しました: $shortcutPath"
} else {
    Write-Host "登録されていません: $shortcutPath"
}
