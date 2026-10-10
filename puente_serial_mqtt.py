#!/usr/bin/env python3
"""
Puente Serial <-> MQTT para la Estacion de Llenado (EL-IoT).

Flujo:
  Arduino --(JSON por USB, 115200)--> este script --> MQTT  estacion/telemetria
                                                   --> CSV  datos/telemetria_YYYYmmdd_HHMMSS.csv
  MQTT estacion/cmd --> este script --(texto + '\\n')--> Arduino

Uso:
  python puente_serial_mqtt.py --puerto COM3
  python puente_serial_mqtt.py --puerto /dev/ttyACM0 --broker 192.168.1.50
  python puente_serial_mqtt.py            # autodetecta el puerto del Arduino
"""
import argparse
import csv
import json
import os
import queue
import re
import sys
import time
from datetime import datetime

import serial
import serial.tools.list_ports

try:
    import paho.mqtt.client as mqtt
except ImportError:  # permite usar el puente solo con CSV
    mqtt = None

TOPIC_TELEMETRIA = "estacion/telemetria"
TOPIC_CMD = "estacion/cmd"
TOPIC_PUENTE = "estacion/puente"

# Solo se reenvian al Arduino comandos que cumplan esta lista blanca
CMD_VALIDO = re.compile(r"^(START|STOP|RESET|AUTO,[01]|PING,\d{1,9})$")

CAMPOS = ["ts_pc", "id", "t", "st", "niv", "dist", "temp", "bomba", "estop", "auto", "alm"]


def detectar_puerto():
    """Busca un Arduino original o clon CH340/CP210x/FTDI."""
    for p in serial.tools.list_ports.comports():
        desc = f"{p.description} {p.manufacturer}".lower()
        if p.vid in (0x2341, 0x1A86, 0x10C4, 0x0403) or "arduino" in desc or "ch340" in desc:
            return p.device
    return None


def crear_cliente_mqtt(args, cola_cmd):
    if mqtt is None:
        print("[AVISO] paho-mqtt no instalado: se trabaja solo con CSV.")
        return None
    try:  # paho-mqtt >= 2.0
        cli = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="puente-el-iot")
    except AttributeError:  # paho-mqtt 1.x
        cli = mqtt.Client(client_id="puente-el-iot")

    def on_connect(client, userdata, flags, rc, *extra):
        print(f"[MQTT] conectado a {args.broker}:{args.mqtt_puerto} (rc={rc})")
        client.subscribe(TOPIC_CMD)
        client.publish(TOPIC_PUENTE, "ONLINE", retain=True)

    def on_message(client, userdata, msg):
        cmd = msg.payload.decode("utf-8", errors="ignore").strip().upper()
        if CMD_VALIDO.match(cmd):
            cola_cmd.put(cmd)
        else:
            print(f"[MQTT] comando rechazado: {cmd!r}")

    cli.on_connect = on_connect
    cli.on_message = on_message
    cli.will_set(TOPIC_PUENTE, "OFFLINE", retain=True)
    try:
        cli.connect(args.broker, args.mqtt_puerto, keepalive=30)
        cli.loop_start()
    except OSError as e:
        print(f"[MQTT] no se pudo conectar ({e}). Se continua solo con CSV.")
        return None
    return cli


def abrir_serial(puerto, baudios):
    while True:
        try:
            ser = serial.Serial(puerto, baudios, timeout=1)
            time.sleep(2)  # el Arduino se reinicia al abrir el puerto
            ser.reset_input_buffer()
            print(f"[SERIAL] abierto {puerto} @ {baudios}")
            return ser
        except serial.SerialException as e:
            print(f"[SERIAL] no se pudo abrir {puerto}: {e}. Reintento en 3 s...")
            time.sleep(3)


def main():
    ap = argparse.ArgumentParser(description="Puente Serial-MQTT EL-IoT")
    ap.add_argument("--puerto", default=None, help="COMx o /dev/ttyACMx (autodetecta si se omite)")
    ap.add_argument("--baudios", type=int, default=115200)
    ap.add_argument("--broker", default="localhost")
    ap.add_argument("--mqtt-puerto", type=int, default=1883)
    ap.add_argument("--sin-mqtt", action="store_true", help="solo registrar CSV")
    ap.add_argument("--carpeta", default=os.path.join(os.path.dirname(__file__), "..", "datos"))
    args = ap.parse_args()

    puerto = args.puerto or detectar_puerto()
    if not puerto:
        sys.exit("No se encontro Arduino. Indica el puerto con --puerto COM3")

    os.makedirs(args.carpeta, exist_ok=True)
    nombre = datetime.now().strftime("telemetria_%Y%m%d_%H%M%S.csv")
    ruta_csv = os.path.join(args.carpeta, nombre)
    print(f"[CSV] registrando en {os.path.abspath(ruta_csv)}")

    cola_cmd = queue.Queue()
    cli = None if args.sin_mqtt else crear_cliente_mqtt(args, cola_cmd)
    ser = abrir_serial(puerto, args.baudios)

    n_ok = n_err = 0
    with open(ruta_csv, "w", newline="", encoding="utf-8") as f:
        wr = csv.DictWriter(f, fieldnames=CAMPOS, extrasaction="ignore")
        wr.writeheader()
        try:
            while True:
                # 1) Reenviar comandos pendientes hacia el Arduino
                while not cola_cmd.empty():
                    cmd = cola_cmd.get()
                    ser.write((cmd + "\n").encode())
                    print(f"[CMD] -> Arduino: {cmd}")

                # 2) Leer una linea del Arduino
                try:
                    linea = ser.readline().decode("utf-8", errors="ignore").strip()
                except serial.SerialException:
                    print("[SERIAL] conexion perdida; reconectando...")
                    ser.close()
                    ser = abrir_serial(puerto, args.baudios)
                    continue
                if not linea:
                    continue

                # 3) Validar JSON
                try:
                    datos = json.loads(linea)
                except json.JSONDecodeError:
                    n_err += 1
                    print(f"[JSON] linea invalida ({n_err}): {linea[:60]!r}")
                    continue
                if "pong" in datos:
                    continue  # respuestas de latencia: las usa prueba_estres.py
                if "st" not in datos:
                    continue

                # 4) Enriquecer, guardar y publicar
                datos["ts_pc"] = datetime.now().isoformat(timespec="milliseconds")
                wr.writerow(datos)
                f.flush()
                if cli is not None:
                    cli.publish(TOPIC_TELEMETRIA, json.dumps(datos))
                n_ok += 1
                if n_ok % 20 == 0:
                    print(f"[OK] {n_ok} paquetes | errores JSON: {n_err} | "
                          f"estado={datos['st']} nivel={datos['niv']}% temp={datos['temp']}C")
        except KeyboardInterrupt:
            print("\nFinalizado por el usuario.")
        finally:
            if cli is not None:
                cli.publish(TOPIC_PUENTE, "OFFLINE", retain=True)
                cli.loop_stop()
            ser.close()


if __name__ == "__main__":
    main()
