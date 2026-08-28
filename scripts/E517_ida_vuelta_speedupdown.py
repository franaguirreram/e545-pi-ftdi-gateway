#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E-517 / E-545 — Ida y vuelta CON rampa suavizada (WAV_LIN + speedupdown)

Variante de E517_ida_vuelta.py: en vez de cargar la trayectoria como una
lista de puntos crudos (WAV_PNT/writewavepoints, rampa perfectamente
recta, con una esquina filosa en el pico donde la velocidad cambia de
signo instantáneamente), acá se cargan DOS segmentos con WAV_LIN (ida y
vuelta), usando el parámetro 'speedupdown' para que el propio controlador
suavice la aceleración/desaceleración en los extremos de cada tramo.

[MEDIDO 2026-08-28] Dos intentos, con hardware real de por medio:

1) Primer intento: dos llamadas a WAV_LIN (una para la ida, otra para la
   vuelta con append='&' y amplitude NEGATIVA para que baje). Se cargó
   sin error, pero al leer la wave table cargada con qGWD, TODA la
   "vuelta" quedó plana en el valor del pico -- una amplitud negativa en
   WAV_LIN no genera una rampa descendente en este firmware, la ignora.
   Confirmado leyendo los 200 puntos completos, no fue un error de
   lectura parcial.

2) Se encontró (mirando qué otras funciones de "wave" ofrece pipython)
   WAV_RAMP: una función dedicada a ESTO -- una rampa simétrica completa
   (sube y baja) en una sola llamada, con un parámetro 'center' que marca
   dónde está el pico. Probado con probe_wav_ramp.py (offset=0,
   amplitude=10, N=40, center=20, speedupdown=4): sube suave de 0 a 10
   (pico exacto en el índice esperado), y baja suave y simétrica de
   vuelta cerca de 0 -- funciona tal cual se esperaba, sin el problema de
   la amplitud negativa. Este script usa WAV_RAMP, no dos WAV_LIN.

SPEEDUPDOWN es un primer valor de prueba (20% de los puntos totales) --
pensado para comparar contra la versión sin suavizar, no como un valor
ya optimizado.

Uso en Spyder: apuntar el intérprete a ~/python-envs/pi/bin/python3.14,
correr por celdas (#%%) con Ctrl+Enter.
"""

# %% -- Imports --------------------------------------------------
from pipython import GCSDevice, pitools
from pi_ftdi_gateway import PIFtdiGateway, cleanup_gcsdevice
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATOS_RAW = REPO_ROOT / "datos" / "raw"
DATOS_METADATA = REPO_ROOT / "datos" / "metadata"
RESULTADOS_FIGURAS = REPO_ROOT / "resultados" / "figuras"
for _dir in (DATOS_RAW, DATOS_METADATA, RESULTADOS_FIGURAS):
    _dir.mkdir(parents=True, exist_ok=True)


# %% -- Parámetros del experimento -------------------------------
CENTRO_X    = 100.0   # µm, centro del scan
CENTRO_Y    = 100.0   # µm, Y queda quieto acá
AMPLITUD    = 0.5     # µm, semi-amplitud (igual que la corrida sin suavizar, para comparar)

T_SERVO_US  = 40.0    # [MEDIDO — E517_diagnostico.py, SPA 0x0E000200]
WTR         = 25      # ciclos de servo por punto → 1 ms/punto
DWELL_US    = T_SERVO_US * WTR

N_IDA       = 100
N_VUELTA    = 100
N_TOTAL     = N_IDA + N_VUELTA
DURACION_MS = N_TOTAL * DWELL_US / 1000

# [ELEGIDO — primer valor a probar] puntos de aceleración/desaceleración
# en cada extremo de cada segmento, como fracción de sus puntos totales.
SPEEDUPDOWN = N_IDA // 5   # 20% -> 20 puntos de 100

print(f"[timing] dwell/punto = {DWELL_US:.0f} µs, N_total = {N_TOTAL}, "
      f"duración = {DURACION_MS:.2f} ms, speedupdown = {SPEEDUPDOWN}")

TABLA_X     = 1
WGEN_X      = 1
AXIS_X      = 'A'


# %% -- Conexión y estado inicial --------------------------------
pidevice = GCSDevice('E-517', gateway=PIFtdiGateway())
print(f"[conexión] {pidevice.qIDN().strip()}")

pidevice.ONL([1, 2, 3], [1, 1, 1])
pidevice.SVO(['A', 'B'], [True, True])

# Mismo criterio que E517_ida_vuelta.py: sin compensación de deriva ni
# control de velocidad activo durante el wave generator.
pidevice.DCO(['A', 'B'], [False, False])
print(pidevice.qDCO())
pidevice.VCO(['A', 'B'], [False, False])
print(pidevice.qVCO())


# %% -- Punto de partida y de llegada de cada segmento ------------
# No se arma un array de trayectoria en Python -- WAV_LIN calcula la
# rampa (y su suavizado) del lado del controlador. Solo necesitamos los
# extremos.
inicio_ida    = CENTRO_X - AMPLITUD
pico          = CENTRO_X + AMPLITUD
fin_vuelta    = CENTRO_X - AMPLITUD   # vuelve al mismo punto que el inicio

print(f"[trayectoria] ida: {inicio_ida:.4f} -> {pico:.4f} µm, "
      f"vuelta: {pico:.4f} -> {fin_vuelta:.4f} µm "
      f"(excursión: {2*AMPLITUD*1000:.0f} nm)")


# %% -- Ir al punto de partida y asentar --------------------------
# Mismo criterio que E517_ida_vuelta.py: arrancar la wave sobre un sistema
# ya quieto, sin el escalón inicial de saltar directo desde el centro.
pidevice.MOV(['A', 'B'], [inicio_ida, CENTRO_Y])
pitools.waitontarget(pidevice, ['A', 'B'], timeout=10)

T_ASENTAMIENTO_S = 0.5
time.sleep(T_ASENTAMIENTO_S)
print(f"[posición inicial + asentamiento {T_ASENTAMIENTO_S}s] {dict(pidevice.qPOS())}")


# %% -- Helper: leer un array GCS asíncrono (qGWD/qDRR) --------------
def _leer_array_gcs(pidevice, timeout=10.0):
    t0 = time.time()
    while pidevice.bufstate is not True:
        if time.time() - t0 > timeout:
            raise TimeoutError("bufstate no llegó a True")
        time.sleep(0.005)
    return np.array(pidevice.bufdata[0])


# %% -- Cargar la wave table: UN solo WAV_RAMP (sube y baja) -----------
# WAV_RAMP genera la rampa simétrica completa (ida + vuelta) en una sola
# llamada -- ver docstring de este archivo para por qué no se usan dos
# WAV_LIN (la amplitud negativa para la "vuelta" no funcionó).
pidevice.WCL(TABLA_X)

pidevice.WAV_RAMP(
    table=TABLA_X, firstpoint=1, numpoints=N_TOTAL, append='X',
    center=N_TOTAL // 2, speedupdown=SPEEDUPDOWN,
    amplitude=(pico - inicio_ida), offset=inicio_ida, seglength=N_TOTAL,
)

# Verificar leyendo la tabla completa -- en particular, confirmar que el
# pico cae donde se espera y que vuelve cerca del valor inicial.
pidevice.qGWD(TABLA_X, 1, N_TOTAL)
wave_cargada = _leer_array_gcs(pidevice)

print(f"[carga] {N_TOTAL} puntos cargados con WAV_RAMP (1 llamada)")
print(f"[wave] min={wave_cargada.min():.4f} max={wave_cargada.max():.4f} µm "
      f"(nominal: {inicio_ida:.4f}..{pico:.4f}), pico en índice {wave_cargada.argmax()+1} "
      f"(esperado ~{N_TOTAL//2})")
print(f"[wave] primer punto={wave_cargada[0]:.4f}, último punto={wave_cargada[-1]:.4f} "
      f"(nominal ambos: {inicio_ida:.4f})")


# %% -- Configurar reproducción ---------------------------------
pidevice.WTR(WGEN_X, WTR, 0)
pidevice.WGC(WGEN_X, 1)
pidevice.WOS(WGEN_X, 0.0)
print(f"[wave] WTR={WTR}, WGC=1, WOS=0.0, speedupdown={SPEEDUPDOWN}")


# %% -- Configurar el data recorder ------------------------------
pidevice.DRC(tables=[1, 2, 3], sources=['A', 'A', 'A'], options=[1, 2, 3])
print(f"[DRC] {dict(pidevice.qDRC())}")

RTR_VAL = 1
pidevice.RTR(RTR_VAL)
print(f"[RTR] {pidevice.qRTR()} → dt_grabación = {RTR_VAL*T_SERVO_US:.0f} µs")


# %% -- DISPARO ----------------------------------------------
print("\n[GO] disparando wave generator...")
t_start = time.time()
pidevice.WGO(WGEN_X, 1)
pitools.waitonwavegen(pidevice, wavegens=WGEN_X, timeout=10)
pidevice.WGO(WGEN_X, 0)
print(f"[GO] duración medida en host: {(time.time()-t_start)*1000:.1f} ms "
      f"(esperado ≈ {DURACION_MS:.1f} ms)")


# %% -- Leer datos grabados ----------------------------------------
# Mismo cuidado que E517_ida_vuelta.py: WTR != RTR, hay que pedir
# N_TOTAL × WTR muestras para cubrir la secuencia completa.
n_leer = N_TOTAL * WTR

pidevice.qDRR(1, 1, n_leer)
target = _leer_array_gcs(pidevice)

pidevice.qDRR(2, 1, n_leer)
current = _leer_array_gcs(pidevice)

pidevice.qDRR(3, 1, n_leer)
error = _leer_array_gcs(pidevice)

t_ms = np.arange(len(current)) * (T_SERVO_US / 1000)

print(f"[data] {len(current)} muestras leídas; span temporal = {t_ms[-1]:.2f} ms")
print(f"[error tracking] max = {np.max(np.abs(error))*1000:.2f} nm, "
      f"RMS = {np.sqrt(np.mean(error**2))*1000:.2f} nm")


# %% -- Guardar los datos crudos (para analizar después) ----------
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
nombre_base = f"E517_ida_vuelta_speedupdown_{timestamp}"

df = pd.DataFrame({
    't_ms': t_ms,
    'target_um': target,
    'current_um': current,
    'error_um': error,
})
df.to_csv(DATOS_RAW / f"{nombre_base}.csv", index=False)

with open(DATOS_METADATA / f"{nombre_base}_metadata.txt", 'w') as f:
    f.write(f"E517_ida_vuelta_speedupdown.py -- {timestamp}\n")
    f.write(f"CENTRO_X={CENTRO_X} CENTRO_Y={CENTRO_Y} AMPLITUD={AMPLITUD}\n")
    f.write(f"T_SERVO_US={T_SERVO_US} WTR={WTR} RTR_VAL={RTR_VAL}\n")
    f.write(f"N_IDA={N_IDA} N_VUELTA={N_VUELTA} N_TOTAL={N_TOTAL}\n")
    f.write(f"SPEEDUPDOWN={SPEEDUPDOWN}\n")
    f.write(f"T_ASENTAMIENTO_S={T_ASENTAMIENTO_S}\n")
    f.write(f"n_leer={n_leer} muestras_leidas={len(current)}\n")
    f.write(f"error_max_nm={np.max(np.abs(error))*1000:.3f}\n")
    f.write(f"error_rms_nm={np.sqrt(np.mean(error**2))*1000:.3f}\n")

print(f"[guardado] {nombre_base}.csv + {nombre_base}_metadata.txt")


# %% -- Volver al centro y cerrar --------------------------------
pidevice.MOV(['A'], [CENTRO_X])
pitools.waitontarget(pidevice, ['A'], timeout=5)
print(f"[cierre] posición final {dict(pidevice.qPOS())}")
cleanup_gcsdevice(pidevice)  # deja la conexión lista para volver a correr el script


# %% -- Gráficos --------------------------------------------
plt.rcParams.update({
    'font.family':   'serif',
    'font.size':     12,
    'axes.labelsize': 14,
    'xtick.labelsize': 12,
    'ytick.labelsize': 12,
    'figure.dpi':    120,
})

fig, axes = plt.subplots(3, 1, figsize=(6.5, 6), sharex=True)

axes[0].plot(t_ms, (target  - CENTRO_X) * 1000, color='#1f77b4', lw=1.2, label='comandada')
axes[0].plot(t_ms, (current - CENTRO_X) * 1000, color='#d62728', lw=0.9, label='real')
axes[0].set_ylabel(r'$x - x_c$ [nm]')
axes[0].legend(frameon=False, loc='upper right')

axes[1].plot(t_ms, error * 1000, color='k', lw=0.8)
axes[1].axhline(0, color='gray', lw=0.5, ls='--')
axes[1].set_ylabel('error [nm]')

axes[2].plot((target - CENTRO_X) * 1000, (current - CENTRO_X) * 1000,
             color='k', lw=0.6, alpha=0.7)
axes[2].plot([-AMPLITUD*1000, AMPLITUD*1000], [-AMPLITUD*1000, AMPLITUD*1000],
             color='gray', lw=0.5, ls='--')
axes[2].set_xlabel('comandada − centro [nm]')
axes[2].set_ylabel('real − centro [nm]')
axes[2].set_aspect('equal')

axes[1].set_xlabel('t [ms]')
plt.tight_layout()
plt.savefig(RESULTADOS_FIGURAS / f"{nombre_base}.pdf", bbox_inches='tight', dpi=200)
plt.show()

print(f"\n[fin] gráfico guardado como {nombre_base}.pdf")
