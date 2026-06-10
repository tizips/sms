# SMS Gateway Notes

This directory is the working root for the SMS gateway VM.

## Current Layout

- `conf/`: service and mail configuration
- `data/`: SQLite files and schemas
- `bin/`: helper scripts and receive hooks
- `spool/`: Gammu file backend queues
- `logs/`: SMS forwarding logs

Important files:

- `/htdocs/sms/conf/gammu-smsdrc`
- `/htdocs/sms/data/sms.sqlite`
- `/htdocs/sms/bin/sms-received-hook`
- `/htdocs/sms/logs/sms-forward.log`

## Cold Boot Recovery Notes

After the PVE host was manually cold booted, the SMS VM was first stopped so
PVE could exclusively access the Quectel modem.

The PVE layer was checked first:

- Confirmed VM `131` (`SMS`) was stopped.
- Confirmed the Quectel EM120R-GL module was visible on PVE:
  - PCI device: `07:00.0`
  - Device ID: `1eac:1001`
  - Driver: `mhi-pci-generic`
  - Device nodes:
    - `/dev/wwan0at0`
    - `/dev/wwan0mbim0`
    - `/dev/wwan0qcdm0`
- Confirmed `wwan0` stayed `DOWN`.
- Confirmed `ModemManager` was disabled/inactive.
- Ran FCC unlock:

```sh
/usr/share/ModemManager/fcc-unlock.available.d/1eac:1001 /dummy/path wwan0mbim0
```

After FCC unlock, MBIM still showed radio off, so the AT port was checked on
PVE directly through `/dev/wwan0at0`. The modem reported:

```text
AT+CFUN? -> +CFUN: 4
AT+CPIN? -> +CPIN: READY
```

`CFUN=4` means the modem was awake and the SIM was ready, but radio was still
off. Radio was then enabled with:

```text
AT+CFUN=1
```

After that, PVE checks showed:

```text
Software radio state: on
Register state: home
Provider name: 中国电信
Available data classes: lte
wwan0: DOWN
```

The PVE unlock service was enabled so it runs after future host boots:

```sh
systemctl enable quectel-radio-unlock.service
```

Only after the PVE modem state was healthy, the SMS VM was started again and
validated:

```text
VM 131: running
gammu-smsd: active
IMEI: 015930000049750
IMSI: 460115033554699
NetworkSignal: 90
```

## Recovery Checklist

Use this order when the SMS module appears offline.

1. Stop the SMS VM first:

```sh
qm stop 131
```

2. Confirm PVE sees the modem:

```sh
lspci -nnk | grep -A4 -i -E 'quectel|1eac|mhi'
ls -l /dev/wwan*
ip link show wwan0
```

3. Run FCC unlock:

```sh
/usr/share/ModemManager/fcc-unlock.available.d/1eac:1001 /dummy/path wwan0mbim0
```

4. Check MBIM state:

```sh
mbimcli -p -d /dev/wwan0mbim0 --query-radio-state
mbimcli -p -d /dev/wwan0mbim0 --query-registration-state
mbimcli -p -d /dev/wwan0mbim0 --query-signal-state
```

5. If MBIM still says software radio is off, check AT state on PVE:

```text
AT+CFUN?
AT+CPIN?
AT+CEREG?
AT+CSQ
```

If `AT+CFUN?` returns `+CFUN: 4`, enable radio:

```text
AT+CFUN=1
```

6. Confirm PVE is healthy before starting the VM:

```text
Software radio state: on
Register state: home
Signal is not 99
wwan0 remains DOWN
```

7. Start the SMS VM:

```sh
qm start 131
```

8. Confirm VM service:

```sh
systemctl is-active gammu-smsd
gammu-smsd-monitor -c /htdocs/sms/conf/gammu-smsdrc -n 1
```

## Important Notes

- This SIM is for SMS only. Do not configure APN, dial-up, DHCP, or data
  networking for the SIM.
- Keep PVE `wwan0` down. It is normal and intentional.
- Use only the AT/SMS path for SMS work.
- Do not reset or rebind the PCI modem while the SMS VM is running. Stop VM
  `131` first.
- If kernel logs show `firmware crashed`, `D3cold`, `reset failed`, or
  `Unable to change power state from D3cold to D0`, soft recovery may fail.
  A full PVE host cold boot may be required.
- If `/dev/wwan0at0` disappears, do not start the SMS VM. Recover the PVE
  modem layer first.
- If `gammu-smsd-monitor` in the VM shows empty IMEI/IMSI or signal `0`, check
  PVE first before changing VM configuration.
