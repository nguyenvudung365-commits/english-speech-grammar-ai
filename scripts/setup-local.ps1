$ErrorActionPreference = 'Stop'
if (Test-Path '.env') { throw '.env exists; preserve it and configure manually.' }
$bytes = New-Object byte[] 32
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$rng.Dispose()
$password = [BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()
$content = @"
POSTGRES_PASSWORD=$password
DATABASE_URL=postgresql+psycopg://speech:$password@127.0.0.1:55439/speech
ORIGIN=http://127.0.0.1:8000
STORAGE_ROOT=data/private-audio
COOKIE_SECURE=false
"@
[System.IO.File]::WriteAllText((Join-Path (Get-Location) '.env'), $content)
Write-Output 'Created local .env without printing credentials. Keep it private.'
