@echo off
echo ===================================================
echo A ENVIAR ATUALIZACOES PARA O GITHUB E RENDER
echo ===================================================
git add .
set /p mensagem="Digite a mensagem do commit (ex: Correcao de chaves e contagem): "
if "%mensagem%"=="" set mensagem=Atualizacao automatica do sistema
git commit -m "%mensagem%"
git push origin main
echo ===================================================
echo PROCESSO CONCLUIDO!
echo ===================================================
pause