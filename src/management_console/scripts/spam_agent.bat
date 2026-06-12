@echo off
setlocal enabledelayedexpansion
SET "URL=http://127.0.0.1:8000/api/v1/agents/register"
SET "START=%1"
SET "END=%2"
SET "AUTH_TOKEN=%3"

:: Generate agents using the API
FOR /L %%i IN (%START%,1,%END%) DO (
  curl -X POST "%URL%" ^
    -H "accept: application/json" ^
    -H "Authorization: Bearer %AUTH_TOKEN%" ^
    -H "Content-Type: application/json" ^
    -d "{\"hostname\": \"win-0%%i\", \"status\": \"active\"}"

  timeout /t 1 >nul
)

endlocal