#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E-517 / E-545 — Diagnóstico inicial vía gateway FTDI (macOS/arm64)

Objetivo: recuperar todos los parámetros del controlador que necesitamos
para diseñar cargas de wave table, timing de scans y grabación de datos.

Todo va a stdout con etiquetas claras. Copiar el output al cuaderno de laboratorio.

Trazabilidad:
    [MEDIDO]     = leído del controlador vía qXXX
    [VERIFICAR]  = interpretación que hay que confirmar contra documentación
    [PENDIENTE]  = no queda claro sin más experimentación

Uso en Spyder:
    Python Interpreter debe apuntar a ~/python-envs/pi/bin/python3.14
    Pegar y ejecutar de arriba a abajo. Sin argumentos.
"""

# %% -- Imports y conexión --------------------------------------------------
import time
from pipython import GCSDevice, pitools
from pi_ftdi_gateway import PIFtdiGateway, cleanup_gcsdevice
import numpy as np

pidevice = GCSDevice('E-517', gateway=PIFtdiGateway())
print("=" * 70)
print("E-517 DIAGNÓSTICO — inicio")
print("=" * 70)


# %% -- Identificación --------------------------------------------------
idn = pidevice.qIDN().strip()
print(f"\n[qIDN]        {idn}")
print(f"[qAXES]       {pidevice.qSAI()}   # ejes disponibles")
print(f"[qVER]        {pidevice.qVER().strip()}   # firmware")


# %% -- Rango físico y home --------------------------------------------
print(f"\n[qDFH]        {dict(pidevice.qDFH())}   # posición home (µm)")
print(f"[qTMN]        {dict(pidevice.qTMN())}   # mín. comandable (µm)")
print(f"[qTMX]        {dict(pidevice.qTMX())}   # máx. comandable (µm)")


# %% -- Estado servo/online --------------------------------------------
print(f"\n[qONL]        {dict(pidevice.qONL())}   # 1 = ONLINE, 0 = OFFLINE")
print(f"[qSVO]        {dict(pidevice.qSVO())}   # 1 = servo cerrado")
print(f"[qDCO]        {dict(pidevice.qDCO())}   # drift compensation")
print(f"[qVCO]        {dict(pidevice.qVCO())}   # velocity control mode")
print(f"[qVEL]        {dict(pidevice.qVEL())}   # µm/s, sólo para MOV")


# %% -- Posición actual --------------------------------------------
print(f"\n[qPOS]        {dict(pidevice.qPOS())}   # posición real (µm)")
print(f"[qMOV]        {dict(pidevice.qMOV())}   # target actual (µm)")


# %% -- Parámetros clave del wave generator y data recorder ---------------
# Servo update time — el reloj interno del controlador.
# [MEDIDO — manual PZ214E p. 165] Nominal 40 µs. Confirmamos:
t_servo_s = float(pidevice.qSPA(1, 0x0E000200)[1][0x0E000200])
print(f"\n[SPA 0x0E000200]  Servo Update Time = {t_servo_s*1e6:.2f} µs "
      f"({1/t_servo_s:.0f} Hz)")

# Cantidad de wave generators y máx. puntos por tabla
n_wg = pidevice.qTWG()
n_wms = pidevice.qWMS()
print(f"[qTWG]        Wave generators disponibles: {n_wg}")
print(f"[qWMS]        Máx. puntos por wave table: {n_wms}")

# Cantidad de record tables
n_rec = pidevice.qTNR()
print(f"[qTNR]        Record tables disponibles: {n_rec}")

# Wave table rate (WTR) actual
print(f"[qWTR]        {dict(pidevice.qWTR())}   # ciclos de servo por punto")

# Record table rate (RTR)
print(f"[qRTR]        {pidevice.qRTR()}   # ciclos de servo por muestra grabada")


# %% -- Configuración actual del data recorder ---------------------------
# Cada entrada es {tabla: [source, option]}
# [MEDIDO — manual PZ214E p. 151] Options: 1=Target, 2=Current, 3=Error,
#                                          7=Control Voltage, 15=Control Output
print(f"\n[qDRC]        {dict(pidevice.qDRC())}")
print("             # option 1=Target, 2=Current, 3=Error, 7=CtrlVolt, 15=CtrlOut")


# %% -- HDR: lista completa de opciones del data recorder ---------------
# Devuelve un texto multilínea con todas las opciones que soporta el firmware.
print("\n[qHDR] — opciones completas del data recorder:")
print(pidevice.qHDR())


# %% -- Última verificación: qué comandos soporta el gateway ---------------
# El gateway FTDI probado sólo con qIDN, ONL, SVO, MOV, qPOS, qVOL.
# Si alguno de los siguientes falla, hay que ajustar el gateway.

def _wait_bufstate(pidevice, timeout=5.0):
    """qGWD/qDRR (y qDDL, qHIT, etc.) son asíncronos: la llamada dispara un
    hilo de fondo que sigue leyendo datos y devuelve el control enseguida.
    Sin esperar bufstate==True, ese hilo puede seguir vivo cuando el script
    sigue adelante (por ejemplo hasta pidevice.close() más abajo), y su
    chequeo final de ERR? se cae si la conexión ya se cerró. Ver docstring
    de qDRR/qGWD en pipython."""
    t0 = time.time()
    while pidevice.bufstate is not True:
        if time.time() - t0 > timeout:
            raise TimeoutError("bufstate no llegó a True")
        time.sleep(0.01)

print("\n[VERIFICAR] Comandos que vamos a necesitar y todavía no están probados:")
for cmd_desc, callable_ in [
    ("WCL   (clear wave table)",           lambda: pidevice.WCL(1)),
    # table=1, firstpoint=1 (1-indexado), numpoints=10, append='X' (desde cero),
    # speedupdown=0, amplitude=10.0, offset=0.0, seglength=10
    ("WAV_LIN (cargar segmento)",          lambda: pidevice.WAV_LIN(1, 1, 10, 'X', 0, 10.0, 0.0, 10)),
    ("qGWD  (leer wave table)",            lambda: (pidevice.qGWD(1, 1, 10), _wait_bufstate(pidevice))),
    ("WGC   (set cycles)",                 lambda: pidevice.WGC(1, 1)),
    ("WTR   (set table rate)",             lambda: pidevice.WTR(1, 1, 0)),
    ("WOS   (set output offset)",          lambda: pidevice.WOS(1, 0.0)),
    ("qHDR  (list record options)",        lambda: pidevice.qHDR()),
    ("qDRR  (read recorded data)",         lambda: (pidevice.qDRR(1, 1, 10), _wait_bufstate(pidevice))),
]:
    try:
        callable_()
        print(f"    OK    {cmd_desc}")
    except Exception as e:
        print(f"    FAIL  {cmd_desc}   -> {type(e).__name__}: {e}")


# %% -- Cierre limpio --------------------------------------------
# cleanup_gcsdevice() (no pidevice.close() suelto) para desregistrar el
# callback de cambio de estado y poder volver a correr el script en la
# misma consola sin reiniciar el kernel. Ver pi_ftdi_gateway/__init__.py.
cleanup_gcsdevice(pidevice)
print("\n" + "=" * 70)
print("DIAGNÓSTICO — fin")
print("=" * 70)
