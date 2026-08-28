# pi-ftdi-gateway

Gateway `PIGateway` (compatible con `pipython`, la librería oficial en Python de
Physik Instrumente) para hablar el protocolo GCS2 con controladores PI **sin**
la DLL/dylib propietaria de PI — probado contra un **E-545 / E-517** en
**macOS arm64**.

`scripts/` tiene los scripts de laboratorio reales que usan este driver
(`E517_diagnostico.py`, `E517_ida_vuelta.py`) — sirven de ejemplo de uso
más allá del snippet mínimo de más abajo. Ver `CONTEXTO_HISTORICO.md` para
cómo se llegó a esta solución, e `INFORME_AUDITORIA.md` para las
decisiones de diseño tomadas en la revisión v0.3.0.

## Por qué existe

PI no distribuye una build de `libpi_pi_gcs2.dylib` para macOS arm64 en su
"PI Software Suite" (orientado a Windows, con un agregado para Linux x86_64).
Sin esa librería, `pipython.GCSDevice.ConnectUSB()` falla con
`OSError: libpi_pi_gcs2.dylib not found`.

Este paquete evita el problema por completo: el controlador usa por dentro un
chip **FTDI** (con Vendor/Product ID propios de PI, `0x1a72`/`0x1005`,
programados sobre silicio FTDI genérico), y ese chip habla un protocolo
**público y documentado** por FTDI. Encima de eso, PI documenta públicamente
el set de comandos ASCII de GCS2 (`*IDN?`, `MOV`, `SVO`, etc.) — pensado
justamente para que el cliente lo use.

Este driver combina ambas cosas (protocolo FTDI abierto + comandos GCS2
públicos) usando `pyftdi`/`pyusb`, sin decompilar ni copiar nada del software
propietario de PI.

## Instalación

```bash
pip install -e .
```

## Uso

```python
from pipython import GCSDevice, pitools
from pi_ftdi_gateway import PIFtdiGateway

with GCSDevice('E-545', gateway=PIFtdiGateway()) as pidevice:
    print(pidevice.qIDN())
    pidevice.ONL([1], [True])
    pidevice.SVO('A', True)
    pidevice.send("MOV A 100.000000\n")
    pitools.waitontarget(pidevice, axes='A', timeout=10.0)
    print(pidevice.qPOS('A'))
```

Si hay más de un controlador PI conectado:

```python
from pi_ftdi_gateway import list_devices, PIFtdiGateway

print(list_devices())  # [{'bus':..., 'address':..., 'idProduct':...}, ...]
gateway = PIFtdiGateway(address=(1, 4))  # bus, address del que querés usar
```

### Cargar una wave table y releerla (WAV_LIN + qGWD)

**Sin validar contra hardware real todavía** (ver "Estado de verificación"
más abajo) — armado siguiendo la firma documentada de `WAV_LIN`/`qGWD` en
pipython, pendiente de confirmar mañana con el E-517 real:

```python
from pipython import GCSDevice
from pi_ftdi_gateway import PIFtdiGateway

with GCSDevice('E-545', gateway=PIFtdiGateway()) as pidevice:
    pidevice.WCL(1)  # limpia la wave table 1 antes de cargar
    pidevice.WAV_LIN(
        table=1, firstpoint=1, numpoints=100, append='X',
        speedupdown=0, amplitude=50.0, offset=0.0, seglength=100,
    )

    pidevice.qGWD(tables=1, offset=1, numvalues=100)
    while pidevice.bufstate is not True:
        pass  # bufstate es progreso 0..1 mientras llega el array por el hilo de fondo
    datos = pidevice.bufdata  # lista de columnas; datos[0] = los 100 puntos de la tabla 1

    assert len(datos[0]) == 100
```

## Estado de verificación

Todo lo que sigue fue chequeado el 2026-08-27 leyendo el código fuente de
`pipython` (qué métodos existen, cómo arman el comando, cómo leen la
respuesta) y con tests unitarios contra un `Ftdi`/`usb.core` **mockeados**
(`tests/test_pi_ftdi_gateway.py`, 20 tests, sin hardware). **Nada de esto
se corrió todavía contra el E-517 real** — el equipo no estaba conectado
durante esa sesión. Ver `INFORME_AUDITORIA.md` para el detalle completo.

| Comando(s) | Wrapper en pipython | Gateway (mock) | Hardware real |
|---|---|---|---|
| `qIDN`, `ONL`/`qONL`, `SVO`/`qSVO`, `MOV`/`qMOV`, `qPOS`, `qVOL`, `pitools.waitontarget` | Sí | ✅ probado | ✅ **probado** (sesión anterior, incluye movimiento físico real) |
| `WCL`, `WAV_LIN`, `WAV_SIN_P`, `qGWD`, `WTR`/`qWTR`, `WGC`/`qWGC`, `WOS`/`qWOS`, `WGO`/`qWGO`, `qWMS`, `qTWG` | Sí (`WAV_LIN`/`WAV_SIN_P` arman internamente un único comando `WAV ... LIN\|SIN_P ...`) | ✅ contrato I/O probado (send/read/timeouts) | ⏳ pendiente |
| `DRC`/`qDRC`, `RTR`/`qRTR`, `qDRR`, `qHDR`, `qTNR` | Sí | ✅ contrato I/O probado, incluida una respuesta simulada de 5000 valores repartida en múltiples paquetes USB | ⏳ pendiente |
| `CTO`/`qCTO`, `TWS`/`qTWS`, `TWC` | Sí | ✅ contrato I/O probado | ⏳ pendiente |
| `IMP`/`qIMP`, `STE`/`qSTE`, `qTAD`, `SPA`/`qSPA`, `qHPA` | Sí | ✅ contrato I/O probado | ⏳ pendiente |
| `DCO`/`qDCO`, `VCO`/`qVCO`, `VEL`/`qVEL`, `qSAI`, `qVER` | Sí | ✅ contrato I/O probado | ✅ **`DCO`/`VCO`/`VEL` probados** (script de diagnóstico), resto ⏳ |
| `ATS?` | **No implementado en pipython** (ningún wrapper) | — | — habría que mandarlo crudo con `pidevice.send("ATS?\n")` |
| `WAV` (setter crudo, sin pasar por `WAV_LIN`/`WAV_SIN_P`) | No tiene wrapper dedicado (solo `qWAV`) | — | — usar `WAV_LIN`/`WAV_SIN_P`/etc., o `pidevice.send(...)` a mano |

"Contrato I/O probado" quiere decir: se validó con mocks que el gateway
manda/recibe bytes correctamente, no trunca respuestas grandes, y maneja
timeouts/desconexión — **no** que se haya verificado la semántica GCS2 de
cada comando puntual contra el firmware real del E-517. Eso solo se sabe
corriendo `qHLP()` contra el equipo (ver conversación) y probando cada
comando en la práctica.

## Alcance / limitaciones conocidas

- Probado contra hardware real únicamente para el subconjunto de arriba
  (conexión, movimiento punto a punto, lectura de posición/voltaje). El
  wave generator y el data recorder están implementados y con tests
  unitarios, pero **sin verificar contra el E-517 real todavía**.
- Debería andar con cualquier controlador PI que use el mismo par VID/PID
  (`0x1a72`/`0x1005`) sobre chip FTDI, pero no está validado contra otros
  modelos.
- El descriptor USB de este equipo no trae el número de serie real (queda
  vacío); el serial de GCS2 solo se puede leer preguntando `*IDN?` una vez
  conectado, no antes.
- **Tamaño de respuesta**: `read()` no tiene un límite propio — cada
  llamada pide hasta 64 KB al buffer USB, y `pipython` (no este gateway)
  se encarga de acumular llamadas sucesivas hasta ver el fin de una
  respuesta GCS (incluidas las de varios miles de líneas, como `qDRR`).
  No hay un techo artificial impuesto acá.
- **Timeout y ráfagas de comandos**: no hay un delay mínimo impuesto entre
  comandos. Por diseño de `pipython`, cada llamada "setter" normal (como
  `WAV_LIN`) ya hace un round-trip completo mandando el comando y
  después leyendo `ERR?` antes de devolver el control — no hace falta (ni
  se agregó) un mecanismo de ACK/retry aparte en este gateway, porque ya
  existe uno por defecto en `pipython` para cada comando. Esto implica que
  una ráfaga de 200-500 `WAV_LIN()` va a tardar la suma de esos 200-500
  round-trips (no son instantáneos), pero no debería perder comandos
  silenciosamente con el uso normal de la API. Si en el script real se usa
  el patrón `pidevice.send(cmd)` crudo (sin chequeo de `ERR?`, como se hizo
  en `Codigo_michelsonTRES_CHAD.py` para evitar timeouts) se pierde esa
  protección a cambio de velocidad — es una decisión del script, no de
  este gateway.
- **`WAV_PNT`/`pitools.writewavepoints` — límite de bunchsize medido en
  hardware real (2026-08-27)**: en el E-517 (serial 0111176619), un solo
  comando `WAV_PNT` funciona hasta **72 puntos**, falla con timeout desde
  **73**. Es un límite del propio controlador (probablemente el buffer de
  línea de su parser de comandos, ~800 caracteres), no del gateway.
  `bunchsize=None` (el default de `writewavepoints`) manda todos los puntos
  en un solo comando — **no usar `None` con este equipo**; usar
  `bunchsize=50` o menos.
- **Gotcha de pipython, no de este gateway**: `GCSDevice`/`PIGateway`
  registran un callback de cambio de estado en una lista **compartida a
  nivel de clase** (`PIGateway._connection_status_changed_callbacks`).
  Un `pidevice.close()` suelto no lo desregistra — solo `__exit__`/`__del__`
  lo hacen. Crear varias conexiones en el mismo proceso (por ejemplo, un
  script de prueba en loop) sin usar `with GCSDevice(...) as pidevice:`
  acumula callbacks apuntando a gateways ya cerrados, y la siguiente
  conexión exitosa los dispara a todos — provoca errores confusos tipo
  `PIFtdiConnectionError: send() llamado sin conexión activa` en una
  conexión que en realidad sí está viva. **Usar siempre `with`.**
- El timeout USB de bajo nivel queda deliberadamente acotado a 500 ms como
  máximo (`_USB_POLL_TIMEOUT_MS`), independiente de qué tan largo sea el
  timeout GCS configurado — así una lectura sin datos todavía no bloquea
  el hilo por todo el timeout GCS de una sola vez. Ver comentarios en el
  código fuente y `INFORME_AUDITORIA.md`.

## Tests

```bash
python -m unittest discover -s tests -v
```

20 tests, todos contra un `Ftdi`/`usb.core` mockeados (sin hardware).
Cubren: detección de dispositivos, errores de conexión (ninguno/ambiguo/
falla de USB), envío y traducción de terminador de línea, timeouts de
lectura sin datos vs. desconexión real, tamaño de lectura, y que una
respuesta grande repartida en múltiples paquetes USB no se trunca ni se
corta al reensamblarla.

## Licencia

MIT.
