# Informe de auditoría — pi_ftdi_gateway 0.2.0 → 0.3.0

Fecha: 2026-08-27. Sin hardware conectado durante esta sesión (`list_devices()`
devolvió `[]` al momento de empezar) — todo lo hecho acá es código + tests
contra mocks, no verificación contra el E-517 real.

## Resumen ejecutivo

De los 4 pedidos técnicos concretos de la auditoría, implementé 2 tal cual,
y en los otros 2 hice algo distinto de lo pedido porque, revisando el código
fuente de `pipython` (`gcsmessages.py`), encontré que **ya existe una
solución construida para exactamente ese problema en la capa correcta**
(no en el gateway). Prefiero explicar esto en detalle a implementar algo
redundante o que compita con esa lógica ya existente.

## 1. Respuestas largas (qDRR, qGWD, qHPA)

**Lo que se pidió**: que `read()` no trunque respuestas de 64+ KB, timeout
USB más permisivo, y un helper `read_until_prompt()` que busque el
marcador de fin de respuesta GCS en vez de un timeout ciego.

**Lo que encontré**: `pipython.pidevice.gcsmessages.GCSMessages` ya
implementa exactamente esa detección de fin de respuesta — una función
`eol(rcvbuf)` que decide si una respuesta terminó mirando si la última
línea NO empieza con espacio (las líneas de continuación de un array GCS sí
empiezan con espacio). Para respuestas tipo array grandes (`qDRR`, `qGWD`)
hay además un mecanismo dedicado más sofisticado: un **hilo de fondo**
(`_readgcsdata`/`_fillbuffer`) que va leyendo y convirtiendo a float línea
por línea hasta encontrar el marcador de fin de datos, exponiendo progreso
vía `pidevice.bufstate`/`pidevice.bufdata`.

**Qué hice en cambio**:
- **No** agregué `read_until_prompt()` — habría duplicado (y podido
  desincronizarse de) esta lógica que ya vive en pipython, en la capa que
  además ya está probada por el propio proyecto PI. El contrato de
  `read()` sigue siendo "devolvé lo que haya disponible ahora mismo", igual
  que `PISerial`/`PISocket` — es la capa de arriba (`GCSMessages`) la que
  sabe de sintaxis GCS.
- Subí el tamaño pedido por llamada a `read_data()` de 4096 a **65536
  bytes** (`_READ_CHUNK`). Esto no evita ningún truncamiento real (`read()`
  nunca truncaba: `GCSMessages` ya acumulaba llamadas sucesivas), pero sí
  reduce la cantidad de round-trips USB necesarios para una respuesta de
  varios KB — mejora de performance real, sin cambiar el contrato.
- Separé el timeout USB de bajo nivel del timeout GCS configurado (ver
  punto siguiente) — esto es lo que de verdad hacía falta para que un
  timeout GCS largo (necesario para `qDRR` con miles de puntos) no
  se traduzca en una sola llamada bloqueante gigante.

**Test agregado**: `test_read_large_response_not_truncated_across_calls`
simula una respuesta de 5000 valores repartida en paquetes de 300 bytes
(como llegaría realmente por USB) y verifica que reensamblarla en un loop
(como hace `GCSMessages`) da los 5000 valores completos, sin cortes.

## 2. Timeout USB vs. timeout GCS

**Lo que se pidió**: timeout USB permisivo para `qDRR` con 8000+ puntos.

**Lo que hice**: en vez de simplemente alargar el timeout USB al mismo
valor que el timeout GCS (lo que había en la v0.2.0 — `settimeout()`
copiaba el valor directo a `_usb_read_timeout`), lo desacoplé:

- El timeout GCS (el que pipython necesita para saber cuándo rendirse en
  un `qDRR` grande) se sigue guardando tal cual, sin cambios, vía
  `gateway.timeout` / `settimeout()`.
- El timeout USB de bajo nivel (el que usa cada llamada individual a
  `read_data()`/`write_data()`) queda acotado a `_USB_POLL_TIMEOUT_MS`
  (500 ms), sin importar cuán largo sea el timeout GCS pedido.

**Por qué**: si el timeout USB fuera igual al timeout GCS completo (por
ejemplo 20 segundos para un `qDRR` grande), una sola llamada a
`read_data()` sin datos disponibles bloquearía el hilo esos 20 segundos
enteros antes de que `GCSMessages._read()` — que tiene su propio loop con
su propio chequeo de timeout — pueda siquiera reevaluar si ya pasó el
tiempo. Con el timeout USB corto y desacoplado, `read()` vuelve rápido
(vacío) cuando no hay datos, y es el loop de `GCSMessages` el que decide
cuándo rendirse de verdad, con el timeout GCS real.

**Test agregado**: `test_settimeout_caps_usb_poll_timeout_for_long_gcs_timeouts`
verifica que pedir un timeout GCS de 20000 ms no se traduce en un timeout
USB de 20000 ms, sino que queda acotado a 500 ms.

## 3. Ráfagas de comandos rápidos (WAV_LIN × 200-500)

**Lo que se pidió**: verificar serialización, agregar ACK-check opcional
cada N comandos o mecanismo de retry si se detecta pérdida de comandos.

**Lo que encontré**: revisando `GCSMessages.send()` (el método que
`WAV_LIN()` usa internamente), el comportamiento **por defecto de
pipython ya es más estricto que lo pedido**: cada llamada "setter" normal
(como `WAV_LIN`, `MOV`, `SVO`) manda el comando y **después lee `ERR?` y
lo valida antes de devolver el control** — no es "cada N comandos", es en
cada comando, siempre, salvo que se desactive explícitamente
(`pidevice.errcheck = False`) o se use el `send()` crudo (como hace
`Codigo_michelsonTRES_CHAD.py` con `mov_raw()`, deliberadamente, para
evitar timeouts que habían tenido en Windows).

**Qué hice en cambio**: no agregué un mecanismo de retry/ACK propio en el
gateway, porque:
- Ya existe uno (más estricto) en la capa correcta, por defecto.
- Un retry a ciegas en la capa de transporte es peligroso sin evidencia de
  qué falla realmente: no sabemos todavía si un comando GCS2 tipo `WAV`
  es seguro de reenviar sin efectos secundarios (podría, por ejemplo,
  duplicar un `append='&'` sobre la wave table). Prefiero no inventar ese
  mecanismo sin haber visto una falla real.

Lo que sí puede importar en la práctica: si el script real usa el patrón
`send()` crudo (como `mov_raw()` en Michelson) para la ráfaga de
`WAV_LIN`, pierde la protección de `ERR?` automático a cambio de
velocidad — eso es una decisión de diseño del script del experimento, no
algo que este gateway deba decidir por su cuenta. Lo dejé documentado en
el README.

**Sobre la serialización en sí**: `send()`/`write_data()` en este gateway
es una llamada bloqueante (espera a que la transferencia USB termine antes
de devolver el control), así que dos comandos consecutivos desde Python de
un solo hilo — que es como se usa esto hoy — no pueden pisarse en el cable
por construcción. No hay concurrencia en juego acá.

**No pude testear esto contra hardware real** (sin equipo conectado). El
test que sí agregué (`test_send_translates_trailing_newline_to_cr`,
`test_send_usb_error_marks_disconnected`) cubre el contrato de `send()`
en aislamiento, no una ráfaga real de cientos de comandos contra el
firmware. Eso queda para mañana.

## 4. Cobertura de comandos con tests

Se agregaron 20 tests unitarios (`tests/test_pi_ftdi_gateway.py`, todos
pasando) contra un `Ftdi`/`usb.core` **mockeados**. Cubren el contrato del
gateway en sí (`send`, `read`, `flush`, `close`, `settimeout`,
`list_devices`, manejo de conexión/desconexión) — **no** la semántica GCS2
de cada comando individual de la lista (`WGO`, `DRC`, `CTO`, `IMP`, etc.),
porque esa semántica vive en `pipython` (ya probado río arriba por el
propio proyecto) y no en este gateway. Ver el detalle de cuáles wrappers
existen en pipython para cada comando de tu lista en la conversación
previa (todos existen salvo `ATS?` y el setter crudo de `WAV`).

## 5. Documentación

Actualicé `README.md` con:
- Tabla de estado por grupo de comandos (wrapper en pipython / contrato
  I/O probado con mock / hardware real), marcando explícitamente qué es
  mock-only vs. lo poco que sí está confirmado contra hardware real de
  sesiones anteriores (conexión, `MOV` punto a punto, `qPOS`, `qVOL`).
- Limitaciones conocidas: sin techo artificial de tamaño de respuesta,
  comportamiento de timeout USB vs. GCS, y la nota sobre `ERR?` automático
  en ráfagas.
- Ejemplo de uso de wave table (`WCL` + `WAV_LIN` + `qGWD`/`bufstate`/
  `bufdata`) — armado siguiendo las firmas reales documentadas en
  pipython, pero **sin correr contra hardware**, marcado como tal.

## Qué falta para mañana (con hardware real)

1. Correr `qHLP()` contra el E-517 real (pendiente desde la conversación
   anterior) para confirmar qué de esta lista está realmente habilitado en
   este firmware puntual — pipython no tiene tabla hardcodeada para
   'E-545'/'E-517', así que hoy no hay forma de saberlo sin preguntarle al
   equipo.
2. Correr el ejemplo de `WAV_LIN` + `qGWD` de 100 puntos del README contra
   hardware real y confirmar que los valores leídos coinciden con los
   escritos.
3. Repetir esa prueba con 500 puntos (como pide el punto 3 de la
   auditoría) y medir cuánto tarda la ráfaga en la práctica — con el
   `ERR?` automático de pipython, va a ser la suma de 500 round-trips, no
   instantáneo; conviene medirlo antes de asumir un tiempo total para el
   experimento de mañana.
4. Probar `qDRR` con varios miles de puntos reales y confirmar que el
   timeout GCS configurado (subirlo con `pidevice.timeout = ...` antes de
   una lectura grande) alcanza — con el timeout USB ahora desacoplado
   debería comportarse mejor, pero no hay forma de confirmarlo sin el
   volumen de datos real.
5. Si algún comando de la lista falla con el equipo real (por ejemplo si
   el firmware no tiene wave generator habilitado), volver a esta
   auditoría y ajustar la tabla de estado del README en consecuencia.

## Archivos tocados

- `pi_ftdi_gateway/__init__.py`: `_READ_CHUNK` (4096→65536),
  `_USB_POLL_TIMEOUT_MS` (desacople de timeouts), comentarios explicando
  ambas decisiones y por qué no hay `read_until_prompt()`/retry propios.
- `tests/test_pi_ftdi_gateway.py`: nuevo, 20 tests contra mocks.
- `README.md`: tabla de estado de comandos, limitaciones, ejemplo de wave
  table.
- `pyproject.toml`: versión 0.2.0 → 0.3.0.
- Reinstalado con `pip install -e .` en `~/python-envs/pi/`.
