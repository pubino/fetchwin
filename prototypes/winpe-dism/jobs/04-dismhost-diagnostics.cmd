@echo off
echo === fetchwin WinPE worker: dismhost diagnostics (put image.wim and package.cab in the job) ===
>X:\dp.txt echo select disk 1
>>X:\dp.txt echo clean
>>X:\dp.txt echo convert gpt
>>X:\dp.txt echo create partition primary
>>X:\dp.txt echo format fs=ntfs quick label=FWSCR
>>X:\dp.txt echo assign letter=S
diskpart /s X:\dp.txt > nul
mkdir S:\mnt S:\scratch
copy /y %JOB%\image.wim S:\image.wim > nul
dism /English /Mount-Image /ImageFile:S:\image.wim /Index:1 /MountDir:S:\mnt /ScratchDir:S:\scratch
echo [%time%] add package.cab with LogLevel 4
dism /English /LogLevel:4 /LogPath:%OUT%\dism4.log /Image:S:\mnt /Add-Package /PackagePath:%JOB%\package.cab /ScratchDir:S:\scratch
echo exit=%errorlevel%
echo ---- scratch dirs
dir /s /b S:\scratch | more
echo ---- registry: dismcoreps
reg query HKLM\Software\Classes\Interface /s /f dismcoreps > %OUT%\reg_iface.txt 2>&1
reg query HKLM\Software\Classes\CLSID /s /f dism > %OUT%\reg_clsid.txt 2>&1
reg query HKLM\Software\Classes\AppID /s /f dism > %OUT%\reg_appid.txt 2>&1
dism /English /Unmount-Image /MountDir:S:\mnt /Discard
exit /b 0
