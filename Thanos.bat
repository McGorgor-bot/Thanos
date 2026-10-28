@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title Thanos 2.64.41 - uruchamianie

REM Najpierw uzyj python z PATH; w razie braku sprawdz launcher py.
set "PYTHON_CMD="
where python >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=python"
if not defined PYTHON_CMD (
  where py >nul 2>nul
  if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if not defined PYTHON_CMD (
  echo BLAD: Nie znaleziono Pythona.
  echo Zainstaluj Python 3.11 lub nowszy i zaznacz opcje Add Python to PATH.
  pause
  exit /b 1
)

echo [1/4] Wybrany interpreter:
%PYTHON_CMD% --version
if errorlevel 1 goto PYTHON_ERROR

echo [2/4] Sprawdzanie pip...
%PYTHON_CMD% -m pip --version
if errorlevel 1 (
  echo BLAD: pip nie jest dostepny dla wybranego Pythona.
  pause
  exit /b 1
)

echo [3/4] Instalowanie lub aktualizowanie wymaganych bibliotek...
%PYTHON_CMD% -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 (
  echo BLAD: Instalacja zaleznosci nie powiodla sie.
  echo Przewin wyzej, aby zobaczyc komunikat pip. Thanos nie zostal uruchomiony.
  pause
  exit /b 1
)

echo [4/4] Uruchamianie Thanos...
echo Jesli aplikacja sie nie uruchomi, ponizszy komunikat bledu pozostanie widoczny.
%PYTHON_CMD% -m streamlit run "%~dp0app.py" --server.headless false
set "RUN_RC=%ERRORLEVEL%"
echo.
echo Thanos zakonczyl dzialanie z kodem: %RUN_RC%
pause
exit /b %RUN_RC%

:PYTHON_ERROR
echo BLAD: Wybrany Python nie uruchamia sie prawidlowo.
pause
exit /b 1
