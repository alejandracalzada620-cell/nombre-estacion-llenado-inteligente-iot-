#!/usr/bin/env python3
"""
Prueba de estres: mide latencia (PING/PONG), perdida de paquetes, JSON invalidos
y reinicios inesperados del Arduino durante N minutos.

IMPORTANTE: el puerto serie solo puede abrirlo un programa a la vez.
Detén puente_serial_mqtt.py antes de correr esta prueba.

Uso:
  python prueba_estres.py --puerto COM3 --minutos 30 --auto
  python prueba_estres.py --puerto COM3 --minutos 1        # prueba rapida
"""
import argparse
import csv
import json
import os
import statistics
import time
from datetime import datetime

import serial


def percentil(valores, p):
    if not valores:
        return 0.0
    v = sorted(valores)
    k = min(len(v) - 1, int(round(p / 100 * (len(v) - 1))))
    return v[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--puerto", required=True)
    ap.add_argument("--baudios", type=int, default=115200)
    ap.add_argument("--minutos", type=float, default=30)
    ap.add_argument("--ping-cada", type=float, default=1.0, help="segundos entre PING")
    ap.add_argument("--auto", action="store_true", help="activa AUTO,1 durante la prueba")
    ap.add_argument("--carpeta", default=os.path.join(os.path.dirname(__file__), "..", "datos"))
    args = ap.parse_args()

    os.makedirs(args.carpeta, exist_ok=True)
    sello = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta_lat = os.path.join(args.carpeta, f"latencia_{sello}.csv")
    ruta_res = os.path.join(args.carpeta, f"resumen_estres_{sello}.txt")

    ser = serial.Serial(args.puerto, args.baudios, timeout=0.05)
    time.sleep(2)
    ser.reset_input_buffer()
    if args.auto:
        ser.write(b"AUTO,1\n")

    fin = time.time() + args.minutos * 60
    prox_ping = time.time()
    ping_id = 0
    enviados = {}          # id -> instante de envio
    latencias = []         # ms
    json_malos = 0
    paquetes = 0
    ids_vistos = []
    perdidos = 0
    reinicios = 0
    ult_id = None
    ult_t = None
    errores_serial = 0
    buf = b""

    print(f"Prueba de {args.minutos} min iniciada. Ctrl+C para abortar.")
    inicio = time.time()
    with open(ruta_lat, "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["ping_id", "latencia_ms"])
        try:
            while time.time() < fin:
                ahora = time.time()
                if ahora >= prox_ping:
                    ping_id += 1
                    enviados[ping_id] = time.perf_counter()
                    try:
                        ser.write(f"PING,{ping_id}\n".encode())
                    except serial.SerialException:
                        errores_serial += 1
                    prox_ping += args.ping_cada

                try:
                    buf += ser.read(256)
                except serial.SerialException:
                    errores_serial += 1
                    time.sleep(0.5)
                    continue

                while b"\n" in buf:
                    linea, buf = buf.split(b"\n", 1)
                    t_rx = time.perf_counter()
                    linea = linea.strip()
                    if not linea:
                        continue
                    try:
                        d = json.loads(linea.decode("utf-8", errors="strict"))
                    except (ValueError, UnicodeDecodeError):
                        json_malos += 1
                        continue

                    if "pong" in d:
                        t0 = enviados.pop(d["pong"], None)
                        if t0 is not None:
                            ms = (t_rx - t0) * 1000
                            latencias.append(ms)
                            wr.writerow([d["pong"], f"{ms:.2f}"])
                    elif "id" in d:
                        paquetes += 1
                        if ult_id is not None:
                            if d["t"] < ult_t:          # millis() reinicio => reset del MCU
                                reinicios += 1
                            elif d["id"] > ult_id + 1:
                                perdidos += d["id"] - ult_id - 1
                        ult_id, ult_t = d["id"], d["t"]
                        ids_vistos.append(d["id"])
        except KeyboardInterrupt:
            print("\nAbortada por el usuario; se resume lo medido.")
    ser.close()

    dur = time.time() - inicio
    sin_resp = len(enviados) - 0
    esperados = int(dur / 0.5)  # T_TELEMETRIA_MS = 500 ms
    lines = [
        "RESUMEN DE PRUEBA DE ESTRES",
        f"Fecha/hora               : {datetime.now().isoformat(timespec='seconds')}",
        f"Duracion real            : {dur/60:.1f} min",
        f"PING enviados            : {ping_id}",
        f"PONG recibidos           : {len(latencias)}",
        f"PING sin respuesta       : {sin_resp}",
        f"Latencia minima (ms)     : {min(latencias):.1f}" if latencias else "Latencia minima (ms)     : n/d",
        f"Latencia promedio (ms)   : {statistics.mean(latencias):.1f}" if latencias else "Latencia promedio (ms)   : n/d",
        f"Latencia p95 (ms)        : {percentil(latencias, 95):.1f}",
        f"Latencia maxima (ms)     : {max(latencias):.1f}" if latencias else "Latencia maxima (ms)     : n/d",
        f"Paquetes telemetria      : {paquetes} (esperados aprox. {esperados})",
        f"Paquetes perdidos (gaps) : {perdidos}",
        f"JSON invalidos           : {json_malos}",
        f"Reinicios del Arduino    : {reinicios}",
        f"Errores de puerto serie  : {errores_serial}",
    ]
    texto = "\n".join(lines)
    print("\n" + texto)
    with open(ruta_res, "w", encoding="utf-8") as f:
        f.write(texto + "\n")
    print(f"\nGuardado: {os.path.abspath(ruta_lat)}\n          {os.path.abspath(ruta_res)}")


if __name__ == "__main__":
    main()
