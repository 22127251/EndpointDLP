@echo off
setlocal enabledelayedexpansion

set URL=http://127.0.0.1:8000/api/v1/policies/

set channels=usb browser clipboard email
set actions=block alert allow

set "START=%1"
set "END=%2"
set "AUTH_TOKEN=%3"

FOR /L %%i IN (%START%,1,%END%) DO (

  :: random channel (0-3)
  set /a c_idx=!random! %% 4
  set idx=0
  for %%c in (%channels%) do (
    if !idx! == !c_idx! set channel=%%c
    set /a idx+=1
  )

  :: random action (0-2)
  set /a a_idx=!random! %% 3
  set idx=0
  for %%a in (%actions%) do (
    if !idx! == !a_idx! set action=%%a
    set /a idx+=1
  )

  echo Creating policy %%i with !channel! - !action!

  curl -X POST "%URL%" ^
    -H "accept: application/json" ^
    -H "Authorization: Bearer %AUTH_TOKEN%" ^
    -H "Content-Type: application/json" ^
    -d "{\"name\":\"Policy %%i\",\"description\":\"Auto generated policy %%i\",\"rule_type\":\"regex\",\"rule\":{\"pattern\":\"\\\\b\\\\d{16}\\\\b\"},\"action\":\"!action!\",\"channel\":\"!channel!\",\"is_active\":true}"

  timeout /t 1 >nul
)

endlocal