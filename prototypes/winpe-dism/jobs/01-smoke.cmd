@echo off
echo === fetchwin WinPE worker milestone 1 ===
ver
echo JOB=%JOB% OUT=%OUT% PROCESSOR_ARCHITECTURE=%PROCESSOR_ARCHITECTURE%
dism /English /Get-WimInfo /WimFile:%JOB%\..\sources\boot.wim
echo list disk> X:\dp.txt
echo list volume>> X:\dp.txt
diskpart /s X:\dp.txt
