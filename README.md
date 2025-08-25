# Chinabroadcast RDS Encoder

http://chinabroadcast.aliexpress.com/

**RDS control (tkinter + Flask) with CT and PC-time toggle**

A Python tool for sending RDS (Radio Data System) packets to an FM transmitter over a serial interface. It provides a Tkinter GUI for local control, a small Flask-based web UI for remote configuration and quick actions, and an optional Clock Time (CT, RDS Group 4A) sender which can use the PC clock or a manual timestamp. Only for GD-2015 FM Transmitter.

---

## Main features

* Send PS (Program Service) and RT (RadioText) RDS packets repeatedly to a serial-connected transmitter.
* Optional CT (Clock Time) Group 4A generation and send:

  * Send an initial CT frame on start.
  * Optionally spawn a background thread that sends CT exactly at the minute boundary using the PC local minute rollover.
  * CT can use PC time or a manually specified timestamp (format `YYYY-MM-DD HH:MM` or just `HH:MM`).
  * Configurable local time offset in minutes for CT encoding.
* Local GUI (Tkinter) with serial port scanning, PS/RT editing, PTY selection and debug console.
* Lightweight Flask web UI to view/change configuration, start/stop sending and read status (JSON endpoint).
* Save/load configuration in `config.json`.

---

## Requirements

* Python 3.8 or newer (tested up to Python 3.11). Use the system Python or a virtual environment.

Python packages (install via `pip`):

* `pyserial` — serial port access (reads/writes to the transmitter).
* `Flask` — minimal web interface.

These can be installed with:

```bash
python -m pip install pyserial Flask
```

Notes:

* `tkinter` is used for the GUI. On many Linux distributions you must install the `tk` or `python3-tk` OS package (e.g. `sudo apt install python3-tk`).
* No external web hosting is required; Flask runs locally on the machine and is started by the script.

---

## Files

* `rdsencoder.py` — main script (contains Tkinter GUI, RDS worker, Flask app and CLI entrypoint).
* `config.json` — configuration file created/updated by the GUI or web interface.

---

## How it works (high level)

1. The GUI scans available serial ports and allows the user to open a port.
2. User composes the PS (16 chars) and RT (up to 64 chars), chooses a PI (4 hex digits), Program Type (PTY) and RT address (A/B).
3. When `Send` is pressed, a background `RDSWorker` thread is created which:

   * Prepares PS and RT 2-byte GB2312-encoded slices.
   * Sends PS (4 packets) and RT (16 packets) to the serial device in a loop.
   * Optionally sends an initial CT frame and starts a CT thread which will send CT at each minute rollover.
4. The Flask app can be used to remotely apply configuration, start or stop the sending loop and query status.

---

## Clock Time (CT) behavior and details

* CT is implemented as RDS Group 4A (the script sets Group Type = 4, version A).
* The CT payload encodes Modified Julian Date, hour, minute and a signed local time offset. The code contains helper functions to compute MJD and to encode the offset.
* `ct_use_pc_time` (boolean): when `True`, CT uses the PC system time (UTC-aware `datetime.now(timezone.utc)`). When `False`, the script attempts to parse the `ct_manual` field. If parsing fails, it falls back to PC time and logs a debug message.
* `ct_manual` formats accepted:

  * `YYYY-MM-DD HH:MM` — uses that date/time as the CT timestamp (interpreted as local time, converted to UTC internally).
  * `HH:MM` — uses today's date with that time (interpreted as local time).
* `ct_offset` is a signed integer in minutes and is encoded in 30-minute units as required by the RDS spec (the script validates range).
* The CT **minute-roll sender** thread waits until the next minute boundary of the PC local clock and then transmits CT; this ensures RDS receivers see CT sent immediately after the minute changes.

---

## Configuration file (`config.json`)

The script reads and writes `config.json` in the working directory. Example fields saved/used:

```json
{
  "port": "COM3",
  "baud": 9600,
  "pty_index": 1,
  "pi": "E230",
  "ps": "LOS40 CL",
  "rt": "RadioText up to 64 chars",
  "rt_address": "2",
  "debug": false,
  "save_log": false,
  "log_filename": "rds_debug.log",
  "server_port": 8080,
  "ct_enabled": false,
  "ct_offset": 60,
  "ct_use_pc_time": true,
  "ct_manual": ""
}
```

Important fields explained:

* `port`: serial device (e.g., `COM1` on Windows or `/dev/ttyUSB0` on Linux).
* `baud`: serial baud rate.
* `pty_index`: index into the built-in PTY table used to select a Program Type.
* `pi`: Program Identification code (4 hex chars).
* `ps`: Program Service name (max 16 chars).
* `rt`: RadioText message (max 64 chars).
* `rt_address`: `2` for A, `3` for B addressing.
* `ct_enabled`: enable CT sending.
* `ct_offset`: local offset in minutes (e.g., `60` for UTC+1).
* `ct_use_pc_time`: when `true` the CT sender uses the PC clock; when `false` the script tries to parse `ct_manual`.

---

## Command-line / usage

Basic invocation (from the repository directory):

```bash
python pyrds_web_ct_pc_toggle.py
```

Options:

* `start` (positional, optional): pass the literal word `start` to auto-start sending shortly after the GUI opens.

Example:

```bash
python pyrds_web_ct_pc_toggle.py start
```

* `--port <HTTP_PORT>`: override the HTTP server port used by the internal Flask web UI. If not given, the script uses the `server_port` value in `config.json` or the default `8080`.

Example:

```bash
python pyrds_web_ct_pc_toggle.py --port 9090
```

---

## Web interface (Flask)

* The web UI runs on `0.0.0.0:<server_port>` in a background thread.
* It exposes a small form with the same configuration fields as the Tkinter GUI. Submitting the form sends a message to the main app via an internal command queue and the GUI is updated.
* Quick actions available: `Save Config`, `Start`, `Stop`, and `Status (JSON)`.

**Security note:** The Flask server is not hardened or authenticated — it is a convenience interface for local networks and lab use only. If you expose this port on an untrusted network, add authentication / a reverse proxy or firewall rules.

---

## PTY table

The script contains a built-in `PTY_TABLE` array (hex value, name). The PTY value is used to craft the Group 2B/2A data fields (program type). You can change the table or select entries from the GUI's dropdown.

---

## Troubleshooting & tips

* If `tkinter` GUI fails to start on Linux, install `python3-tk` or the equivalent distribution package.
* If `pyserial` cannot open a port, ensure the device exists and you have permission; on Linux you may need to add your user to `dialout` or run with appropriate privileges.
* If characters look wrong in RT/PS: the code attempts to encode 2-character slices with `gb2312` and falls back to `utf-8` if necessary. Ensure the transmitter expects the intended encoding.
* If CT seems out of sync: check your PC clock and timezone settings, and verify `ct_offset` is correct for the transmitter’s expected local time encoding.

---

## Safety & legal

This tool sends RDS frames to an FM transmitter. Use it only with equipment you own or are explicitly authorized to test. Transmitting on licensed FM frequencies without authorization is illegal in most jurisdictions. The author and contributors are not responsible for misuse.

---

## Contributing

* Bug reports and pull requests are welcome. Keep changes focused (e.g., add tests, improve error handling or modularize the code).

---

## License

GNU GPLv3

---

If you want, I can also:

* Generate a shorter `USAGE.md` showing example sessions and common commands.
* Produce a sample `config.json` file to ship with the repo.
* Convert this README to Spanish or another language.
