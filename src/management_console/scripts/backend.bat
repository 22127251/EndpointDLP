@echo off
SET BACKEND_DIR=C:\Users\Huy\Documents\EndpointDLP\src\management_console\server
SET VENV_NAME=dlpserver

echo Starting Backend...
call conda activate %VENV_NAME% 
cd /d %BACKEND_DIR% && uvicorn main:app --reload



echo Done! Backend services are running.
pause