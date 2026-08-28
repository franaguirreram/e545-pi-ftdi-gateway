# Contexto histórico — driver E-545/E-517 para macOS arm64

Este documento resume, en orden cronológico, el problema y todo el trabajo
hecho para conectar un controlador piezoeléctrico PI E-545 (electrónica
E-517) a macOS arm64 sin la DLL propietaria de Physik Instrumente. Sirve
como base para el README/CONTRIBUTING de un repo GitHub, y como contexto
para retomar el trabajo con otra herramienta (Copilot, otro chat, etc.).

## 1. El problema

Un script de laboratorio (interferómetro de Michelson, barrido de la
platina + lectura de detector) funcionaba en Windows usando `pipython`
(librería oficial de PI) conectado por USB. Al migrar a macOS arm64,
fallaba con:
```
OSError: libpi_pi_gcs2.dylib not found
```
`pipython` necesita esa librería nativa para hablar USB en macOS. **Nunca
existió una build de esa librería para macOS** en el instalador oficial de
PI ("PI Software Suite", ~2.3 GB, orientado a Windows + un agregado para
Linux x86_64). La única librería nativa GCS2 del paquete era un `.so` de
Linux x86_64 — formato ELF, incompatible con macOS (dyld solo carga
Mach-O) sin importar la arquitectura.

## 2. Camino descartado

- Decompilar el `.dll`/`.so` de PI para portarlo: descartado por motivos
  de licencia (reversing de binario propietario).
- Web Serial / notebooks en la nube: sin acceso a USB local; y el
  dispositivo usa un Vendor ID propio de PI (`0x1a72`), no aparece como
  puerto serie estándar del sistema.

## 3. La solución: hablarle directo al chip FTDI

Evidencia: el instalador Linux de PI trae `pi_ftdi_usb-2.3.12-INSTALL...`,
indicio de que el hardware de PI usa chips FTDI reprogramados con VID/PID
propio. Se confirmó inspeccionando los descriptores USB reales
(`bInterfaceClass=0xFF` vendor-specific, 2 endpoints bulk de 64 bytes,
USB 1.1 full-speed — huella típica de un FT232AM/R).

Se armó un gateway propio (`pi_ftdi_gateway`, este paquete) que habla:
- El protocolo FTDI estándar (público, vía `pyftdi`/`libusb`) para el
  transporte USB.
- El set de comandos ASCII GCS2 de PI (`*IDN?`, `MOV`, `SVO`, etc.),
  documentado públicamente por PI.

**No se decompiló ni copió nada del software propietario de PI.**

### Hallazgos técnicos encapsulados en el gateway

- `pyftdi` 0.57.2 identifica el chip como `ft232am` (por `bcdDevice=0x0200`)
  y tiene un bug de división por cero en su cálculo de baudrate "legacy"
  para ciertos valores (115200 incluido) — reimplementado y corregido.
- El controlador requiere DTR y RTS en alto antes de responder.
- Requiere terminador `\r` (no `\n` solo). `pipython` termina sus comandos
  en `\n`; el gateway traduce `\n` → `\r` al escribir.
- El descriptor USB no trae el serial real (`iSerialNumber` vacío); el
  serial GCS2 (`0111176619`) solo se obtiene con `*IDN?` ya conectado.

### Punto de integración con pipython

`pipython` soporta nativamente inyectar un transporte propio:
```python
GCSDevice(devname='E-517', gateway=mi_gateway)
```
donde `mi_gateway` implementa la interfaz `PIGateway` (la misma que
`PISocket`/`PISerial`: `send`, `read`, `flush`, `close`, `connected`,
`timeout`). Esto da acceso a **toda** la API de alto nivel de `pipython`
sin reescribir nada de la capa de comandos.

## 4. Evolución del driver (versiones)

- **v1** (archivo suelto): validado end-to-end contra hardware real,
  incluyendo un movimiento físico real de la platina.
- **v0.2.0** (paquete instalable, `pip install -e .`): mismo código,
  empaquetado con `pyproject.toml`.
- **v0.3.0** (auditoría): sin hardware conectado en esa sesión — buffer de
  lectura 4096→65536 bytes, timeout USB desacoplado del timeout GCS
  (acotado a 500 ms para que un timeout GCS largo, necesario para `qDRR`
  con miles de puntos, no bloquee un único read USB por todo ese tiempo),
  20 tests unitarios contra `Ftdi`/`usb.core` mockeados. Se evaluó y
  **rechazó** deliberadamente agregar un `read_until_prompt()` propio o un
  mecanismo de retry/ACK: ambos ya existen, en la capa correcta
  (`pipython.gcsmessages.GCSMessages`), y agregarlos en el gateway
  hubiera duplicado (y podido desincronizar) esa lógica.
- **v0.3.1**: agregado `CloseConnection()` como alias de `close()` —
  gap real de pipython (específico de `GCSDll`, ausente también en
  `PISerial`/`PISocket`), no algo que "faltaba" en este driver en
  particular.

## 5. Validación contra hardware real (2026-08-27, `E517_diagnostico.py`)

Confirmado contra el E-517 real (serial `0111176619`):
- 3 wave generators, 8192 puntos máx. por wave table, 3 record tables.
- Servo update time: 40 µs (25 kHz).
- Casi todos los comandos GCS2 relevantes tienen wrapper en `pipython`
  (excepciones: `ATS?` sin wrapper, `WAV` setter sin wrapper — usar
  `WAV_LIN`/`WAV_SIN_P`/`WAV_PNT` en su lugar).

## 6. Bugs encontrados y corregidos en los scripts de experimento

**`E517_diagnostico.py`**:
- `WAV_LIN` llamado con menos argumentos de los que pide la firma real de
  pipython (8, no 6) → `TypeError`.
- `CloseConnection()` no existe fuera de `GCSDll` → `AttributeError`;
  usar `pidevice.close()`.
- `qDRR`/`qGWD` son **asíncronos**: la llamada dispara un hilo de fondo y
  devuelve el control enseguida — hay que esperar `pidevice.bufstate is
  True` antes de seguir (si no, ese hilo puede seguir vivo cuando el
  script cierra la conexión, y su chequeo final de `ERR?` falla).

**`E517_ida_vuelta.py`** (bugs más serios, alguno silencioso):
- Wave table cargada con `WAV_LIN` llamado 200 veces (una por punto), con
  argumentos faltantes (crash inmediato). Corregido: `WAV_LIN` genera una
  rampa lineal *completa* en una sola llamada; para trayectorias
  arbitrarias, usar `pitools.writewavepoints()` (usa `WAV_PNT` con
  chunking).
- **Bug silencioso**: `qGWD(...)`/`qDRR(...)` devuelven solo el *header*
  (diccionario), no los datos — los datos reales se leen con
  `pidevice.bufdata` después de esperar `bufstate`. El script original
  trataba el valor de retorno como si fueran los datos — sin tirar
  ningún error, con resultados incorrectos.
- `pitools.waitonwavegenerator(...)` no existe; el nombre real es
  `pitools.waitonwavegen(pidevice, wavegens=...)`.
- **Límite de `bunchsize` medido en hardware real**: un solo comando
  `WAV_PNT` funciona hasta **72 puntos**, falla con timeout desde **73**
  (probablemente el buffer de línea del parser de comandos del
  controlador, ~800 caracteres). `writewavepoints(..., bunchsize=None)`
  manda todo en un solo comando — hay que pasar `bunchsize=50` o menos
  con este equipo.
- **Gotcha de pipython, no del gateway**: `PIGateway` guarda sus
  callbacks de cambio de estado en una lista **compartida a nivel de
  clase** (`_connection_status_changed_callbacks`). Un `pidevice.close()`
  suelto no la desregistra — solo `with GCSDevice(...) as pidevice:`
  (vía `__exit__`) lo hace. Crear varias conexiones en el mismo proceso
  sin `with` acumula callbacks apuntando a gateways ya cerrados, y la
  siguiente conexión exitosa los dispara a todos, produciendo
  `PIFtdiConnectionError` confusos en una conexión que en realidad está
  viva. **Usar siempre `with`.**

## 7. Estado actual

- Gateway funcional y empaquetado, con wave generator y data recorder
  validados contra hardware real (carga de wave table, disparo
  sincronizado, lectura de datos grabados).
- Pendiente: adaptar el script real del experimento
  (`Codigo_michelsonTRES_CHAD.py`, todavía con `ConnectUSB` directo y
  dependencia de `u12`/LabJack específica de Windows) para usar este
  gateway y, eventualmente, wave generator + data recorder en vez del
  barrido punto a punto actual.
