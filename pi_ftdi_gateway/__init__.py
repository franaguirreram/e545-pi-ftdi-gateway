#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[MEDIDO] Gateway PIGateway-compatible para hablar GCS2 con controladores PI
(probado contra un E-545/E-517) sobre macOS arm64, sin la DLL/dylib
propietaria de Physik Instrumente.

Transporte real: chip FTDI (VID 0x1a72 / PID 0x1005, propios de PI, montados
sobre silicio FTDI) vía pyftdi + libusb. Protocolo: FTDI estándar (público)
+ comandos ASCII GCS2 (públicos, documentados por PI). No usa ni copia
código de PI_GCS2_DLL.dll ni de libpi_pi_gcs2.so/.dylib.

Hallazgos empíricos que este módulo encapsula:
- pyftdi 0.57.2 detecta este chip como 'ft232am' (por bcdDevice=0x0200) y
  su algoritmo de baudrate "legacy" tiene un bug de división por cero para
  ciertos baudrates (115200 incluido). _set_baud_fixed() reimplementa el
  mismo cálculo documentado por FTDI, corrigiendo ese bug puntual.
- El controlador no responde si no se afirman DTR y RTS antes de escribir.
- El controlador espera terminador '\\r' (o '\\r\\n'); un '\\n' suelto no
  dispara respuesta. pipython arma sus comandos terminados en '\\n', así que
  send() traduce '\\n' -> '\\r' antes de escribir al puerto.
- El descriptor USB no trae el número de serie real del equipo
  (iSerialNumber vacío); ese dato solo se obtiene preguntando '*IDN?' una
  vez conectado, no se puede usar para elegir un equipo antes de conectar.

Nota sobre respuestas largas (qDRR, qGWD, qHPA, etc.): la detección de fin
de respuesta GCS ("¿esta línea es la última del array?") NO vive acá, vive
en pipython (pipython.pidevice.gcsmessages.GCSMessages: función eol() +
un hilo de fondo dedicado en _readgcsdata()/_fillbuffer() para arrays
grandes tipo qDRR). Este gateway solo tiene que devolver bytes crudos tal
cual llegan, igual que PISerial/PISocket — agregarle acá lógica de
parseo GCS duplicaría (y podría desincronizarse de) esa lógica ya
existente. Ver INFORME_AUDITORIA.md para el detalle de esta decisión.
"""

import time

import usb.core
from usb.core import USBError, USBTimeoutError
from pyftdi.ftdi import Ftdi, FtdiError
from pipython.pidevice.interfaces.pigateway import PIGateway, PI_CONTROLLER_CODEPAGE

__all__ = ['PIFtdiGateway', 'PIFtdiConnectionError', 'list_devices', 'cleanup_gcsdevice', 'PI_VID', 'PI_PID']

PI_VID = 0x1a72
PI_PID = 0x1005

# [Fix respuestas largas] tamaño máximo por llamada a read_data(). No es un
# límite de cuánto se puede leer en total (GCSMessages acumula llamadas
# sucesivas hasta ver el fin de respuesta) sino cuántos bytes se piden de
# una vez al buffer USB; subirlo reduce la cantidad de round-trips para
# respuestas de varios KB (qDRR con miles de puntos, qHPA, qGWD).
_READ_CHUNK = 65536

# [Fix timeout real] timeout de las transferencias USB de bajo nivel
# (libusb), en ms. Se mantiene deliberadamente corto y DESACOPLADO del
# timeout GCS configurado vía settimeout(): si se usara el timeout GCS
# completo (p.ej. varios segundos para qDRR con 8000 puntos) directo en
# cada llamada a read_data()/write_data(), una sola lectura sin datos
# bloquearía el hilo por ese tiempo entero antes de que GCSMessages._read()
# pueda siquiera reevaluar su propio timeout. Con un timeout USB corto,
# read() vuelve rápido con '' cuando no hay datos todavía, y es el loop de
# GCSMessages (que sí conoce el timeout GCS real) el que decide cuándo
# rendirse. Ver INFORME_AUDITORIA.md.
_USB_POLL_TIMEOUT_MS = 500

# [Fix 3 - autodetección] registrar el VID/PID propio de PI ante pyftdi,
# a prueba de recargas de módulo (Spyder recarga módulos sin reiniciar el
# proceso, y pyftdi no soporta registrar el mismo par dos veces).
if PI_PID not in Ftdi.PRODUCT_IDS.get(PI_VID, {}).values():
    Ftdi.add_custom_product(PI_VID, PI_PID)


class PIFtdiConnectionError(IOError):
    """Se levanta cuando falla la conexión o se pierde la comunicación
    con el controlador PI (desenchufado, timeout real, etc.)."""


def list_devices(vid=PI_VID, pid=None):
    """[Fix 3 - autodetección] Lista dispositivos USB conectados que
    coincidan con 'vid' (y opcionalmente 'pid').

    No puede devolver el número de serie real de GCS2: el descriptor USB
    de estos equipos no lo trae (iSerialNumber vacío). Para saber qué
    equipo es cada uno hay que conectarse y preguntar '*IDN?'.

    @return: lista de dicts {'bus':int, 'address':int, 'idProduct':int}
    """
    kwargs = {'idVendor': vid}
    if pid is not None:
        kwargs['idProduct'] = pid
    return [
        {'bus': dev.bus, 'address': dev.address, 'idProduct': dev.idProduct}
        for dev in usb.core.find(find_all=True, **kwargs)
    ]


class PIFtdiGateway(PIGateway):
    """Gateway GCS2 sobre FTDI crudo (pyftdi/libusb), sin DLL de PI."""

    def __init__(self, vid=PI_VID, pid=PI_PID, baudrate=115200, autoconnect=True,
                 address=None):
        """
        @param vid, pid : USB Vendor/Product ID del controlador PI.
        @param baudrate : Baudrate GCS2, 115200 por defecto (probado en E-545/E-517).
        @param autoconnect : Conecta automáticamente al construir el objeto.
        @param address : Tupla (bus, address) opcional. Si hay más de un
            controlador PI conectado con el mismo VID/PID, hay que indicar
            cuál usar (ver list_devices()); si no se indica y hay más de
            uno, ConnectFTDI() levanta PIFtdiConnectionError con la lista.
        """
        self._timeout = 7000  # ms, mismo default que PISerial/PISocket
        self._connected = False
        self._vid = vid
        self._pid = pid
        self._baudrate = baudrate
        self._address = address
        self._ftdi = None
        if autoconnect:
            self.ConnectFTDI()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __str__(self):
        return f'PIFtdiGateway(vid={self._vid:#06x}, pid={self._pid:#06x}, baudrate={self._baudrate})'

    @property
    def timeout(self):
        return self._timeout

    def settimeout(self, value):
        """[Fix 2 - timeout real] Guarda el timeout GCS (el que usa
        GCSMessages para decidir cuándo rendirse) y lo aplica también al
        timeout de las transferencias USB de bajo nivel — pero acotado a
        _USB_POLL_TIMEOUT_MS (ver comentario junto a esa constante): para
        timeouts GCS cortos, el USB usa el mismo valor; para timeouts GCS
        largos (como conviene para qDRR con miles de puntos), el USB sigue
        sondeando en tandas cortas en vez de bloquear una única vez por
        todo ese tiempo."""
        self._timeout = value
        if self._ftdi is not None:
            usb_timeout = min(value, _USB_POLL_TIMEOUT_MS) if value else _USB_POLL_TIMEOUT_MS
            self._ftdi._usb_read_timeout = usb_timeout
            self._ftdi._usb_write_timeout = usb_timeout

    @property
    def connected(self):
        return self._connected

    @property
    def connectionid(self):
        return 0

    def _set_baud_fixed(self, baudrate):
        """Reimplementación del cálculo legacy de baudrate de FTDI (AN232),
        corrigiendo un bug de pyftdi 0.57.2 que reusa 'div8' ya mutado."""
        ref = self._ftdi.BAUDRATE_REF_BASE
        div8 = int(round((8 * ref) / baudrate))
        if (div8 & 0x7) == 7:
            div8 += 1
        div = div8 >> 3
        frac = div8 & 0x7
        if frac == 1:
            div |= 0xc000
        elif frac >= 4:
            div |= 0x4000
        elif frac != 0:
            div |= 0x8000
        elif div == 1:
            div = 0
        value = div & 0xFFFF
        index = (div >> 16) & 0xFFFF
        self._ftdi._usb_dev.ctrl_transfer(
            Ftdi.REQ_OUT, Ftdi.SIO_REQ_SET_BAUDRATE, value, index,
            bytearray(), self._ftdi._usb_write_timeout)

    def _find_target(self):
        """[Fix 3 - autodetección] Resuelve a qué dispositivo USB conectarse,
        avisando con claridad si no hay ninguno o si hay más de uno."""
        candidatos = list_devices(self._vid, self._pid)
        if self._address is not None:
            candidatos = [d for d in candidatos if (d['bus'], d['address']) == self._address]

        if not candidatos:
            raise PIFtdiConnectionError(
                f"No se encontró ningún controlador PI (VID {self._vid:#06x} / "
                f"PID {self._pid:#06x}). ¿Está enchufado por USB?"
            )
        if len(candidatos) > 1 and self._address is None:
            raise PIFtdiConnectionError(
                f"Hay {len(candidatos)} controladores PI conectados con el mismo "
                f"VID/PID: {candidatos}. Pasá address=(bus, address) para elegir uno "
                f"(ver pi_ftdi_gateway.list_devices())."
            )
        return candidatos[0]

    def ConnectFTDI(self):
        """[Fix 1 - manejo de errores] Abre el dispositivo y deja el enlace
        serie listo para hablar GCS2. Levanta PIFtdiConnectionError con un
        mensaje claro si no hay dispositivo, hay ambigüedad, o falla el USB."""
        self._find_target()  # valida antes de intentar abrir, con mensaje claro
        try:
            self._ftdi = Ftdi()
            self._ftdi.open(vendor=self._vid, product=self._pid, interface=1)
            self._set_baud_fixed(self._baudrate)
            self._ftdi.set_line_property(8, 1, 'N')
            usb_timeout = min(self._timeout, _USB_POLL_TIMEOUT_MS) if self._timeout else _USB_POLL_TIMEOUT_MS
            self._ftdi._usb_read_timeout = usb_timeout
            self._ftdi._usb_write_timeout = usb_timeout
            self._ftdi.set_dtr(True)
            self._ftdi.set_rts(True)
            time.sleep(0.2)
            self._ftdi.purge_buffers()
        except (USBError, FtdiError) as exc:
            self._connected = False
            raise PIFtdiConnectionError(
                f"No se pudo abrir el controlador PI (VID {self._vid:#06x} / "
                f"PID {self._pid:#06x}): {exc}"
            ) from exc

        self._connected = True
        self.call_connection_status_changed_callback(self)

    def send(self, msg):
        """[Fix 1 - manejo de errores] Envía un comando GCS (sin esperar ERR?).
        Si el equipo se desconectó, marca connected=False y levanta un error
        claro en vez de dejar pasar la excepción cruda de libusb."""
        if not self._connected:
            raise PIFtdiConnectionError("send() llamado sin conexión activa al controlador PI")
        payload = msg.replace('\n', '\r')
        try:
            self._ftdi.write_data(payload.encode(PI_CONTROLLER_CODEPAGE))
        except (USBError, FtdiError) as exc:
            self._connected = False
            raise PIFtdiConnectionError(
                f"Se perdió la conexión con el controlador PI al escribir {msg!r}: {exc}"
            ) from exc

    def read(self):
        """[Fix 1 y 2] Devuelve lo que haya disponible en el buffer de entrada.
        Un timeout de lectura sin datos (USBTimeoutError) es normal durante el
        sondeo de pipython y se devuelve como cadena vacía, igual que
        PISerial.read(). Cualquier otro error de USB se interpreta como
        pérdida real de conexión."""
        if not self._connected:
            return ''
        try:
            received = self._ftdi.read_data(_READ_CHUNK)
        except USBTimeoutError:
            return ''
        except (USBError, FtdiError) as exc:
            self._connected = False
            raise PIFtdiConnectionError(
                f"Se perdió la conexión con el controlador PI al leer: {exc}"
            ) from exc
        return received.decode(PI_CONTROLLER_CODEPAGE, errors='ignore')

    def flush(self):
        if self._connected:
            self._ftdi.purge_buffers()

    def unload(self):
        self.close()

    def CloseConnection(self):
        """Alias de compatibilidad: algunos scripts (siguiendo el idioma de
        los ejemplos oficiales de PI, pensados para GCSDll) llaman
        pidevice.CloseConnection() en vez de pidevice.close(). Ese método
        es específico de GCSDll (pega directo a la DLL nativa) y no forma
        parte de la interfaz PIGateway — ni PISerial ni PISocket lo tienen
        tampoco. Se agrega acá solo para que ese idioma no rompa con este
        gateway."""
        self.close()

    def close(self):
        if not self._connected:
            return
        self._connected = False
        try:
            self._ftdi.close()
        except (USBError, FtdiError):
            pass  # ya se está cerrando, no interesa si el USB protesta
        self.call_connection_status_changed_callback(self)


def cleanup_gcsdevice(pidevice):
    """[Fix "solo corre una vez por consola"] Cierra una GCSDevice
    desregistrando su callback de cambio de estado, igual que hace
    'with GCSDevice(...) as pidevice:' (vía __exit__/_cleanup) -- pero sin
    necesitar el bloque 'with', que no sirve en scripts pensados para
    correr celda por celda (Spyder/Jupyter), ya que el cuerpo de un 'with'
    no puede repartirse en celdas separadas.

    Motivo del bug que esto evita: pipython.pidevice.interfaces.pigateway.
    PIGateway guarda sus callbacks de cambio de estado en una lista
    COMPARTIDA a nivel de clase (PIGateway._connection_status_changed_
    callbacks). Un pidevice.close() suelto no la desregistra -- solo
    __exit__/__del__ lo hacen. Sin este helper (o sin 'with'), la próxima
    vez que se conecte un GCSDevice en el mismo proceso (por ejemplo,
    correr el mismo script de nuevo en la misma consola de Spyder sin
    reiniciar el kernel), ese callback stale se dispara también, apuntando
    a una conexión ya cerrada, y produce un PIFtdiConnectionError confuso
    en una conexión que en realidad está viva.

    Uso: reemplazar pidevice.close() por cleanup_gcsdevice(pidevice).
    """
    try:
        interface = pidevice.gcsdevice.messages.interface
        interface.unregister_connection_status_changed_callback(pidevice.connection_status_changed)
    except AttributeError:
        pass
    pidevice.close()
