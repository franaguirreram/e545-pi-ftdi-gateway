#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E-517 / E-545 — Test de repetibilidad: WAV_RAMP (WGC) vs WAV_LIN + MOV

Compara dos formas de repetir un desplazamiento de RANGO COMPLETO
(0 -> 200 µm, el rango físico del eje A según qTMN/qTMX medidos en
E517_diagnostico.py) N_REPS veces:

  A) WAV_RAMP: un ciclo completo (ida+vuelta) por wave generator, con
     speedupdown para suavizar los extremos, repetido N_REPS veces
     (una llamada a WGO por repetición, cada vez leyendo el ciclo
     completo grabado por el data recorder).
  B) WAV_LIN: solo la ida (0->200) por wave generator (con speedupdown),
     y la vuelta (200->0) con un MOV normal (sin wave table), también
     repetido N_REPS veces.

Hipótesis a probar (usuario, 2026-08-28): un MOV directo, sin pasar por
la wave table, podría ser más rápido que la rampa de muchos puntos. Este
script mide el tiempo real de cada tramo en ambos métodos para comparar,
y además compara la repetibilidad: qué tan consistente es la posición
final entre repeticiones, en cada método.

[PENDIENTE] Primera vez que se prueba el rango físico completo (200 µm)
-- todo el trabajo anterior fue a escala de cientos de nm. WTR/velocidad
acá son un primer intento conservador (~50 µm/s), no algo optimizado.
Si el error de tracking sale muy grande o el movimiento se ve raro,
bajar la velocidad (subir WTR) antes de sacar conclusiones.

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


# %% -- Parámetros ------------------------------------------------
RANGO_MIN, RANGO_MAX = 0.0, 200.0   # [MEDIDO — qTMN/qTMX, E517_diagnostico.py]
T_SERVO_US = 40.0                   # [MEDIDO — SPA 0x0E000200]

N_REPS = 5        # [ELEGIDO] repeticiones por método; subir una vez visto el timing real

N_LEG  = 100       # puntos de wave table para UN tramo (0->200 o 200->0)
WTR    = 1000      # [ELEGIDO] 1000 × 40µs = 40 ms/punto → ida sola en 4 s (~50 µm/s)
SPEEDUPDOWN = N_LEG // 5   # 20%, mismo criterio que E517_ida_vuelta_speedupdown.py

# RTR: hay que quedar por debajo de 8192 muestras grabadas por lectura.
# ciclo completo (A) = 2*N_LEG puntos de wave; tramo simple (B) = N_LEG.
RTR_VAL = 25       # [ELEGIDO] con esto: ciclo completo A = 8000 muestras (< 8192)

VEL_MOV = 1000.0   # [ELEGIDO] alta a propósito — no le imponemos un techo de
                    # velocidad al MOV de la opción B, para que compita a su
                    # velocidad "natural" contra la rampa de la opción A.

TABLA_X = WGEN_X = 1
AXIS_X  = 'A'

n_leer_leg   = N_LEG * WTR // RTR_VAL
n_leer_ciclo = 2 * N_LEG * WTR // RTR_VAL
assert n_leer_ciclo <= 8192, "Ajustar RTR_VAL: el ciclo completo se pasa del límite de 8192 muestras"

print(f"[timing estimado] ida sola ≈ {N_LEG*WTR*T_SERVO_US/1000:.0f} ms, "
      f"ciclo completo ≈ {2*N_LEG*WTR*T_SERVO_US/1000:.0f} ms")
print(f"[muestras por lectura] tramo={n_leer_leg}, ciclo={n_leer_ciclo} (límite 8192)")


# %% -- Conexión y estado inicial --------------------------------
pidevice = GCSDevice('E-517', gateway=PIFtdiGateway())
print(f"[conexión] {pidevice.qIDN().strip()}")

pidevice.ONL([1, 2, 3], [1, 1, 1])
pidevice.SVO([AXIS_X], [True])
pidevice.DCO([AXIS_X], [False])
pidevice.VCO([AXIS_X], [False])
pidevice.VEL([AXIS_X], [VEL_MOV])


# %% -- Helpers ----------------------------------------------------
def _leer_array_gcs(pidevice, timeout=15.0):
    """qGWD/qDRR son asíncronos -- ver E517_ida_vuelta.py para el detalle."""
    t0 = time.time()
    while pidevice.bufstate is not True:
        if time.time() - t0 > timeout:
            raise TimeoutError("bufstate no llegó a True")
        time.sleep(0.005)
    return np.array(pidevice.bufdata[0])


def _ir_y_asentar(pidevice, pos, t_asentamiento=0.3):
    pidevice.MOV([AXIS_X], [pos])
    pitools.waitontarget(pidevice, [AXIS_X], timeout=15)
    time.sleep(t_asentamiento)


# %% -- Opción A: WAV_RAMP repetido (ida+vuelta por ciclo) --------
print("\n=== Opción A: WAV_RAMP ===")
_ir_y_asentar(pidevice, RANGO_MIN)

pidevice.WCL(TABLA_X)
pidevice.WAV_RAMP(
    table=TABLA_X, firstpoint=1, numpoints=2 * N_LEG, append='X',
    center=N_LEG, speedupdown=SPEEDUPDOWN,
    amplitude=(RANGO_MAX - RANGO_MIN), offset=RANGO_MIN, seglength=2 * N_LEG,
)
pidevice.WTR(WGEN_X, WTR, 0)
pidevice.WGC(WGEN_X, 1)
pidevice.WOS(WGEN_X, 0.0)
pidevice.DRC(tables=[1, 2, 3], sources=[AXIS_X, AXIS_X, AXIS_X], options=[1, 2, 3])
pidevice.RTR(RTR_VAL)

datos_A = []
tiempos_A = []
posfinal_A = []
for rep in range(N_REPS):
    t0 = time.time()
    pidevice.WGO(WGEN_X, 1)
    pitools.waitonwavegen(pidevice, wavegens=WGEN_X, timeout=15)
    pidevice.WGO(WGEN_X, 0)
    dt = time.time() - t0
    tiempos_A.append(dt)

    pidevice.qDRR(1, 1, n_leer_ciclo)
    target = _leer_array_gcs(pidevice)
    pidevice.qDRR(2, 1, n_leer_ciclo)
    current = _leer_array_gcs(pidevice)
    pidevice.qDRR(3, 1, n_leer_ciclo)
    error = _leer_array_gcs(pidevice)
    t_ms = np.arange(len(current)) * (RTR_VAL * T_SERVO_US / 1000)

    pos_final = pidevice.qPOS(AXIS_X)[AXIS_X]
    posfinal_A.append(pos_final)
    print(f"  [A] rep {rep+1}/{N_REPS}: {dt*1000:.1f} ms, pos_final={pos_final:.4f} µm")

    for tt, ta, cu, er in zip(t_ms, target, current, error):
        datos_A.append({'rep': rep, 't_ms': tt, 'target_um': ta, 'current_um': cu, 'error_um': er})

    time.sleep(0.2)  # el ciclo ya vuelve solo a RANGO_MIN; solo un respiro antes de la próxima

df_A = pd.DataFrame(datos_A)


# %% -- Opción B: WAV_LIN (ida) + MOV (vuelta) ---------------------
print("\n=== Opción B: WAV_LIN + MOV ===")
_ir_y_asentar(pidevice, RANGO_MIN)

pidevice.WCL(TABLA_X)
pidevice.WAV_LIN(
    table=TABLA_X, firstpoint=1, numpoints=N_LEG, append='X',
    speedupdown=SPEEDUPDOWN, amplitude=(RANGO_MAX - RANGO_MIN),
    offset=RANGO_MIN, seglength=N_LEG,
)
pidevice.WTR(WGEN_X, WTR, 0)
pidevice.WGC(WGEN_X, 1)
pidevice.WOS(WGEN_X, 0.0)
pidevice.DRC(tables=[1, 2, 3], sources=[AXIS_X, AXIS_X, AXIS_X], options=[1, 2, 3])
pidevice.RTR(RTR_VAL)

datos_B = []
tiempos_B_ida = []
tiempos_B_vuelta = []
posfinal_B = []
for rep in range(N_REPS):
    # -- ida: WAV_LIN --
    t0 = time.time()
    pidevice.WGO(WGEN_X, 1)
    pitools.waitonwavegen(pidevice, wavegens=WGEN_X, timeout=15)
    pidevice.WGO(WGEN_X, 0)
    dt_ida = time.time() - t0
    tiempos_B_ida.append(dt_ida)

    pidevice.qDRR(1, 1, n_leer_leg)
    target = _leer_array_gcs(pidevice)
    pidevice.qDRR(2, 1, n_leer_leg)
    current = _leer_array_gcs(pidevice)
    pidevice.qDRR(3, 1, n_leer_leg)
    error = _leer_array_gcs(pidevice)
    t_ms = np.arange(len(current)) * (RTR_VAL * T_SERVO_US / 1000)
    for tt, ta, cu, er in zip(t_ms, target, current, error):
        datos_B.append({'rep': rep, 'tramo': 'ida', 't_ms': tt,
                         'target_um': ta, 'current_um': cu, 'error_um': er})

    # -- vuelta: MOV directo, sin wave table --
    t0 = time.time()
    pidevice.MOV([AXIS_X], [RANGO_MIN])
    pitools.waitontarget(pidevice, [AXIS_X], timeout=15)
    dt_vuelta = time.time() - t0
    tiempos_B_vuelta.append(dt_vuelta)

    pos_final = pidevice.qPOS(AXIS_X)[AXIS_X]
    posfinal_B.append(pos_final)
    print(f"  [B] rep {rep+1}/{N_REPS}: ida={dt_ida*1000:.1f} ms, "
          f"vuelta={dt_vuelta*1000:.1f} ms, pos_final={pos_final:.4f} µm")

    time.sleep(0.2)

df_B = pd.DataFrame(datos_B)


# %% -- Comparación numérica ----------------------------------------
tA = np.array(tiempos_A)
tB_total = np.array(tiempos_B_ida) + np.array(tiempos_B_vuelta)

print("\n=== Comparación ===")
print(f"[A: WAV_RAMP]    ciclo completo: {tA.mean()*1000:.1f} ± {tA.std()*1000:.1f} ms")
print(f"[B: WAV_LIN+MOV] ida+vuelta:     {tB_total.mean()*1000:.1f} ± {tB_total.std()*1000:.1f} ms "
      f"(ida {np.mean(tiempos_B_ida)*1000:.1f} ms + vuelta {np.mean(tiempos_B_vuelta)*1000:.1f} ms)")
print(f"[repetibilidad A] std posición final: {np.std(posfinal_A)*1000:.2f} nm")
print(f"[repetibilidad B] std posición final: {np.std(posfinal_B)*1000:.2f} nm")


# %% -- Guardar los datos crudos ------------------------------------
timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
nombre_base = f"E517_repetibilidad_{timestamp}"

df_A.to_csv(DATOS_RAW / f"{nombre_base}_A_wavramp.csv", index=False)
df_B.to_csv(DATOS_RAW / f"{nombre_base}_B_wavlin_mov.csv", index=False)

with open(DATOS_METADATA / f"{nombre_base}_metadata.txt", 'w') as f:
    f.write(f"E517_repetibilidad.py -- {timestamp}\n")
    f.write(f"RANGO_MIN={RANGO_MIN} RANGO_MAX={RANGO_MAX}\n")
    f.write(f"N_REPS={N_REPS} N_LEG={N_LEG} WTR={WTR} RTR_VAL={RTR_VAL} SPEEDUPDOWN={SPEEDUPDOWN}\n")
    f.write(f"VEL_MOV={VEL_MOV}\n")
    f.write(f"tiempos_A_ciclo_ms={[round(x*1000,1) for x in tiempos_A]}\n")
    f.write(f"tiempos_B_ida_ms={[round(x*1000,1) for x in tiempos_B_ida]}\n")
    f.write(f"tiempos_B_vuelta_ms={[round(x*1000,1) for x in tiempos_B_vuelta]}\n")
    f.write(f"posfinal_A_um={posfinal_A}\n")
    f.write(f"posfinal_B_um={posfinal_B}\n")

print(f"\n[guardado] {nombre_base}_A_wavramp.csv + _B_wavlin_mov.csv + _metadata.txt")


# %% -- Volver al origen y cerrar ------------------------------------
_ir_y_asentar(pidevice, RANGO_MIN, t_asentamiento=0)
print(f"[cierre] posición final {dict(pidevice.qPOS())}")
cleanup_gcsdevice(pidevice)


# %% -- Gráficos --------------------------------------------
plt.rcParams.update({
    'font.family':   'serif',
    'font.size':     12,
    'axes.labelsize': 14,
    'xtick.labelsize': 12,
    'ytick.labelsize': 12,
    'figure.dpi':    120,
})

fig, axes = plt.subplots(2, 2, figsize=(11, 7))

# Repetibilidad A: todas las repeticiones superpuestas
for rep in range(N_REPS):
    sub = df_A[df_A['rep'] == rep]
    axes[0, 0].plot(sub['t_ms'], sub['current_um'], lw=0.7, alpha=0.7)
axes[0, 0].set_title('A) WAV_RAMP — repeticiones superpuestas')
axes[0, 0].set_xlabel('t [ms]')
axes[0, 0].set_ylabel('posición real [µm]')

# Repetibilidad B (solo la ida, tramo WAV_LIN): todas las repeticiones superpuestas
for rep in range(N_REPS):
    sub = df_B[(df_B['rep'] == rep) & (df_B['tramo'] == 'ida')]
    axes[0, 1].plot(sub['t_ms'], sub['current_um'], lw=0.7, alpha=0.7)
axes[0, 1].set_title('B) WAV_LIN (ida) — repeticiones superpuestas')
axes[0, 1].set_xlabel('t [ms]')
axes[0, 1].set_ylabel('posición real [µm]')

# Comparación de tiempos
axes[1, 0].bar(['A: WAV_RAMP\n(ciclo)', 'B: WAV_LIN+MOV\n(ida+vuelta)'],
                [tA.mean() * 1000, tB_total.mean() * 1000],
                yerr=[tA.std() * 1000, tB_total.std() * 1000],
                color=['#1f77b4', '#d62728'], capsize=5)
axes[1, 0].set_ylabel('duración por repetición [ms]')
axes[1, 0].set_title('Tiempo total por repetición')

# Repetibilidad de la posición final
axes[1, 1].scatter(range(N_REPS), (np.array(posfinal_A) - RANGO_MIN) * 1000,
                    label=f'A (std={np.std(posfinal_A)*1000:.1f} nm)', color='#1f77b4')
axes[1, 1].scatter(range(N_REPS), (np.array(posfinal_B) - RANGO_MIN) * 1000,
                    label=f'B (std={np.std(posfinal_B)*1000:.1f} nm)', color='#d62728')
axes[1, 1].set_xlabel('repetición #')
axes[1, 1].set_ylabel('posición final − RANGO_MIN [nm]')
axes[1, 1].set_title('Repetibilidad de la posición final')
axes[1, 1].legend(frameon=False, fontsize=9)

plt.tight_layout()
plt.savefig(RESULTADOS_FIGURAS / f"{nombre_base}.pdf", bbox_inches='tight', dpi=200)
plt.show()

print(f"\n[fin] gráfico guardado como {nombre_base}.pdf")
