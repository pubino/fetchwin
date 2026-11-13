@echo off
echo === fetchwin WinPE worker milestone 2b: offline LCU servicing ===
ver
echo [%time%] preparing NTFS scratch disk (disk 1)
>X:\dp.txt echo select disk 1
>>X:\dp.txt echo clean
>>X:\dp.txt echo convert gpt
>>X:\dp.txt echo create partition primary
>>X:\dp.txt echo format fs=ntfs quick label=FWSCR
>>X:\dp.txt echo assign letter=S
diskpart /s X:\dp.txt
if errorlevel 1 exit /b 10
mkdir S:\mnt S:\scratch
echo [%time%] copying pro.wim to scratch
copy /y %JOB%\pro.wim S:\pro.wim > nul
if errorlevel 1 exit /b 11
echo [%time%] mount
dism /English /Mount-Image /ImageFile:S:\pro.wim /Index:1 /MountDir:S:\mnt /ScratchDir:S:\scratch
if errorlevel 1 exit /b 12

echo [%time%] STEP A: servicing stack update (SSU cab)
dism /English /Image:S:\mnt /Add-Package /PackagePath:%JOB%\SSU-26100.9971-arm64.cab /ScratchDir:S:\scratch
echo STEP A exit=%errorlevel%

echo [%time%] STEP B: LCU as MSU
dism /English /Image:S:\mnt /Add-Package /PackagePath:%JOB%\Windows11.0-KB5132061-arm64.msu /ScratchDir:S:\scratch
set RC=%errorlevel%
echo STEP B exit=%RC%
if "%RC%"=="0" goto commit
if "%RC%"=="3010" goto commit

echo [%time%] STEP C: LCU as PSFX wim (+psf alongside)
dism /English /Image:S:\mnt /Add-Package /PackagePath:%JOB%\Windows11.0-KB5132061-arm64.wim /ScratchDir:S:\scratch
set RC=%errorlevel%
echo STEP C exit=%RC%
if "%RC%"=="0" goto commit
if "%RC%"=="3010" goto commit

echo [%time%] all strategies failed, discarding
dism /English /Unmount-Image /MountDir:S:\mnt /Discard
exit /b 20

:commit
echo [%time%] package list
dism /English /Image:S:\mnt /Get-Packages /Format:Table > %OUT%\packages.txt
echo [%time%] commit
dism /English /Unmount-Image /MountDir:S:\mnt /Commit /ScratchDir:S:\scratch
if errorlevel 1 exit /b 14
echo [%time%] split to FAT32 output
dism /English /Split-Image /ImageFile:S:\pro.wim /SWMFile:%OUT%\pro.swm /FileSize:3800
if errorlevel 1 exit /b 15
echo [%time%] done
dir %OUT%
exit /b 0
