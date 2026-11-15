@echo off
setlocal
set "LLAMA_DIR=D:\AI\llama.cpp"
set "MODEL=%LLAMA_DIR%\models\Huihui-Qwen3.5-9B-abliterated.Q5_K_M_2.gguf"
if not exist "%LLAMA_DIR%\llama-server.exe" goto missing
if not exist "%MODEL%" goto missing
cd /d "%LLAMA_DIR%"
echo Starting Huihui Qwen3.5 9B Q5_K_M at http://127.0.0.1:8082
echo Profile: config\llm-qwen35-9b-local.yaml in the Funscripter_ASMR project.
echo Stop other GPU model servers first to free GPU memory.
"%LLAMA_DIR%\llama-server.exe" -m "%MODEL%" --alias Huihui-Qwen3.5-9B-abliterated-Q5_K_M -ngl 99 -c 32768 -np 1 -fa on --host 127.0.0.1 --port 8082 --reasoning off --chat-template-kwargs "{\"enable_thinking\":false}"
set "SERVER_EXIT=%ERRORLEVEL%"
pause
exit /b %SERVER_EXIT%
:missing
echo llama-server.exe or model file not found. Check LLAMA_DIR and MODEL.
pause
exit /b 1
