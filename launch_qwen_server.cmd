@echo off
setlocal
set "LLAMA_DIR=D:\AI\llama.cpp"
set "MODEL=%LLAMA_DIR%\models\Huihui-Qwen3.8-27B-abliterated-UD-DW-IQ3_S.gguf"
if not exist "%LLAMA_DIR%\llama-server.exe" goto missing
if not exist "%MODEL%" goto missing
cd /d "%LLAMA_DIR%"
llama-server.exe -m "%MODEL%" --alias Huihui-Qwen3.8-27B-abliterated-UD-DW-IQ3_S -ngl 99 -c 32768 -np 1 -fa on --host 127.0.0.1 --port 8080 --chat-template-kwargs "{\"enable_thinking\":false}"
pause
exit /b
:missing
echo llama-server.exe or model file not found. Check LLAMA_DIR and MODEL.
pause
exit /b 1
