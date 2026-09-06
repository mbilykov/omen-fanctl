savedcmd_hp_wmi_sensor_probe.mod := printf '%s\n'   hp_wmi_sensor_probe.o | awk '!x[$$0]++ { print("./"$$0) }' > hp_wmi_sensor_probe.mod
