@echo off
setlocal
set "LLAMA_DIR=D:\AI\llama.cpp"
set "MODEL=%LLAMA_DIR%\models\gemma-4-12b-it-UD-Q5_K_XL.gguf"
if not exist "%LLAMA_DIR%\llama-server.exe" goto missing
if not exist "%MODEL%" goto missing
cd /d "%LLAMA_DIR%"
echo Starting Gemma 4 12B at http://127.0.0.1:8081
echo Profile: config\llm-gemma-local.yaml in the Funscripter_ASMR project.
echo Stop Qwen first to free GPU memory.
"%LLAMA_DIR%\llama-server.exe" -m "%MODEL%" --alias gemma-4-12b-it-UD-Q5_K_XL -ngl 99 -c 32768 -np 1 -fa on --host 127.0.0.1 --port 8081 --reasoning off --chat-template-kwargs "{\"enable_thinking\":false}"
set "SERVER_EXIT=%ERRORLEVEL%"
pause
exit /b %SERVER_EXIT%
:missing
echo llama-server.exe or model file not found. Check LLAMA_DIR and MODEL.
pause
exit /b 1
