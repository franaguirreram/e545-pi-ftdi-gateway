# Instrucciones para GitHub Copilot — pi_ftdi_gateway

Este repo es un driver Python para hablar el protocolo GCS2 con un
controlador piezoeléctrico PI E-545 (electrónica E-517) en macOS arm64,
sin la DLL/dylib propietaria de Physik Instrumente (PI nunca la compiló
para esta plataforma). Lee `CONTEXTO_HISTORICO.md` para el detalle
completo de cómo se llegó a esta solución antes de proponer cambios.

## Reglas de oro (no romperlas)

1. **No decompilar ni copiar código de PI.** Todo el protocolo usado acá
   es público: el protocolo FTDI estándar (documentado por FTDI) y el set
   de comandos ASCII GCS2 de PI (documentado en sus manuales, pensado para
   que el cliente lo use). Si una función nueva necesita hablar con el
   hardware de un modo no documentado públicamente, no la implementes sin
   preguntar primero — puede requerir reversing, que este proyecto evita
   deliberadamente.
2. **No dupliques lógica que ya vive en `pipython`.** El paquete oficial
   `pipython` (dependencia de este repo) ya implementa: parseo de fin de
   respuesta GCS (`gcsmessages.eol()`), lectura asíncrona de arrays
   grandes (`qDRR`/`qGWD` vía `bufstate`/`bufdata`, con un hilo de fondo),
   y verificación de errores tras cada comando (`ERR?` automático en cada
   `send()`). Este gateway (`pi_ftdi_gateway/__init__.py`) solo debe
   implementar la interfaz `PIGateway` (`send`, `read`, `flush`, `close`,
   `connected`, `timeout`) al nivel de transporte crudo — devolver bytes
   tal cual llegan, igual que `PISerial`/`PISocket` de pipython. Si te
   piden agregar parseo GCS o reintentos automáticos acá, primero revisá
   si `pipython.pidevice.gcsmessages.GCSMessages` ya lo resuelve.
3. **Usar siempre `with GCSDevice(...) as pidevice:`**, nunca
   `pidevice = GCSDevice(...)` + `pidevice.close()` suelto. Motivo:
   `PIGateway._connection_status_changed_callbacks` es una lista
   compartida a nivel de clase; solo `__exit__`/`__del__` (que dispara
   `with`) la desregistra. Sin `with`, conexiones sucesivas en el mismo
   proceso acumulan callbacks stale y producen errores confusos.
4. **`pidevice.close()`, no `pidevice.CloseConnection()`** salvo que ya
   estés dentro de un `with` (que lo hace solo). `CloseConnection()` es
   específico de la DLL nativa (`GCSDll`); este gateway lo expone como
   alias de compatibilidad, pero no es la forma idiomática.

## Datos medidos en hardware real — no inventar valores nuevos sin volver a medir

- VID/PID: `0x1a72` / `0x1005`. Baudrate GCS2: `115200`, 8N1, requiere
  DTR+RTS altos, terminador `\r` (no `\n` solo).
- 3 wave generators, 8192 puntos máx. por wave table, 3 record tables,
  servo update time 40 µs (25 kHz).
- **`WAV_PNT` / `pitools.writewavepoints`**: máximo medido 72 puntos por
  comando (73 falla con timeout). Usar `bunchsize<=50` con margen — nunca
  `bunchsize=None` con este equipo.
- `qGWD`/`qDRR` son asíncronos: el valor de retorno es solo el header.
  Los datos reales están en `pidevice.bufdata` después de esperar
  `pidevice.bufstate is True`. Esto NO tira error si te olvidás — da
  resultados silenciosamente incorrectos. Revisar siempre este patrón en
  cualquier código nuevo que use `qGWD`/`qDRR`/`qDDL`/`qHIT`.
- **WTR vs RTR — relojes distintos, no confundir la cantidad de muestras a
  pedir con `qDRR`.** El wave generator avanza un punto de la wave table
  cada `WTR` ciclos de servo; el data recorder graba una muestra cada
  `RTR` ciclos de servo. Con `WTR=25, RTR=1` (valores usados en
  `E517_ida_vuelta.py`), la wave tarda 25× más por punto de lo que tarda
  el recorder por muestra — pedir `qDRR(..., numvalues=N_puntos_wave)` (el
  bug real que encontramos el 2026-08-28) solo trae el primer 1/WTR de la
  secuencia grabada. La cantidad correcta de muestras a pedir es
  `N_puntos_wave × WTR` (o, en general,
  `duración_total_us / (RTR × T_servo_us)`), sin exceder 8192.

## Estructura del repo

- `pi_ftdi_gateway/__init__.py` — el driver (transporte USB↔GCS2 crudo).
- `tests/test_pi_ftdi_gateway.py` — tests unitarios contra `Ftdi`/`usb.core`
  mockeados (sin hardware). Nuevas funciones del gateway necesitan tests
  acá siguiendo el mismo patrón de mocks.
- `README.md` — uso, tabla de estado de comandos (mock-only vs. hardware
  real), limitaciones conocidas.
- `CONTEXTO_HISTORICO.md` — la narrativa completa de cómo se llegó a esta
  solución, útil antes de tocar el transporte de bajo nivel.
- `INFORME_AUDITORIA.md` — decisiones tomadas en la revisión v0.3.0,
  incluye por qué se rechazaron ciertos pedidos (parseo GCS propio, retry
  automático) a favor de lo que ya ofrece `pipython`.

## Entorno de desarrollo

Venv de referencia (macOS arm64): `~/python-envs/pi` (Python 3.14).
Reinstalar tras cambios: `pip install -e .` desde la raíz del paquete.
Correr tests: `python -m unittest discover -s tests -v`.
