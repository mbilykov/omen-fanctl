# HP WMI temperature sensor probe

This is a read-only sensor provider for board `8D87`. It evaluates the same
BIOS WMI query used by HP Gaming Hub and exposes four readings at
`/proc/hp_wmi_sensors`:

| Index | HP software name |
|---:|---|
| 0 | IR |
| 1 | Ambient |
| 2 | PCH |
| 3 | VR |

Gaming Hub logs from this `8D87` say `ChangeIrSensorToBoard = False`, so its
IR curve uses index `0` on this machine. The standalone daemon consumes that
row as its canonical IR source. The WMI interface does not identify the
physical sensor-chip model.

Build, load, and verify the provider with the project script:

```bash
cd fan-control-daemon-research/src
sudo ./probe_hp_ir_sensor.sh --load-only
```

A valid reading contains a row such as `0 IR 39`. In `--load-only` mode the
script deliberately leaves the module loaded for the daemon. It can be unloaded
after the daemon stops with `sudo rmmod hp_wmi_sensor_probe`.

For a bounded comparison with the Linux thermal zones, install the headers
matching the running Arch kernel and run:

```bash
sudo pacman -S linux-headers
cd fan-control-daemon-research/src
sudo ./probe_hp_ir_sensor.sh
```

Without `--load-only`, the script logs the HP readings and both Linux `acpitz`
zones for three minutes. A module loaded by the script is then unloaded; an
already-loaded module is preserved. Logs default to the project `logs/`
directory. Override the defaults with `DURATION`, `INTERVAL`, or `OUTPUT`, for
example `sudo DURATION=300 INTERVAL=5 ./probe_hp_ir_sensor.sh`.

The module sends only command group `0x20008`, query `0x23`, with indices 0–3.
It does not send fan-control or power-profile commands.
