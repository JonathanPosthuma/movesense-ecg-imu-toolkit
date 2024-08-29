# Sample: simeple_ecglogger_app #

This firmware is created to simplify collecting ECG data from not "tech savvy" users. It demonstrates the use of DataLogger and Logbook services in the sensor firmware.

**IMPORTANT: This is a sample meant to demonstrate DataLogger operation on the sensor firmware. If you want to use this sample for more important data gathering, please test it thoroughly!**

## Basic operation of the firmware ##

* Sensor wakeup when battery inserted or stud contact detected
* If logbook is empty, the firmware starts logging ECG @ 200 Hz
* Firmware stops datalogger when the data memory is full
* otherwise works as normal hr_wakeup_app

To fetch the recorded data from the sensor, use the [DataLoggerSample for Android](https://bitbucket.org/movesense/movesense-mobile-lib/src/master/android/samples/DataLoggerSample/) (apk-installer can be found [int the Downloads section](https://bitbucket.org/movesense/movesense-mobile-lib/downloads/) ).