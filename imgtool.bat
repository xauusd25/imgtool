@echo off
setlocal enabledelayedexpansion
title imgtool - .img / super.img toolkit

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH.
    echo Please install Python 3.8+ from https://www.python.org/downloads/
    echo and make sure to check "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

:menu
cls
echo ===============================================
echo   imgtool - Android .img / super.img toolkit
echo ===============================================
echo   1. Unpack .img      (sparse -^> raw)
echo   2. Repack .img      (raw -^> sparse)
echo   3. Unpack super.img (extract all partitions)
echo   4. Repack super.img (write partitions back in)
echo   5. List partitions in super.img
echo   6. Split super.img into parts
echo   7. Merge split parts into super.img
echo   0. Exit
echo ===============================================
set /p choice=Choose an option:

if "%choice%"=="1" goto img_unpack
if "%choice%"=="2" goto img_repack
if "%choice%"=="3" goto super_unpack
if "%choice%"=="4" goto super_repack
if "%choice%"=="5" goto super_list
if "%choice%"=="6" goto super_split
if "%choice%"=="7" goto super_merge
if "%choice%"=="0" exit /b 0
goto menu

:img_unpack
set /p src=Path to sparse .img file:
set /p dst=Output raw .img path:
python -m imgtool img-unpack "%src%" "%dst%"
pause
goto menu

:img_repack
set /p src=Path to raw .img file:
set /p dst=Output sparse .img path:
python -m imgtool img-repack "%src%" "%dst%"
pause
goto menu

:super_unpack
set /p src=Path to super.img:
set /p dst=Output folder for extracted partitions:
python -m imgtool super-unpack "%src%" "%dst%"
pause
goto menu

:super_repack
set /p src=Path to original super.img:
set /p pdir=Folder containing replacement ^<partition^>.img files:
set /p dst=Output path for new super.img:
python -m imgtool super-repack "%src%" "%pdir%" "%dst%"
pause
goto menu

:super_list
set /p src=Path to super.img:
python -m imgtool super-list "%src%"
pause
goto menu

:super_split
set /p src=Path to super.img:
set /p dst=Output folder for parts:
set /p mb=Chunk size in MB (blank = 1024):
if "%mb%"=="" set mb=1024
python -m imgtool super-split "%src%" "%dst%" %mb%
pause
goto menu

:super_merge
set /p src=Path to first part (e.g. super.img.000):
set /p dst=Output merged super.img path:
python -m imgtool super-merge "%src%" "%dst%"
pause
goto menu
