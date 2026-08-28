#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E-517 / E-545 — Ida y vuelta simple con wave table + data recorder

Ejecuta UNA trayectoria triangular (ida + vuelta) en el eje X, con el eje Y
mantenido en su posición central. Graba simultáneamente:
    - tabla 1: Target Position   (comandada, opción 1)
    - tabla 2: Current Position  (real, opción 2)
    - tabla 3: Position Error    (opción 3)
Todas del eje A. Al final grafica las tres.

Trazabilidad:
    [MEDIDO]   parámetros verificados del controlador (ver E517_diagnostico.py)
    [ELEGIDO]  parámetros del experimento fijados acá
    [PENDIENTE] cosas que faltan: triggers, integración con ADwin, scan 2D

Escala del experimento:
    Excursión total: 200 nm (± 100 nm alrededor del centro)
    Duración de la ida: ~4 ms
    Duración total (ida + vuelta): ~8 ms

Esto es un ida-vuelta MÍNIMO. Sirve para:
    (a) verificar que el gateway FTDI banca todos los comandos de wave;
    (b) obtener una primera medida de tracking error del stage a esta escala;
    (c) tener una base sobre la cual crecer (agregar triggers, más líneas, 2D).

Uso en Spyder:
    Python Interpreter debe apuntar a ~/python-envs/pi/bin/python3.14.
    Ejecutar por celdas (#%%) con Ctrl+Enter.
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

# [Reorganización 2026-08-28] Rutas fijas al repo, no al directorio de
# trabajo actual -- así da igual desde dónde se corra el script (Spyder
# corre con --wdir en la carpeta del script, no en la raíz del repo).
REPO_ROOT = Path(__file__).resolve().parent.parent
DATOS_RAW = REPO_ROOT / "datos" / "raw"
DATOS_METADATA = REPO_ROOT / "datos" / "metadata"
RESULTADOS_FIGURAS = REPO_ROOT / "resultados" / "figuras"
for _dir in (DATOS_RAW, DATOS_METADATA, RESULTADOS_FIGURAS):
    _dir.mkdir(parents=True, exist_ok=True)


# %% -- Parámetros del experimento -------------------------------
# [ELEGIDO] Excursión chica: 200 nm total en X, en torno al centro del rango
CENTRO_X    = 100.0   # µm, centro del scan (medio del rango 0..200)
CENTRO_Y    = 100.0   # µm, Y queda quieto acá
AMPLITUD    = 0.5   # µm, semi-amplitud 

# [ELEGIDO] Timing
# [MEDIDO — E517_diagnostico.py, SPA 0x0E000200] Servo update time = 40 µs.
T_SERVO_US  = 40.0 
WTR         = 25      # cantidad de ciclos del servo por punto
DWELL_US    = T_SERVO_US * WTR   # → dwell por punto = 25 × 40 µs = 1 ms            

# [ELEGIDO] Puntos por rampa (ida y vuelta simétricas)
N_IDA       = 100     # puntos de la ida
N_VUELTA    = 100     # puntos de la vuelta
N_TOTAL     = N_IDA + N_VUELTA
DURACION_MS = N_TOTAL * DWELL_US / 1000
print(f"[timing] dwell/punto = {DWELL_US:.0f} µs, "
      f"N_total = {N_TOTAL}, duración = {DURACION_MS:.2f} ms")

# Identificadores fijos del E-517 (asignación 1↔A, 2↔B, 3↔C)
TABLA_X     = 1
WGEN_X      = 1
AXIS_X      = 'A'


# %% -- Conexión y estado inicial --------------------------------
pidevice = GCSDevice('E-517', gateway=PIFtdiGateway())
print(f"[conexión] {pidevice.qIDN().strip()}")

# Poner el controlador en modo ONLINE (indispensable para comandos remotos)
pidevice.ONL([1, 2, 3], [1, 1, 1])

# Servo cerrado en X e Y (Z lo dejamos como esté)
pidevice.SVO(['A', 'B'], [True, True])
#pidevice.VEL(['A', 'B'], [100.0, 100.0])   # µm/s, solo para los MOV de acá

# Servo con compensacion de deriva y sin control de velocidad

pidevice.DCO(['A', 'B'], [False, False]) 
print(pidevice.qDCO())
pidevice.VCO(['A', 'B'], [False, False])
print(pidevice.qVCO())

# %% -- Construcción de la trayectoria (Python) ------------------
# Trayectoria triangular en coordenadas absolutas del stage.
# Ida: CENTRO_X → CENTRO_X + AMPLITUD
# Vuelta: CENTRO_X + AMPLITUD → CENTRO_X - AMPLITUD (pasa por el centro, va al otro extremo)
# [DECISIÓN] Elegí una trayectoria simétrica que barra ±AMPLITUD alrededor del centro
#            para no acumular deriva por una sola dirección.

ida    = np.linspace(CENTRO_X - AMPLITUD, CENTRO_X + AMPLITUD, N_IDA)
vuelta = np.linspace(CENTRO_X + AMPLITUD, CENTRO_X - AMPLITUD, N_VUELTA)
trayectoria = np.concatenate([ida, vuelta])
assert len(trayectoria) == N_TOTAL

print(f"[trayectoria] rango físico: {trayectoria.min():.4f} .. "
      f"{trayectoria.max():.4f} µm (excursión: {2*AMPLITUD*1000:.0f} nm)")


# %% -- Ir al punto de partida de la trayectoria y asentar -------
# [DECISIÓN 2026-08-28] Antes íbamos a CENTRO_X con MOV y arrancábamos la
# wave table ahí: como el primer punto de la wave es CENTRO_X-AMPLITUD, el
# "target" pegaba un salto instantáneo de ~100 nm justo al disparar WGO,
# contaminando la "ida" con un transitorio de escalón fresco (visible como
# el pico de error grande al principio del gráfico, y probablemente parte
# de la asimetría ida/vuelta que vimos). Ahora vamos directo al primer
# punto de la wave (trayectoria[0]) y dejamos asentar ANTES de disparar
# WGO — así la wave arranca sobre un sistema ya quieto, sin escalón previo.
pidevice.MOV(['A', 'B'], [float(trayectoria[0]), CENTRO_Y])
pitools.waitontarget(pidevice, ['A', 'B'], timeout=10)

T_ASENTAMIENTO_S = 0.5   # [ELEGIDO] margen generoso; ver E517_diagnostico.py
                          # si se quiere afinar con la dinámica real medida
time.sleep(T_ASENTAMIENTO_S)
print(f"[posición inicial + asentamiento {T_ASENTAMIENTO_S}s] "
      f"{dict(pidevice.qPOS())}")


# %% -- Helper: leer un array GCS asíncrono (qGWD/qDRR) --------------
# qGWD/qDRR devuelven SOLO el header (un diccionario), no los datos.
# Los datos reales los va juntando un hilo de fondo y se leen con
# pidevice.bufdata, esperando a que pidevice.bufstate llegue a True.
# (ver docstring de qDRR/qGWD en pipython — y el problema que tuvimos
# con esto mismo en E517_diagnostico.py). Sin este paso, 'target',
# 'current', 'error' más abajo habrían quedado con el header, no con
# los datos — sin tirar ningún error, silenciosamente mal.
def _leer_array_gcs(pidevice, timeout=10.0):
    t0 = time.time()
    while pidevice.bufstate is not True:
        if time.time() - t0 > timeout:
            raise TimeoutError("bufstate no llegó a True")
        time.sleep(0.005)
    return np.array(pidevice.bufdata[0])


# %% -- Cargar la wave table -----------------------------------------
# WAV_LIN llamado una vez por punto (200 veces) tenía dos problemas:
#  (1) faltaban los argumentos speedupdown/amplitude -> TypeError inmediato.
#  (2) aunque se completaran, WAV_LIN genera UNA rampa lineal completa por
#      llamada (de 'offset' a 'offset+amplitude' en 'numpoints' puntos) —
#      no está pensado para llamarse una vez por punto.
# pitools.writewavepoints() es el helper que pipython ofrece justamente
# para cargar un array arbitrario de puntos en una tabla de una sola vez
# (usa WAV_PNT por debajo, que sí acepta una lista completa de valores).

pidevice.WCL(TABLA_X)
t0 = time.time()
# [MEDIDO 2026-08-27] bunchsize: probado en el E-517 real, funciona hasta
# 72 puntos por comando WAV_PNT, falla (timeout) desde 73 — probablemente
# el buffer de línea del parser de comandos del controlador. 50 deja margen.
pitools.writewavepoints(pidevice, TABLA_X, list(trayectoria), bunchsize=50)
carga_s = time.time() - t0
print(f"[carga] {N_TOTAL} puntos cargados en {carga_s:.2f} s")

# Verificar leyendo la tabla de vuelta (offset 1-indexado; async, hay que
# esperar bufstate antes de que los datos estén disponibles)
pidevice.qGWD(TABLA_X, 1, N_TOTAL)
leida = _leer_array_gcs(pidevice)
err_max = np.max(np.abs(leida - trayectoria))
print(f"[verificación] max |wave_leída - wave_enviada| = {err_max*1000:.3f} nm")
assert err_max < 1e-3, "Discrepancia grande en la wave leída"


# %% -- Configurar reproducción ---------------------------------
# WTR: 25 ciclos de servo por punto → 1 ms/punto
pidevice.WTR(WGEN_X, WTR, 0)

# WGC: 1 ciclo completo de la wave table
pidevice.WGC(WGEN_X, 1)

# WOS: sin offset extra (la wave ya tiene coordenadas absolutas)
pidevice.WOS(WGEN_X, 0.0)

print(f"[wave] WTR={WTR}, WGC=1, WOS=0.0")


# %% -- Configurar el data recorder ------------------------------
# [MEDIDO — manual PZ214E p. 151, opciones DRC]:
#   1 = Target Position   (lo que el controlador manda)
#   2 = Current Position  (lo que el sensor lee)
#   3 = Position Error    (target − current)
#
# Con 3 tablas, gráfica ideal: comandada, real, error, todas del eje X.

pidevice.DRC(
    tables  = [1, 2, 3],
    sources = ['A', 'A', 'A'],
    options = [1, 2, 3],
)
print(f"[DRC] {dict(pidevice.qDRC())}")

# RTR: 1 muestra grabada por cada ciclo de servo → 40 µs por muestra
# (más fino que el wave, que avanza un punto cada WTR=25 ciclos de servo).
RTR_VAL = 1
pidevice.RTR(RTR_VAL)
print(f"[RTR] {pidevice.qRTR()} → dt_grabación = {RTR_VAL*T_SERVO_US:.0f} µs")


# %% -- DISPARO ----------------------------------------------
# WGO 1 1: bit 0 = 1 (start inmediato, sincronizado por servo).
# [MEDIDO — manual PZ214E p. 165] El data recorder arranca AUTOMÁTICAMENTE
#          con WGO.
print("\n[GO] disparando wave generator...")
t_start = time.time()
pidevice.WGO(WGEN_X, 1)

# Esperar a que termine. OJO: la función se llama waitonwavegen (sin "erator")
pitools.waitonwavegen(pidevice, wavegens=WGEN_X, timeout=10)
pidevice.WGO(WGEN_X, 0)
print(f"[GO] duración medida en host: {(time.time()-t_start)*1000:.1f} ms "
      f"(esperado ≈ {DURACION_MS:.1f} ms)")


# %% -- Leer datos grabados ----------------------------------------
# [MEDIDO — manual PZ214E p. 152] qDRR devuelve los últimos datos grabados
# de las tablas indicadas.
#
# OJO: N_TOTAL es la cantidad de PUNTOS DE LA WAVE TABLE, no la cantidad de
# MUESTRAS GRABADAS — son relojes distintos. El wave avanza un punto cada
# WTR=25 ciclos de servo (1 ms/punto), pero el recorder graba cada RTR=1
# ciclo de servo (40 µs/muestra). Pedir n_leer=N_TOTAL (bug encontrado en
# la primera corrida real) solo trae los primeros 200×40µs=8 ms de una
# secuencia que en realidad dura N_TOTAL×WTR×40µs=200 ms — el 4% inicial,
# nunca llega a ver la "vuelta" ni el pico de la "ida". Hay que pedir
# N_TOTAL × WTR muestras para cubrir la secuencia completa.
n_leer = N_TOTAL * WTR   # = 200 × 25 = 5000 muestras (< 8192, entra bien)

# Cada qDRR es asíncrono y comparte el mismo buffer de fondo en pipython —
# hay que drenar (esperar bufstate + leer bufdata) cada uno ANTES de
# disparar el siguiente, si no se pisan entre sí.
pidevice.qDRR(1, 1, n_leer)
target = _leer_array_gcs(pidevice)

pidevice.qDRR(2, 1, n_leer)
current = _leer_array_gcs(pidevice)

pidevice.qDRR(3, 1, n_leer)
error = _leer_array_gcs(pidevice)

# Eje temporal: RTR × T_servo por muestra
t_ms = np.arange(len(current)) * (T_SERVO_US / 1000)   # ms

print(f"[data] {len(current)} muestras leídas; span temporal = {t_ms[-1]:.2f} ms")
print(f"[error tracking] max = {np.max(np.abs(error))*1000:.2f} nm, "
      f"RMS = {np.sqrt(np.mean(error**2))*1000:.2f} nm")


# %% -- Guardar los datos crudos (para analizar después) ----------
# Un CSV con los datos + un .txt con los metadatos del experimento
# (parámetros elegidos y medidos), con timestamp en el nombre para no
# pisar corridas anteriores.
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
nombre_base = f"E517_ida_vuelta_{timestamp}"

df = pd.DataFrame({
    't_ms': t_ms,
    'target_um': target,
    'current_um': current,
    'error_um': error,
})
df.to_csv(DATOS_RAW / f"{nombre_base}.csv", index=False)

with open(DATOS_METADATA / f"{nombre_base}_metadata.txt", 'w') as f:
    f.write(f"E517_ida_vuelta.py -- {timestamp}\n")
    f.write(f"CENTRO_X={CENTRO_X} CENTRO_Y={CENTRO_Y} AMPLITUD={AMPLITUD}\n")
    f.write(f"T_SERVO_US={T_SERVO_US} WTR={WTR} RTR_VAL={RTR_VAL}\n")
    f.write(f"N_IDA={N_IDA} N_VUELTA={N_VUELTA} N_TOTAL={N_TOTAL}\n")
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
# Estilo Nature-like: sin título, CM, tamaños chicos, tres paneles.
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
#plt.savefig(RESULTADOS_FIGURAS / 'E517_ida_vuelta_200nm.svg', bbox_inches='tight', transparent=True)
plt.savefig(RESULTADOS_FIGURAS / 'E517_ida_vuelta_10nm.pdf', bbox_inches='tight', dpi=200)
plt.show()

#print("\n[fin] gráficos guardados como E517_ida_vuelta_200nm_2.{png}")
