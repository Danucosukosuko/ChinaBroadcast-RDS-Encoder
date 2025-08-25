#!/usr/bin/env python3

import sys
import os
import json
import threading
import time
import queue
import argparse
from dataclasses import dataclass
from typing import List

from datetime import datetime, timezone, timedelta

import serial
import serial.tools.list_ports
from flask import Flask, render_template_string, request, redirect, url_for, jsonify

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext

CONFIG_FILE = "config.json"
DEFAULT_HTTP_PORT = 8080

PTY_TABLE = [
    ("0008", "None"),
    ("0028", "News"),
    ("0048", "Current Affairs"),
    ("0068", "Information"),
    ("0088", "Sports"),
    ("00A8", "Education"),
    ("00C8", "Drama"),
    ("00E8", "Culture"),
    ("0108", "Science"),
    ("0128", "Varied Speech"),
    ("0148", "POP Music"),
    ("0168", "Rock Music"),
    ("0188", "Easy Listening"),
    ("01A8", "Light Classics"),
    ("01C8", "Classical Music"),
    ("01E8", "Other Music"),
    ("0208", "Weather"),
    ("0228", "Finance"),
    ("0248", "Children's Progs"),
    ("0268", "Social Affairs"),
    ("0288", "Religion"),
    ("02A8", "Phone In"),
    ("02C8", "Travel & Touring"),
    ("02E8", "Leisure & Hobby"),
    ("0308", "Jazz Music"),
    ("0328", "Country Music"),
    ("0348", "National Music"),
    ("0368", "Oldies Music"),
    ("0388", "Folk Music"),
    ("03A8", "Documentary"),
]

# ----------------- Util functions -----------------
def replace_char(s: str, index: int, newchar: str) -> str:
    if s is None or index < 0 or index >= len(s):
        return s
    lst = list(s)
    lst[index] = newchar
    return "".join(lst)

def pad_or_truncate(s: str, length: int, pad_char: str = " ") -> str:
    if s is None:
        s = ""
    return s.ljust(length, pad_char)[:length]

def encode_gb2312_two_bytes(twochars: str) -> bytes:
    t = pad_or_truncate(twochars, 2, " ")
    try:
        b = t.encode("gb2312", errors="replace")
    except Exception:
        b = t.encode("utf-8", errors="replace")
    if len(b) < 2:
        b = b.ljust(2, b" ")
    return b[:2]

def to_hex_byte(hexstr: str) -> int:
    if hexstr is None or len(hexstr) < 2:
        raise ValueError("hex string too short")
    return int(hexstr, 16)

def hex_dump(buffer: bytes) -> str:
    return " ".join(f"{b:02X}" for b in buffer)

# ----------------- CT (Clock Time) helpers -----------------
def mjd_from_datetime_utc(dt_utc: datetime) -> int:
    dt = dt_utc.astimezone(timezone.utc)
    secs = dt.timestamp()
    days = secs / 86400.0
    mjd = int(days + 40587)  # MJD reference
    return mjd

def encode_local_time_offset_minutes(offset_minutes: int) -> int:
    sign = 0 if offset_minutes >= 0 else 1
    mag = abs(offset_minutes) // 30
    if mag > 0x1F:
        raise ValueError("Offset out of range (-15.5h .. +15.5h)")
    return (sign << 5) | (mag & 0x1F)

def make_rds_ct_payload_and_frame(pi_hex: str, dt_utc: datetime, local_offset_minutes: int,
                                  pty: int = 0, tp: int = 0) -> bytes:
    """
    Construye el frame (grupo 4A) de CT. tp=0/1 se coloca en block2.
    (No metemos TA aquí porque TA pertenece semánticamente a grupos 0A/0B/15B)
    """
    dt = dt_utc.astimezone(timezone.utc)
    mjd = mjd_from_datetime_utc(dt)
    hour = dt.hour
    minute = dt.minute
    lto6 = encode_local_time_offset_minutes(local_offset_minutes)

    payload34 = ((mjd & 0x1FFFF) << 17) | ((hour & 0x1F) << 12) | ((minute & 0x3F) << 6) | (lto6 & 0x3F)

    top2 = (payload34 >> 32) & 0x3
    block3 = (payload34 >> 16) & 0xFFFF
    block4 = payload34 & 0xFFFF

    GROUP_TYPE = 4
    VERSION_A = 0
    block2_base = (GROUP_TYPE << 12) | (VERSION_A << 11) | ((tp & 0x1) << 10) | ((pty & 0x1F) << 5)
    block2 = block2_base | (top2 & 0x3)

    pi_val = int(pi_hex, 16) & 0xFFFF
    pi_hi = (pi_val >> 8) & 0xFF
    pi_lo = pi_val & 0xFF
    b2_hi = (block2 >> 8) & 0xFF
    b2_lo = block2 & 0xFF
    b3_hi = (block3 >> 8) & 0xFF
    b3_lo = block3 & 0xFF
    b4_hi = (block4 >> 8) & 0xFF
    b4_lo = block4 & 0xFF

    frame = bytes([pi_hi, pi_lo, b2_hi, b2_lo, b3_hi, b3_lo, b4_hi, b4_lo, 0x0A])
    return frame

def parse_manual_ct_datetime(manual_str: str):
    """
    Acepta:
      - 'YYYY-MM-DD HH:MM'
      - 'HH:MM' (usa la fecha del PC)
    Returns datetime UTC-aware.
    """
    if not manual_str or not manual_str.strip():
        return None
    s = manual_str.strip()
    try:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M")
        local_tz = datetime.now().astimezone().tzinfo
        dt_local = dt.replace(tzinfo=local_tz)
        return dt_local.astimezone(timezone.utc)
    except Exception:
        try:
            dt2 = datetime.strptime(s, "%H:%M")
            today = datetime.now().date()
            dt = datetime.combine(today, dt2.time())
            local_tz = datetime.now().astimezone().tzinfo
            dt_local = dt.replace(tzinfo=local_tz)
            return dt_local.astimezone(timezone.utc)
        except Exception:
            return None

def get_pc_offset_minutes() -> int:
    """Devuelve el offset del PC en minutos (ej: +120)."""
    local = datetime.now().astimezone()
    offs = local.utcoffset()
    if offs is None:
        return 0
    return int(offs.total_seconds() // 60)

# ----------------- Helper: construir grupo 0A con TA -----------------
def make_rds_group0a_frame(pi_hex: str, tp: int = 0, pty: int = 0, ta: int = 0, ps_bytes_pair=(b' ', b' ')) -> bytes:
    """
    Construye un frame para group 0A (basic tuning / PS). Se puede usar para
    transmitir TA=1 (Traffic Announcement). ps_bytes_pair debe ser dos bloques
    de 2 bytes cada uno (p. ej. PS_bytes[0], PS_bytes[1]).
    """
    GROUP_TYPE = 0
    VERSION_A = 0
    top2 = 0  # para 0A no usamos top2 desde payload
    block3 = (ps_bytes_pair[0][0] << 8) | (ps_bytes_pair[0][1] & 0xFF)
    block4 = (ps_bytes_pair[1][0] << 8) | (ps_bytes_pair[1][1] & 0xFF)

    block2_base = (GROUP_TYPE << 12) | (VERSION_A << 11) | ((tp & 0x1) << 10) | ((pty & 0x1F) << 5) | ((ta & 0x1) << 4)
    block2 = block2_base | (top2 & 0x3)

    pi_val = int(pi_hex, 16) & 0xFFFF
    pi_hi = (pi_val >> 8) & 0xFF
    pi_lo = pi_val & 0xFF
    b2_hi = (block2 >> 8) & 0xFF
    b2_lo = block2 & 0xFF
    b3_hi = (block3 >> 8) & 0xFF
    b3_lo = block3 & 0xFF
    b4_hi = (block4 >> 8) & 0xFF
    b4_lo = block4 & 0xFF

    frame = bytes([pi_hi, pi_lo, b2_hi, b2_lo, b3_hi, b3_lo, b4_hi, b4_lo, 0x0A])
    return frame

# ----------------- Data classes -----------------
@dataclass
class RDSParams:
    FGdata: str
    GJ_input: str
    PS_input: str
    RT_input: str
    RTaddress: str

# ----------------- RDS Worker -----------------
class RDSWorker(threading.Thread):
    def __init__(self, ser: serial.Serial, write_lock: threading.Lock, params: RDSParams,
                 stop_event: threading.Event, q_status: queue.Queue,
                 ct_enabled: bool = False, ct_offset_minutes: int = 60,
                 ct_use_pc_time: bool = True, ct_manual: str = "",
                 ct_omit_offset: bool = False, ct_tp: bool = False, ct_ta: bool = False):
        super().__init__(daemon=True)
        self.ser = ser
        self.write_lock = write_lock
        self.params = params
        self.stop_event = stop_event
        self.q_status = q_status
        self.ct_enabled = ct_enabled
        self.ct_offset_minutes = ct_offset_minutes
        self.ct_use_pc_time = ct_use_pc_time
        self.ct_manual = ct_manual
        self.ct_omit_offset = ct_omit_offset
        self.ct_tp = bool(ct_tp)
        self.ct_ta = bool(ct_ta)

        self._ct_thread = None

    def run(self):
        try:
            self._send_loop()
        except Exception as e:
            self.q_status.put(("error", str(e)))
        finally:
            self.q_status.put(("finished", ""))

    def _prepare_slices(self):
        FG = pad_or_truncate(self.params.FGdata, 4, "0").upper()
        FGdata2 = FG[0:2]
        FGdata3 = FG[2:4]

        gj = pad_or_truncate(self.params.GJ_input.upper(), 4, "0")
        GJdata2 = gj[0:2]
        GJdata3 = gj[2:4]

        ps = pad_or_truncate(self.params.PS_input, 16, " ")
        PSparts = [ps[i:i+2] for i in range(0, 16, 2)]

        PSadd0 = replace_char(FGdata3, 1, "8")
        PSadd1 = replace_char(FGdata3, 1, "9")
        PSadd2 = replace_char(FGdata3, 1, "A")
        PSadd3 = replace_char(FGdata3, 1, "B")

        rt = pad_or_truncate(self.params.RT_input, 64, " ")
        RTparts = [rt[i:i+2] for i in range(0, 64, 2)]

        text4 = (self.params.RTaddress or "2") + FG[1:4]
        RTAadd2 = text4[0:2]
        input_hex = text4[2:4]

        hex_digits = "0123456789ABCDEF"
        RTadds = [replace_char(input_hex, 1, d) for d in hex_digits[:16]]

        return {
            "FGdata2": FGdata2, "FGdata3": FGdata3,
            "GJdata2": GJdata2, "GJdata3": GJdata3,
            "PSparts": PSparts, "PSadds": [PSadd0, PSadd1, PSadd2, PSadd3],
            "RTparts": RTparts, "RTAadd2": RTAadd2, "RTadds": RTadds
        }

    def _safe_write(self, buffer: bytes):
        if self.stop_event.is_set():
            raise RuntimeError("Stop requested before write")
        if not self.ser.is_open:
            raise RuntimeError("Serial port closed")
        try:
            with self.write_lock:
                self.ser.write(buffer)
        except serial.SerialTimeoutException as e:
            self.q_status.put(("error", f"Serial write timeout: {e}"))
            raise
        except serial.SerialException as e:
            self.q_status.put(("error", f"Serial error during write: {e}"))
            raise
        except Exception as e:
            self.q_status.put(("error", f"Unknown error during write: {e}"))
            raise

    def _get_ct_datetime_and_offset(self):
        """
        Devuelve (dt_for_payload_utc, offset_minutes_to_send)
        - Si ct_omit_offset: se crea un datetime con los componentes de la hora local
          pero con tz=UTC y se envía offset=0 (modo "raw").
        - Si ct_use_pc_time y NO ct_omit_offset: usa UTC real y envía offset del PC.
        - Si NO ct_use_pc_time: intenta parsear manual; si falla, usa UTC y el offset configurado.
        """
        if self.ct_omit_offset:
            now_local = datetime.now().astimezone()
            dt_fake = datetime(now_local.year, now_local.month, now_local.day,
                               now_local.hour, now_local.minute, tzinfo=timezone.utc)
            return dt_fake, 0

        if self.ct_use_pc_time:
            dt_utc = datetime.now(timezone.utc)
            try:
                offset = get_pc_offset_minutes()
            except Exception:
                offset = self.ct_offset_minutes
            return dt_utc, offset
        else:
            parsed = parse_manual_ct_datetime(self.ct_manual)
            if parsed is not None:
                return parsed, self.ct_offset_minutes
            else:
                self.q_status.put(("debug", "CT manual invalid; using PC time as fallback"))
                dt_utc = datetime.now(timezone.utc)
                try:
                    offset = get_pc_offset_minutes()
                except Exception:
                    offset = self.ct_offset_minutes
                return dt_utc, offset

    # --- CT thread helpers ---
    def _start_ct_thread(self):
        if not self.ct_enabled:
            return
        if self._ct_thread and self._ct_thread.is_alive():
            return
        self._ct_thread = threading.Thread(target=self._ct_sender, daemon=True)
        self._ct_thread.start()
        self.q_status.put(("debug", "CT thread started."))

    def _ct_sender(self):
        """
        Hilo que espera hasta el siguiente cambio de minuto del PC (borde :00) y envía CT justo ahí.
        Envía la hora del PC cada minuto según las opciones (omit offset / use PC time / manual).
        El CT incluirá el bit TP si está activado.
        """
        last_minute_sent = None
        while not self.stop_event.is_set():
            now_local = datetime.now().astimezone()
            secs_to_next = 60 - now_local.second - (now_local.microsecond / 1_000_000)
            if secs_to_next <= 0:
                secs_to_next = 0.05
            if self.stop_event.wait(timeout=secs_to_next):
                break

            try:
                dt_for_payload, offset_to_send = self._get_ct_datetime_and_offset()
                if last_minute_sent is None or dt_for_payload.minute != last_minute_sent:
                    ct_frame = make_rds_ct_payload_and_frame(self.params.GJ_input, dt_for_payload,
                                                             offset_to_send, pty=0, tp=int(bool(self.ct_tp)))
                    self._safe_write(ct_frame)
                    self.q_status.put(("debug", f"CT (minute-roll) -> {hex_dump(ct_frame)} (omit_offset={self.ct_omit_offset}, TP={int(bool(self.ct_tp))})"))
                    last_minute_sent = dt_for_payload.minute
            except Exception as e:
                self.q_status.put(("debug", f"CT thread failed to send: {e}"))
                # seguir intentando el siguiente minuto

        self.q_status.put(("debug", "CT thread exiting."))

    def _send_group0a_ta(self, PS_bytes):
        """
        Envía un grupo 0A con TA=1 (o TA=0 si se quiere retirar).
        Usa los primeros dos PS 'parts' (2+2 bytes) para el contenido del bloque 3 y 4,
        para no chocar demasiado con la secuencia PS que emites normalmente.
        """
        try:
            # PS_bytes debe ser una lista de bytes de 2 elementos mínimo
            pair0 = PS_bytes[0] if len(PS_bytes) > 0 else b"  "
            pair1 = PS_bytes[1] if len(PS_bytes) > 1 else b"  "
            frame = make_rds_group0a_frame(self.params.GJ_input, tp=int(bool(self.ct_tp)), pty=0, ta=1, ps_bytes_pair=(pair0, pair1))
            self._safe_write(frame)
            self.q_status.put(("debug", f"TA group 0A sent -> {hex_dump(frame)} (TP={int(bool(self.ct_tp))})"))
        except Exception as e:
            self.q_status.put(("debug", f"Failed to send TA group: {e}"))

    def _send_loop(self):
        slices = self._prepare_slices()
        ser = self.ser

        if not ser.is_open:
            raise RuntimeError("Serial port not open")

        PS_bytes = [encode_gb2312_two_bytes(p) for p in slices["PSparts"]]
        RT_bytes = [encode_gb2312_two_bytes(p) for p in slices["RTparts"]]

        try:
            GJ_b0 = to_hex_byte(slices["GJdata2"])
            GJ_b1 = to_hex_byte(slices["GJdata3"])
            FG_b = to_hex_byte(slices["FGdata2"])
            RTAadd2_b = to_hex_byte(slices["RTAadd2"])
        except Exception as e:
            raise ValueError(f"Invalid hex fields (PI/FG): {e}")

        PSadds_int = []
        for s in slices["PSadds"]:
            try:
                PSadds_int.append(to_hex_byte(s))
            except Exception:
                PSadds_int.append(0)

        RTadds_int = []
        for s in slices["RTadds"]:
            try:
                RTadds_int.append(to_hex_byte(s))
            except Exception:
                RTadds_int.append(0)

        EOL = 0x0A

        # --- Enviar PS (4 paquetes) ---
        self.q_status.put(("status", "Sending PS (4 packets)..."))
        for i in range(4):
            if self.stop_event.is_set():
                self.q_status.put(("status", "Stop requested (during PS)."))
                return
            buffer = bytearray(9)
            buffer[0] = GJ_b0
            buffer[1] = GJ_b1
            buffer[2] = FG_b
            buffer[3] = PSadds_int[i]
            buffer[6] = PS_bytes[i][0]
            buffer[7] = PS_bytes[i][1]
            buffer[8] = EOL
            self._safe_write(bytes(buffer))
            self.q_status.put(("debug", f"PS {i+1}/4 -> {hex_dump(buffer)}"))
            self.q_status.put(("status", f"PS packet {i+1}/4 sent."))
            for _ in range(10):
                if self.stop_event.is_set():
                    break
                time.sleep(0.1)
            if self.stop_event.is_set():
                return

        # --- Enviar RT (16 paquetes) ---
        self.q_status.put(("status", "Sending RT packets..."))
        rt_index = 0
        for addr_index, rtaddr in enumerate(RTadds_int[:16]):
            if self.stop_event.is_set():
                self.q_status.put(("status", "Stop requested (during RT)."))
                return
            buffer = bytearray(9)
            buffer[0] = GJ_b0
            buffer[1] = GJ_b1
            buffer[2] = RTAadd2_b
            buffer[3] = rtaddr
            r0 = RT_bytes[rt_index]
            r1 = RT_bytes[rt_index + 1]
            buffer[4] = r0[0]
            buffer[5] = r0[1]
            buffer[6] = r1[0]
            buffer[7] = r1[1]
            buffer[8] = EOL
            self._safe_write(bytes(buffer))
            self.q_status.put(("debug", f"RT {addr_index+1}/16 (idx {rt_index}) -> {hex_dump(buffer)}"))
            self.q_status.put(("status", f"RT packet {addr_index+1}/16 sent (RT idx {rt_index})."))
            rt_index += 2
            for _ in range(10):
                if self.stop_event.is_set():
                    break
                time.sleep(0.1)
            if self.stop_event.is_set():
                return

        self.q_status.put(("status", "All packets sent once. Now looping until Stop."))

        # --- Emisión inicial de CT (si está activado) ---
        last_minute_sent = None
        if self.ct_enabled:
            try:
                dt_for_payload, offset_to_send = self._get_ct_datetime_and_offset()
                ct_frame = make_rds_ct_payload_and_frame(self.params.GJ_input, dt_for_payload, offset_to_send, pty=0, tp=int(bool(self.ct_tp)))
                self._safe_write(ct_frame)
                self.q_status.put(("debug", f"CT initial -> {hex_dump(ct_frame)} (omit_offset={self.ct_omit_offset}, TP={int(bool(self.ct_tp))})"))
                last_minute_sent = dt_for_payload.minute
                for _ in range(5):
                    if self.stop_event.is_set():
                        return
                    time.sleep(0.1)
            except Exception as e:
                self.q_status.put(("debug", f"CT initial failed: {e}"))

        # iniciar hilo CT que enviará CT justo en el cambio de minuto
        if self.ct_enabled:
            self._start_ct_thread()

        # --- Bucle principal: repetir PS y RT (el hilo CT se encarga de los CTs) ---
        while not self.stop_event.is_set():
            # Si TA está activado, enviar grupo 0A con TA=1 para señalización
            if self.ct_ta:
                try:
                    self._send_group0a_ta(PS_bytes)
                except Exception:
                    pass

            # PS loop
            for i in range(4):
                if self.stop_event.is_set():
                    self.q_status.put(("status", "Stop requested (loop PS)."))
                    return
                buffer = bytearray(9)
                buffer[0] = GJ_b0
                buffer[1] = GJ_b1
                buffer[2] = FG_b
                buffer[3] = PSadds_int[i]
                buffer[6] = PS_bytes[i][0]
                buffer[7] = PS_bytes[i][1]
                buffer[8] = EOL
                self._safe_write(bytes(buffer))
                self.q_status.put(("debug", f"Loop PS {i+1}/4 -> {hex_dump(buffer)}"))
                self.q_status.put(("status", f"Loop PS packet {i+1}/4 sent."))
                for _ in range(10):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)
                if self.stop_event.is_set():
                    return

            # RT loop
            rt_index = 0
            for addr_index, rtaddr in enumerate(RTadds_int[:16]):
                if self.stop_event.is_set():
                    self.q_status.put(("status", "Stop requested (loop RT)."))
                    return
                buffer = bytearray(9)
                buffer[0] = GJ_b0
                buffer[1] = GJ_b1
                buffer[2] = RTAadd2_b
                buffer[3] = rtaddr
                r0 = RT_bytes[rt_index]
                r1 = RT_bytes[rt_index + 1]
                buffer[4] = r0[0]
                buffer[5] = r0[1]
                buffer[6] = r1[0]
                buffer[7] = r1[1]
                buffer[8] = EOL
                self._safe_write(bytes(buffer))
                self.q_status.put(("debug", f"Loop RT {addr_index+1}/16 -> {hex_dump(buffer)}"))
                self.q_status.put(("status", f"Loop RT packet {addr_index+1}/16 sent."))
                rt_index += 2
                for _ in range(10):
                    if self.stop_event.is_set():
                        break
                    time.sleep(0.1)
                if self.stop_event.is_set():
                    return

        self.q_status.put(("status", "Stop requested; finishing worker."))

# ----------------- Flask server -----------------
def create_flask_app(command_queue: queue.Queue, status_queue: queue.Queue, get_config_func):
    app = Flask("pyrds_web_ct_pc_toggle")

    FORM_HTML = """
    <!doctype html>
    <html>
    <head>
      <meta charset="utf-8">
      <title>RDS Control - Web (CT / TP / TA)</title>
      <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body class="bg-light">
    <div class="container py-4">
      <h2>RDS Control — Web Interface (CT / TP / TA)</h2>
      <form method="post" action="/save" class="mt-3">
        <div class="row">
          <div class="col-md-6">
            <label class="form-label">Serial port</label>
            <input name="port" class="form-control" value="{{cfg.port}}">
          </div>
          <div class="col-md-2">
            <label class="form-label">Baud</label>
            <input name="baud" class="form-control" value="{{cfg.baud}}">
          </div>
          <div class="col-md-4">
            <label class="form-label">Server port (HTTP)</label>
            <input name="server_port" class="form-control" value="{{cfg.server_port}}">
          </div>
        </div>

        <div class="row mt-2">
          <div class="col-md-4">
            <label class="form-label">PTY index</label>
            <input name="pty_index" class="form-control" value="{{cfg.pty_index}}">
          </div>
          <div class="col-md-4">
            <label class="form-label">PI (4 hex)</label>
            <input name="pi" class="form-control" value="{{cfg.pi}}">
          </div>
          <div class="col-md-4">
            <label class="form-label">PS (16 chars)</label>
            <input name="ps" class="form-control" value="{{cfg.ps}}">
          </div>
        </div>

        <div class="row mt-2">
          <div class="col-md-12">
            <label class="form-label">RT (max 64 chars)</label>
            <textarea name="rt" class="form-control" rows="2">{{cfg.rt}}</textarea>
          </div>
        </div>

        <div class="row mt-2">
          <div class="col-md-2">
            <label class="form-label">RT address</label>
            <select name="rt_address" class="form-select">
              <option value="2" {% if cfg.rt_address=='2' %}selected{% endif %}>A</option>
              <option value="3" {% if cfg.rt_address=='3' %}selected{% endif %}>B</option>
            </select>
          </div>
          <div class="col-md-2">
            <label class="form-label">Enable CT</label>
            <input type="checkbox" name="ct_enabled" {% if cfg.ct_enabled %}checked{% endif %}>
          </div>
          <div class="col-md-3">
            <label class="form-label">CT offset (minutes)</label>
            <input name="ct_offset" class="form-control" value="{{cfg.ct_offset}}">
          </div>
          <div class="col-md-3">
            <label class="form-label">Use PC time for CT</label>
            <input type="checkbox" name="ct_use_pc_time" {% if cfg.ct_use_pc_time %}checked{% endif %}>
          </div>
          <div class="col-md-2">
            <label class="form-label">CT manual (YYYY-MM-DD HH:MM)</label>
            <input name="ct_manual" class="form-control" value="{{cfg.ct_manual}}">
          </div>
        </div>

        <div class="row mt-2">
          <div class="col-md-3">
            <label class="form-label">Omit offset (send raw PC time)</label><br>
            <input type="checkbox" name="ct_omit_offset" {% if cfg.ct_omit_offset %}checked{% endif %}>
          </div>
          <div class="col-md-3">
            <label class="form-label">TP (Traffic Programme)</label><br>
            <input type="checkbox" name="ct_tp" {% if cfg.ct_tp %}checked{% endif %}>
          </div>
          <div class="col-md-3">
            <label class="form-label">TA (Traffic Announcement)</label><br>
            <input type="checkbox" name="ct_ta" {% if cfg.ct_ta %}checked{% endif %}>
          </div>
        </div>

        <div class="row mt-3">
          <div class="col-md-12">
            <button class="btn btn-primary" type="submit">Save Config</button>
            <a class="btn btn-success" href="/start">Start</a>
            <a class="btn btn-danger" href="/stop">Stop</a>
            <a class="btn btn-secondary" href="/status">Status (JSON)</a>
          </div>
        </div>
      </form>

      <hr>
      <h5>Quick actions</h5>
      <p>Desde aquí puedes arrancar/parar el envío RDS y guardar configuración. Los cambios se aplican automáticamente en la GUI.</p>

    </div>
    </body>
    </html>
    """

    @app.route("/", methods=["GET"])
    def index():
        cfg = get_config_func()
        return render_template_string(FORM_HTML, cfg=cfg)

    @app.route("/save", methods=["POST"])
    def save():
        form = request.form
        cfg = {
            "port": form.get("port", ""),
            "baud": int(form.get("baud", 9600)),
            "pty_index": int(form.get("pty_index", 1)),
            "pi": form.get("pi", "C121"),
            "ps": form.get("ps", "GD-2015"),
            "rt": form.get("rt", ""),
            "rt_address": form.get("rt_address", "2"),
            "debug": bool(form.get("debug")),
            "save_log": bool(form.get("save_log")),
            "log_filename": form.get("log_filename", "rds_debug.log") if form.get("log_filename") else "rds_debug.log",
            "server_port": int(form.get("server_port", DEFAULT_HTTP_PORT)),
            "ct_enabled": bool(form.get("ct_enabled")),
            "ct_offset": int(form.get("ct_offset") or 60),
            "ct_use_pc_time": bool(form.get("ct_use_pc_time")),
            "ct_manual": form.get("ct_manual", ""),
            "ct_omit_offset": bool(form.get("ct_omit_offset")),
            "ct_tp": bool(form.get("ct_tp")),
            "ct_ta": bool(form.get("ct_ta"))
        }
        command_queue.put(("apply_config", cfg))
        return redirect(url_for("index"))

    @app.route("/start", methods=["GET"])
    def start_route():
        command_queue.put(("start", None))
        return redirect(url_for("index"))

    @app.route("/stop", methods=["GET"])
    def stop_route():
        command_queue.put(("stop", None))
        return redirect(url_for("index"))

    @app.route("/status", methods=["GET"])
    def status_route():
        cfg = get_config_func()
        return jsonify({"status": "ok", "config": cfg})

    return app

# ----------------- Tkinter App -----------------
class App:
    def __init__(self, root, command_queue: queue.Queue, status_queue: queue.Queue, autostart=False, http_port=DEFAULT_HTTP_PORT):
        self.root = root
        self.command_queue = command_queue
        self.status_queue = status_queue
        self.autostart = autostart
        self.http_port = http_port

        self.ser = serial.Serial()
        self.ser.baudrate = 9600
        self.ser.timeout = 1
        self.ser.write_timeout = 1

        self.serial_write_lock = threading.Lock()

        self.stop_event = threading.Event()
        self.worker = None

        self.cfg = {
            "port": "",
            "baud": 9600,
            "pty_index": 1,
            "pi": "C121",
            "ps": "GD-2015",
            "rt": "GD-2015",
            "rt_address": "2",
            "debug": False,
            "save_log": False,
            "log_filename": "rds_debug.log",
            "server_port": http_port,
            "ct_enabled": False,
            "ct_offset": 60,
            "ct_use_pc_time": True,
            "ct_manual": "",
            "ct_omit_offset": False,
            "ct_tp": False,
            "ct_ta": False
        }

        self._build_ui()
        self._fill_pty()
        self.scan_ports()
        self.load_config()
        self._periodic_check()

        if self.autostart:
            self.root.after(700, self._try_auto_start)

    def _build_ui(self):
        self.root.title("Chinabroadcast GD-2015 RDS Encoder")
        self.root.geometry("1030x820")
        frm = ttk.Frame(self.root, padding=8)
        frm.pack(fill=tk.BOTH, expand=True)

        gb_port = ttk.LabelFrame(frm, text="Serial Port")
        gb_port.pack(fill=tk.X, pady=6)
        row = ttk.Frame(gb_port)
        row.pack(fill=tk.X, padx=8, pady=6)
        self.combo_ports = ttk.Combobox(row, values=[], state="readonly", width=30)
        self.combo_ports.pack(side=tk.LEFT, padx=(0,8))
        self.btn_scan = ttk.Button(row, text="Scan", command=self.scan_ports)
        self.btn_scan.pack(side=tk.LEFT, padx=(0,8))
        self.btn_open = ttk.Button(row, text="Open Serial Port", command=self.toggle_port)
        self.btn_open.pack(side=tk.LEFT, padx=(0,8))
        self.btn_savecfg = ttk.Button(row, text="Save Config", command=self.save_config)
        self.btn_savecfg.pack(side=tk.LEFT, padx=(0,8))
        self.led_canvas = tk.Canvas(row, width=14, height=14, highlightthickness=0)
        self.led_canvas.pack(side=tk.LEFT)
        self._set_led_color("gray")

        mid = ttk.Frame(frm)
        mid.pack(fill=tk.X, pady=6)

        gb_pty = ttk.LabelFrame(mid, text="Programme Type")
        gb_pty.pack(side=tk.LEFT, padx=6, pady=3, fill=tk.Y)
        self.combo_pty = ttk.Combobox(gb_pty, state="readonly", width=30)
        self.combo_pty.pack(padx=6, pady=6)
        self.lbl_ptyname = ttk.Label(gb_pty, text="PTY Name:")
        self.lbl_ptyname.pack(anchor=tk.W, padx=6)
        self.lbl_ptyno = ttk.Label(gb_pty, text="PTY No:")
        self.lbl_ptyno.pack(anchor=tk.W, padx=6)

        gb_pi = ttk.LabelFrame(mid, text="Country / PI")
        gb_pi.pack(side=tk.LEFT, padx=6, pady=3, fill=tk.Y)
        ttk.Label(gb_pi, text="PI (4 hex chars):").pack(anchor=tk.W, padx=6, pady=(4,0))
        self.entry_pi = ttk.Entry(gb_pi)
        self.entry_pi.pack(padx=6, pady=4)
        self.entry_pi.insert(0, "C121")

        gb_ps = ttk.LabelFrame(mid, text="Program Identification (PS)")
        gb_ps.pack(side=tk.LEFT, padx=6, pady=3, fill=tk.Y)
        ttk.Label(gb_ps, text="PS (max 16 chars):").pack(anchor=tk.W, padx=6, pady=(4,0))
        self.entry_ps = ttk.Entry(gb_ps, width=30)
        self.entry_ps.pack(padx=6, pady=4)
        self.entry_ps.insert(0, "GD-2015")

        gb_rt = ttk.LabelFrame(frm, text="RadioText (RT)")
        gb_rt.pack(fill=tk.BOTH, padx=6, pady=6, expand=True)
        ttk.Label(gb_rt, text="RT (max 64 chars):").pack(anchor=tk.W, padx=6)
        self.text_rt = tk.Text(gb_rt, height=6, wrap=tk.WORD)
        self.text_rt.pack(fill=tk.BOTH, padx=6, pady=4, expand=True)
        self.text_rt.insert("1.0", "GD-2015")

        pan = ttk.Frame(frm)
        pan.pack(fill=tk.X, padx=6)
        ttk.Label(pan, text="RT Address:").pack(side=tk.LEFT)
        self.var_rtaddr = tk.StringVar(value="2")
        rb_a = ttk.Radiobutton(pan, text="A", variable=self.var_rtaddr, value="2")
        rb_b = ttk.Radiobutton(pan, text="B", variable=self.var_rtaddr, value="3")
        rb_a.pack(side=tk.LEFT, padx=4); rb_b.pack(side=tk.LEFT, padx=4)

        btns = ttk.Frame(frm)
        btns.pack(fill=tk.X, padx=6, pady=6)
        self.btn_send = ttk.Button(btns, text="Send", command=self.start_sending)
        self.btn_send.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0,4))
        self.btn_stop = ttk.Button(btns, text="Stop", command=self.stop_sending, state=tk.DISABLED)
        self.btn_stop.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4,0))

        # CT controls
        gb_ct = ttk.LabelFrame(frm, text="CT (Clock Time) & Traffic flags")
        gb_ct.pack(fill=tk.X, padx=6, pady=6)
        self.var_ct = tk.BooleanVar(value=False)
        chk_ct = ttk.Checkbutton(gb_ct, text="Enable CT (group 4A)", variable=self.var_ct)
        chk_ct.pack(side=tk.LEFT, padx=6)
        ttk.Label(gb_ct, text="CT offset (minutes)").pack(side=tk.LEFT, padx=(12,2))
        self.entry_ct_offset = ttk.Entry(gb_ct, width=6)
        self.entry_ct_offset.pack(side=tk.LEFT, padx=(0,6))
        self.entry_ct_offset.insert(0, "120")

        # Use PC time
        self.var_ct_use_pc = tk.BooleanVar(value=True)
        chk_ct_pc = ttk.Checkbutton(gb_ct, text="Use PC time for CT", variable=self.var_ct_use_pc, command=self._on_ct_use_pc_changed)
        chk_ct_pc.pack(side=tk.LEFT, padx=(12,6))

        ttk.Label(gb_ct, text="CT manual (YYYY-MM-DD HH:MM)").pack(side=tk.LEFT, padx=(12,2))
        self.entry_ct_manual = ttk.Entry(gb_ct, width=20)
        self.entry_ct_manual.pack(side=tk.LEFT, padx=(0,6))
        self.entry_ct_manual.insert(0, "")

        # Omit offset and TP/TA controls
        self.var_ct_omit = tk.BooleanVar(value=False)
        chk_ct_omit = ttk.Checkbutton(gb_ct, text="Omit offset (send raw PC time)", variable=self.var_ct_omit)
        chk_ct_omit.pack(side=tk.LEFT, padx=(12,6))

        self.var_tp = tk.BooleanVar(value=False)
        chk_tp = ttk.Checkbutton(gb_ct, text="TP (Traffic Programme)", variable=self.var_tp)
        chk_tp.pack(side=tk.LEFT, padx=(12,6))

        self.var_ta = tk.BooleanVar(value=False)
        chk_ta = ttk.Checkbutton(gb_ct, text="TA (Traffic Announcement)", variable=self.var_ta)
        chk_ta.pack(side=tk.LEFT, padx=(12,6))

        # debug controls
        gb_debugctrl = ttk.LabelFrame(frm, text="Debug")
        gb_debugctrl.pack(fill=tk.X, padx=6, pady=6)
        self.var_debug = tk.BooleanVar(value=False)
        chk_debug = ttk.Checkbutton(gb_debugctrl, text="Enable Debug Console (hex dumps)", variable=self.var_debug)
        chk_debug.pack(side=tk.LEFT, padx=6)
        self.var_logfile = tk.BooleanVar(value=False)
        chk_log = ttk.Checkbutton(gb_debugctrl, text="Save debug to file", variable=self.var_logfile)
        chk_log.pack(side=tk.LEFT, padx=6)
        ttk.Label(gb_debugctrl, text="Filename:").pack(side=tk.LEFT, padx=(12,2))
        self.entry_logname = ttk.Entry(gb_debugctrl, width=28)
        self.entry_logname.pack(side=tk.LEFT, padx=(0,6))
        self.entry_logname.insert(0, "rds_debug.log")
        btn_clear = ttk.Button(gb_debugctrl, text="Clear Console", command=self._clear_debug_console)
        btn_clear.pack(side=tk.RIGHT, padx=6)

        gb_dbg = ttk.LabelFrame(frm, text="Debug Console")
        gb_dbg.pack(fill=tk.BOTH, padx=6, pady=6, expand=True)
        self.debug_text = scrolledtext.ScrolledText(gb_dbg, height=12, wrap=tk.NONE)
        self.debug_text.pack(fill=tk.BOTH, expand=True)
        self.debug_text.config(state=tk.DISABLED)

        self.status_var = tk.StringVar(value="Ready ✅")
        lbl_status = ttk.Label(frm, textvariable=self.status_var, relief=tk.SUNKEN, anchor=tk.W)
        lbl_status.pack(fill=tk.X, padx=6, pady=(4,0))

        self.combo_pty.bind("<<ComboboxSelected>>", lambda e: self.pty_changed())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        # initial UI state
        self._on_ct_use_pc_changed()

    def _on_ct_use_pc_changed(self):
        use_pc = bool(self.var_ct_use_pc.get())
        state = "disabled" if use_pc else "normal"
        try:
            self.entry_ct_manual.config(state=state)
        except Exception:
            pass

    def _set_led_color(self, color: str):
        self.led_canvas.delete("all")
        self.led_canvas.create_oval(0, 0, 14, 14, fill=color, outline=color)

    def _fill_pty(self):
        vals = [name for _hex, name in PTY_TABLE]
        self.combo_pty['values'] = vals
        if len(vals) > 1:
            self.combo_pty.current(1)
        self.pty_changed()

    def pty_changed(self):
        idx = self.combo_pty.current()
        if idx < 0:
            return
        hexval = PTY_TABLE[idx][0]
        name = PTY_TABLE[idx][1]
        self.lbl_ptyname.config(text=f"PTY Name: {name}")
        self.lbl_ptyno.config(text=f"PTY No: {hexval}")

    def scan_ports(self):
        ports = serial.tools.list_ports.comports()
        devices = [p.device for p in ports]
        if not devices:
            devices = [f"COM{i}" for i in range(1, 21)]
        self.combo_ports['values'] = devices
        if devices:
            self.combo_ports.current(0)
        self.status_var.set("Ports scanned ✅")

    def toggle_port(self):
        if self.ser.is_open:
            self.stop_sending()
            time.sleep(0.05)
            try:
                acquired = self.serial_write_lock.acquire(timeout=2)
                try:
                    if self.ser.is_open:
                        self.ser.close()
                finally:
                    if acquired:
                        self.serial_write_lock.release()
                self._set_led_color("gray")
                self.btn_open.config(text="Open Serial Port")
                self.status_var.set("Port closed.")
            except Exception as e:
                self.status_var.set(f"Port close failed: {e}")
            return

        port = self.combo_ports.get()
        if not port:
            messagebox.showwarning("Serial", "No port selected.")
            return
        try:
            self.ser.port = port
            self.ser.open()
            self._set_led_color("green")
            self.btn_open.config(text="Close Serial Port")
            self.status_var.set(f"Opened {port}")
        except Exception as e:
            messagebox.showerror("WRONG", f"The serial port fails to open:\n{e}")
            self.status_var.set("Open failed ❌")

    def _gather_params(self) -> RDSParams:
        idx = self.combo_pty.current()
        FGdata = PTY_TABLE[idx][0] if idx >= 0 else PTY_TABLE[0][0]
        GJ_input = self.entry_pi.get().strip().upper()
        PS_input = self.entry_ps.get()
        RT_input = self.text_rt.get("1.0", tk.END).rstrip("\n")
        RTaddress = self.var_rtaddr.get()
        return RDSParams(FGdata=FGdata, GJ_input=GJ_input, PS_input=PS_input, RT_input=RT_input, RTaddress=RTaddress)

    def start_sending(self):
        if not self.ser.is_open:
            port = self.combo_ports.get()
            if port:
                try:
                    self.ser.port = port
                    self.ser.open()
                    self._set_led_color("green")
                    self.btn_open.config(text="Close Serial Port")
                    self.status_var.set(f"Opened {port} automatically")
                except Exception as e:
                    messagebox.showwarning("Serial", f"Open failed: {e}")
                    return
            else:
                messagebox.showwarning("Serial", "Open a serial port first.")
                return

        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Already", "Worker already running.")
            return

        params = self._gather_params()
        try:
            if len(params.GJ_input) != 4 or int(params.GJ_input, 16) < 0:
                raise ValueError("PI must be 4 hex digits (e.g. E230).")
        except Exception as e:
            messagebox.showwarning("PI error", f"PI (GJ_input) invalid: {e}")
            return

        self.save_config()
        self.stop_event.clear()
        ct_enabled = bool(self.var_ct.get())
        try:
            ct_offset = int(self.entry_ct_offset.get())
        except Exception:
            ct_offset = 60
        ct_use_pc_time = bool(self.var_ct_use_pc.get())
        ct_manual = self.entry_ct_manual.get().strip()
        ct_omit = bool(self.var_ct_omit.get())
        ct_tp = bool(self.var_tp.get())
        ct_ta = bool(self.var_ta.get())
        self.worker = RDSWorker(self.ser, self.serial_write_lock, params, self.stop_event, self.status_queue,
                                ct_enabled=ct_enabled, ct_offset_minutes=ct_offset,
                                ct_use_pc_time=ct_use_pc_time, ct_manual=ct_manual,
                                ct_omit_offset=ct_omit, ct_tp=ct_tp, ct_ta=ct_ta)
        self.worker.start()
        self.btn_send.config(state=tk.DISABLED)
        self.btn_stop.config(state=tk.NORMAL)
        self.status_var.set("Sending started... 🚀")

    def stop_sending(self):
        if self.worker and self.worker.is_alive():
            self.stop_event.set()
            waited = 0.0
            while self.worker.is_alive() and waited < 5.0:
                time.sleep(0.1)
                waited += 0.1
            if self.worker.is_alive():
                try:
                    acquired = self.serial_write_lock.acquire(timeout=2)
                    if acquired:
                        self.serial_write_lock.release()
                except Exception:
                    pass
            if self.worker.is_alive():
                self.status_var.set("Worker did not stop cleanly (forcing).")
            else:
                try:
                    self.worker.join(timeout=1.0)
                except Exception:
                    pass
                try:
                    if hasattr(self.worker, "_ct_thread") and self.worker._ct_thread is not None:
                        self.worker._ct_thread.join(timeout=1.0)
                except Exception:
                    pass
                self.status_var.set("Worker stopped.")
        else:
            self.status_var.set("No worker running.")
        self.btn_send.config(state=tk.NORMAL)
        self.btn_stop.config(state=tk.DISABLED)

    def _clear_debug_console(self):
        self.debug_text.config(state=tk.NORMAL)
        self.debug_text.delete("1.0", tk.END)
        self.debug_text.config(state=tk.DISABLED)

    def _append_debug(self, text: str):
        self.debug_text.config(state=tk.NORMAL)
        self.debug_text.insert(tk.END, text + "\n")
        self.debug_text.see(tk.END)
        self.debug_text.config(state=tk.DISABLED)

    def save_config(self):
        try:
            ct_offset_val = int(self.entry_ct_offset.get())
        except Exception:
            ct_offset_val = 60
        cfg = {
            "port": self.combo_ports.get(),
            "baud": int(self.ser.baudrate),
            "pty_index": int(self.combo_pty.current() or 0),
            "pi": self.entry_pi.get(),
            "ps": self.entry_ps.get(),
            "rt": self.text_rt.get("1.0", tk.END).rstrip("\n"),
            "rt_address": self.var_rtaddr.get(),
            "debug": bool(self.var_debug.get()),
            "save_log": bool(self.var_logfile.get()),
            "log_filename": self.entry_logname.get().strip() or "rds_debug.log",
            "server_port": int(self.http_port or DEFAULT_HTTP_PORT),
            "ct_enabled": bool(self.var_ct.get()),
            "ct_offset": ct_offset_val,
            "ct_use_pc_time": bool(self.var_ct_use_pc.get()),
            "ct_manual": self.entry_ct_manual.get().strip(),
            "ct_omit_offset": bool(self.var_ct_omit.get()),
            "ct_tp": bool(self.var_tp.get()),
            "ct_ta": bool(self.var_ta.get())
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
            self.status_var.set(f"Config saved to {CONFIG_FILE} ✅")
            self.cfg.update(cfg)
        except Exception as e:
            self.status_var.set(f"Save config failed: {e}")

    def load_config(self):
        if not os.path.exists(CONFIG_FILE):
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            port = cfg.get("port")
            if port:
                try:
                    idx = list(self.combo_ports['values']).index(port)
                    self.combo_ports.current(idx)
                except Exception:
                    pass
            self.ser.baudrate = cfg.get("baud", 9600)
            try:
                self.combo_pty.current(int(cfg.get("pty_index", 1)))
            except Exception:
                pass
            self.entry_pi.delete(0, tk.END); self.entry_pi.insert(0, cfg.get("pi", "C121"))
            self.entry_ps.delete(0, tk.END); self.entry_ps.insert(0, cfg.get("ps", "GD-2015"))
            self.text_rt.delete("1.0", tk.END); self.text_rt.insert("1.0", cfg.get("rt", "GD-2015 FM Transmitter! Web: http://chinabroadcast.aliexpress.com/"))
            self.var_rtaddr.set(cfg.get("rt_address", "2"))
            self.var_debug.set(bool(cfg.get("debug", False)))
            self.var_logfile.set(bool(cfg.get("save_log", False)))
            self.entry_logname.delete(0, tk.END); self.entry_logname.insert(0, cfg.get("log_filename", "rds_debug.log"))
            self.var_ct.set(bool(cfg.get("ct_enabled", False)))
            self.entry_ct_offset.delete(0, tk.END); self.entry_ct_offset.insert(0, str(cfg.get("ct_offset", 60)))
            self.var_ct_use_pc.set(bool(cfg.get("ct_use_pc_time", True)))
            self.entry_ct_manual.delete(0, tk.END); self.entry_ct_manual.insert(0, cfg.get("ct_manual", ""))
            self.var_ct_omit.set(bool(cfg.get("ct_omit_offset", False)))
            self.var_tp.set(bool(cfg.get("ct_tp", False)))
            self.var_ta.set(bool(cfg.get("ct_ta", False)))
            self._on_ct_use_pc_changed()
            self.status_var.set(f"Config loaded from {CONFIG_FILE}")
            self.cfg.update(cfg)
        except Exception as e:
            self.status_var.set(f"Load config failed: {e}")

    def _periodic_check(self):
        try:
            while True:
                typ, msg = self.status_queue.get_nowait()
                if typ == "status":
                    self.status_var.set(msg)
                elif typ == "debug":
                    if self.var_debug.get():
                        self._append_debug(msg)
                    if self.var_logfile.get():
                        try:
                            fname = self.entry_logname.get().strip() or "rds_debug.log"
                            with open(fname, "a", encoding="utf-8") as f:
                                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} DEBUG: {msg}\n")
                        except Exception as e:
                            self.status_var.set(f"Log write error: {e}")
                elif typ == "error":
                    self.status_var.set("ERROR: " + msg)
                    messagebox.showerror("Worker error", msg)
                    self.stop_sending()
                elif typ == "finished":
                    self.btn_send.config(state=tk.NORMAL)
                    self.btn_stop.config(state=tk.DISABLED)
                    self.status_var.set("Stopped.")
        except queue.Empty:
            pass

        try:
            while True:
                cmd, payload = self.command_queue.get_nowait()
                if cmd == "apply_config":
                    self._apply_remote_config(payload)
                elif cmd == "start":
                    self.start_sending()
                elif cmd == "stop":
                    self.stop_sending()
                else:
                    pass
        except queue.Empty:
            pass

        self.root.after(150, self._periodic_check)

    def _apply_remote_config(self, cfg):
        try:
            if "port" in cfg and cfg["port"]:
                vals = list(self.combo_ports['values'])
                if cfg["port"] in vals:
                    self.combo_ports.current(vals.index(cfg["port"]))
            if "baud" in cfg:
                try:
                    self.ser.baudrate = int(cfg["baud"])
                except Exception:
                    pass
            if "pty_index" in cfg:
                try:
                    self.combo_pty.current(int(cfg["pty_index"]))
                except Exception:
                    pass
            if "pi" in cfg:
                self.entry_pi.delete(0, tk.END); self.entry_pi.insert(0, cfg.get("pi", "C121"))
            if "ps" in cfg:
                self.entry_ps.delete(0, tk.END); self.entry_ps.insert(0, cfg.get("ps", "GD-2015"))
            if "rt" in cfg:
                self.text_rt.delete("1.0", tk.END); self.text_rt.insert("1.0", cfg.get("rt", ""))
            if "rt_address" in cfg:
                self.var_rtaddr.set(cfg.get("rt_address", "2"))
            if "debug" in cfg:
                self.var_debug.set(bool(cfg.get("debug", False)))
            if "save_log" in cfg:
                self.var_logfile.set(bool(cfg.get("save_log", False)))
            if "log_filename" in cfg:
                self.entry_logname.delete(0, tk.END); self.entry_logname.insert(0, cfg.get("log_filename", "rds_debug.log"))
            if "ct_enabled" in cfg:
                self.var_ct.set(bool(cfg.get("ct_enabled", False)))
            if "ct_offset" in cfg:
                self.entry_ct_offset.delete(0, tk.END); self.entry_ct_offset.insert(0, str(cfg.get("ct_offset", 60)))
            if "ct_use_pc_time" in cfg:
                self.var_ct_use_pc.set(bool(cfg.get("ct_use_pc_time", True)))
            if "ct_manual" in cfg:
                self.entry_ct_manual.delete(0, tk.END); self.entry_ct_manual.insert(0, cfg.get("ct_manual", ""))
            if "ct_omit_offset" in cfg:
                self.var_ct_omit.set(bool(cfg.get("ct_omit_offset", False)))
            if "ct_tp" in cfg:
                self.var_tp.set(bool(cfg.get("ct_tp", False)))
            if "ct_ta" in cfg:
                self.var_ta.set(bool(cfg.get("ct_ta", False)))
            self._on_ct_use_pc_changed()
            self.save_config()
            self.status_var.set("Config updated from web UI and saved ✅")
        except Exception as e:
            self.status_var.set(f"Apply remote config failed: {e}")

    def _try_auto_start(self):
        if not self.ser.is_open:
            port = self.combo_ports.get()
            if not port:
                vals = list(self.combo_ports['values'])
                if vals:
                    port = vals[0]
            if port:
                try:
                    self.ser.port = port
                    self.ser.open()
                    self._set_led_color("green")
                    self.btn_open.config(text="Close Serial Port")
                except Exception as e:
                    self.status_var.set(f"Auto-open port failed: {e}")
        if self.ser.is_open:
            self.root.after(200, self.start_sending)
        else:
            self.status_var.set("Auto-start: no port opened.")

    def _on_close(self):
        self.save_config()
        try:
            self.stop_sending()
        except Exception:
            pass
        try:
            acquired = self.serial_write_lock.acquire(timeout=2)
            try:
                if self.ser.is_open:
                    self.ser.close()
            finally:
                if acquired:
                    self.serial_write_lock.release()
        except Exception:
            pass
        self.root.destroy()

# ----------------- main -----------------
def main():
    parser = argparse.ArgumentParser(description="RDS control (tkinter + Flask) with CT, TP and TA")
    parser.add_argument("start", nargs="?", help="use 'start' to autostart sending", default=None)
    parser.add_argument("--port", type=int, help="HTTP server port (default from config or 8080)", default=None)
    args = parser.parse_args()

    autostart = (args.start == "start")
    command_queue = queue.Queue()
    status_queue = queue.Queue()

    def get_cfg_snapshot():
        cfg = {
            "port": "",
            "baud": 9600,
            "pty_index": 1,
            "pi": "C121",
            "ps": "GD-2015",
            "rt": "GD-2015 FM Transmitter! Aliexpress: http://chinabroadcast.aliexpress.com/",
            "rt_address": "2",
            "debug": False,
            "save_log": False,
            "log_filename": "rds_debug.log",
            "server_port": args.port or DEFAULT_HTTP_PORT,
            "ct_enabled": False,
            "ct_offset": 120,
            "ct_use_pc_time": True,
            "ct_manual": "",
            "ct_omit_offset": False,
            "ct_tp": False,
            "ct_ta": False
        }
        if os.path.exists(CONFIG_FILE):
            try:
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    cfg_file = json.load(f)
                cfg.update(cfg_file)
            except Exception:
                pass
        return cfg

    app_flask = create_flask_app(command_queue, status_queue, get_cfg_snapshot)
    flask_port = args.port or get_cfg_snapshot().get("server_port", DEFAULT_HTTP_PORT)

    def run_flask():
        try:
            app_flask.run(host="0.0.0.0", port=int(flask_port), debug=False, use_reloader=False)
        except Exception as e:
            status_queue.put(("status", f"Flask server failed to start on port {flask_port}: {e}"))

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    root = tk.Tk()
    app = App(root, command_queue, status_queue, autostart=autostart, http_port=flask_port)
    root.mainloop()

if __name__ == "__main__":
    main()
