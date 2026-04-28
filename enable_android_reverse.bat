@echo off
setlocal

adb devices
adb reverse tcp:8000 tcp:8000
adb reverse --list

endlocal
