@echo off
title DLP Project Starter
SET BACKEND_DIR=C:\Users\Huy\Documents\EndpointDLP\src\management_console\server
SET VENV_NAME=dlpserver
SET FRONTEND_DIR=C:\Users\Huy\Documents\EndpointDLP\src\management_console\frontend

wt -w 0 nt -d "%BACKEND_DIR%" cmd /k "call conda activate %VENV_NAME% && uvicorn main:app --reload" ; sp -V -d "%FRONTEND_DIR%" cmd /k "npm run dev"

echo Done! All services are running.
pause