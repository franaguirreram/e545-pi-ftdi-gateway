#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[MEDIDO] Tests unitarios de pi_ftdi_gateway contra un Ftdi/usb.core mockeados
(sin hardware real). Cubren el contrato del gateway (send/read/flush/close/
settimeout/list_devices) y los tres puntos pedidos en la auditoría del
2026-08-27: respuestas largas sin truncar, timeout USB desacoplado del
timeout GCS, y manejo de errores de conexión/desconexión.

Lo que estos tests NO cubren (y no deberían, no es responsabilidad del
gateway): sintaxis GCS2, parseo de arrays qDRR/qGWD, ni el protocolo de
ERR? después de cada comando — todo eso vive en pipython.pidevice.gcsmessages
y ya tiene su propia lógica, ver el docstring del módulo del gateway.

Sin hardware conectado durante esta sesión no se pudo correr nada de esto
contra el E-517 real; ver INFORME_AUDITORIA.md para el plan de verificación
en hardware real.
"""

import unittest
from unittest.mock import MagicMock, patch

from usb.core import USBError, USBTimeoutError

import pi_ftdi_gateway as gw


def make_fake_ftdi_class(usb_dev=None):
    """Arma una clase Ftdi falsa cuya instancia (Ftdi()) tiene los
    atributos mínimos que _set_baud_fixed()/ConnectFTDI() necesitan."""
    instance = MagicMock()
    instance.BAUDRATE_REF_BASE = 3_000_000  # mismo valor real que pyftdi usa para este chip
    instance._usb_dev = usb_dev or MagicMock()
    instance._usb_write_timeout = 500
    instance._usb_read_timeout = 500

    fake_class = MagicMock(return_value=instance)
    fake_class.REQ_OUT = 0x40
    fake_class.SIO_REQ_SET_BAUDRATE = 3
    fake_class.PRODUCT_IDS = {}
    fake_class.add_custom_product = MagicMock()
    return fake_class, instance


def make_fake_usb_device(bus=1, address=4, idProduct=gw.PI_PID):
    dev = MagicMock()
    dev.bus = bus
    dev.address = address
    dev.idProduct = idProduct
    return dev


class ListDevicesTests(unittest.TestCase):

    def test_no_devices_connected(self):
        with patch.object(gw.usb.core, 'find', return_value=[]):
            self.assertEqual(gw.list_devices(), [])

    def test_one_device_connected(self):
        dev = make_fake_usb_device()
        with patch.object(gw.usb.core, 'find', return_value=[dev]):
            found = gw.list_devices()
        self.assertEqual(found, [{'bus': 1, 'address': 4, 'idProduct': gw.PI_PID}])

    def test_filters_by_pid_when_given(self):
        with patch.object(gw.usb.core, 'find', return_value=[]) as mock_find:
            gw.list_devices(pid=0x1005)
        _, kwargs = mock_find.call_args
        self.assertEqual(kwargs.get('idProduct'), 0x1005)


class ConnectFTDITests(unittest.TestCase):

    def test_no_device_raises_clear_error(self):
        with patch.object(gw.usb.core, 'find', return_value=[]):
            with self.assertRaises(gw.PIFtdiConnectionError) as ctx:
                gw.PIFtdiGateway()
        self.assertIn('enchufado', str(ctx.exception))

    def test_ambiguous_devices_raise_clear_error(self):
        devs = [make_fake_usb_device(bus=1, address=4), make_fake_usb_device(bus=1, address=7)]
        with patch.object(gw.usb.core, 'find', return_value=devs):
            with self.assertRaises(gw.PIFtdiConnectionError) as ctx:
                gw.PIFtdiGateway()
        self.assertIn('address=', str(ctx.exception))

    def test_address_disambiguates(self):
        devs = [make_fake_usb_device(bus=1, address=4), make_fake_usb_device(bus=1, address=7)]
        fake_class, instance = make_fake_ftdi_class()
        with patch.object(gw.usb.core, 'find', return_value=devs), \
             patch.object(gw, 'Ftdi', fake_class):
            gateway = gw.PIFtdiGateway(address=(1, 7))
        self.assertTrue(gateway.connected)

    def test_successful_connect_sets_dtr_rts_and_connected(self):
        dev = make_fake_usb_device()
        fake_class, instance = make_fake_ftdi_class()
        with patch.object(gw.usb.core, 'find', return_value=[dev]), \
             patch.object(gw, 'Ftdi', fake_class), \
             patch('time.sleep', return_value=None):
            gateway = gw.PIFtdiGateway()
        self.assertTrue(gateway.connected)
        instance.set_dtr.assert_called_once_with(True)
        instance.set_rts.assert_called_once_with(True)
        instance.purge_buffers.assert_called_once()
        instance._usb_dev.ctrl_transfer.assert_called_once()  # el set de baudrate corregido

    def test_usb_error_during_open_raises_and_stays_disconnected(self):
        dev = make_fake_usb_device()
        fake_class, instance = make_fake_ftdi_class()
        instance.open.side_effect = USBError('no such device')
        with patch.object(gw.usb.core, 'find', return_value=[dev]), \
             patch.object(gw, 'Ftdi', fake_class):
            with self.assertRaises(gw.PIFtdiConnectionError):
                gw.PIFtdiGateway()


class SendReadTests(unittest.TestCase):

    def _connected_gateway(self):
        dev = make_fake_usb_device()
        fake_class, instance = make_fake_ftdi_class()
        patcher_find = patch.object(gw.usb.core, 'find', return_value=[dev])
        patcher_ftdi = patch.object(gw, 'Ftdi', fake_class)
        patcher_sleep = patch('time.sleep', return_value=None)
        patcher_find.start()
        patcher_ftdi.start()
        patcher_sleep.start()
        self.addCleanup(patcher_find.stop)
        self.addCleanup(patcher_ftdi.stop)
        self.addCleanup(patcher_sleep.stop)
        gateway = gw.PIFtdiGateway()
        return gateway, instance

    def test_send_translates_trailing_newline_to_cr(self):
        gateway, instance = self._connected_gateway()
        gateway.send("MOV A 100.000000\n")
        sent_bytes = instance.write_data.call_args[0][0]
        self.assertEqual(sent_bytes, b"MOV A 100.000000\r")

    def test_send_without_connection_raises(self):
        gateway, instance = self._connected_gateway()
        gateway._connected = False
        with self.assertRaises(gw.PIFtdiConnectionError):
            gateway.send("MOV A 1\n")

    def test_send_usb_error_marks_disconnected(self):
        gateway, instance = self._connected_gateway()
        instance.write_data.side_effect = USBError('device disappeared')
        with self.assertRaises(gw.PIFtdiConnectionError):
            gateway.send("MOV A 1\n")
        self.assertFalse(gateway.connected)

    def test_read_returns_decoded_bytes(self):
        gateway, instance = self._connected_gateway()
        instance.read_data.return_value = b"Physik Instrumente, E-517, 0111176619, V01.243\n"
        self.assertEqual(gateway.read(), "Physik Instrumente, E-517, 0111176619, V01.243\n")

    def test_read_uses_large_chunk_size(self):
        """[Auditoria pto 1] read() debe pedir al menos 64 KB por llamada,
        no 4096, para necesitar menos round-trips en respuestas grandes
        (qDRR/qGWD/qHPA)."""
        gateway, instance = self._connected_gateway()
        instance.read_data.return_value = b""
        gateway.read()
        requested_size = instance.read_data.call_args[0][0]
        self.assertGreaterEqual(requested_size, 65536)

    def test_read_large_response_not_truncated_across_calls(self):
        """[Auditoria pto 1] Simula una respuesta de qDRR con 5000 valores
        repartida en varias llamadas a read_data() (como pasaría de verdad
        por USB), y verifica que concatenar lo que devuelve read() en un
        loop tipo GCSMessages no pierde ni corta nada."""
        gateway, instance = self._connected_gateway()
        # 5000 líneas de datos simuladas, la última sin espacio final (fin GCS)
        lines = [f" {i}.000000\n" for i in range(5000)]
        full_response = ''.join(lines).encode('cp1252')
        # Partido en pedacitos de 300 bytes, como llegaría realmente por USB
        chunk_size = 300
        chunks = [full_response[i:i + chunk_size] for i in range(0, len(full_response), chunk_size)]
        chunks.append(b'')  # fin de datos disponibles
        instance.read_data.side_effect = chunks

        received = ''
        for _ in range(len(chunks)):
            received += gateway.read()

        self.assertEqual(received.count('\n'), 5000)
        self.assertIn('4999.000000', received)

    def test_read_timeout_returns_empty_without_disconnecting(self):
        gateway, instance = self._connected_gateway()
        instance.read_data.side_effect = USBTimeoutError('timed out')
        self.assertEqual(gateway.read(), '')
        self.assertTrue(gateway.connected)

    def test_read_usb_error_marks_disconnected(self):
        gateway, instance = self._connected_gateway()
        instance.read_data.side_effect = USBError('device disappeared')
        with self.assertRaises(gw.PIFtdiConnectionError):
            gateway.read()
        self.assertFalse(gateway.connected)

    def test_settimeout_caps_usb_poll_timeout_for_long_gcs_timeouts(self):
        """[Auditoria pto 1] Un timeout GCS largo (20s, razonable para
        qDRR con 8000+ puntos) no debe convertirse en un único read USB
        bloqueante de 20s: el timeout USB de bajo nivel queda acotado."""
        gateway, instance = self._connected_gateway()
        gateway.settimeout(20000)
        self.assertEqual(instance._usb_read_timeout, gw._USB_POLL_TIMEOUT_MS)
        self.assertEqual(instance._usb_write_timeout, gw._USB_POLL_TIMEOUT_MS)
        self.assertEqual(gateway.timeout, 20000)  # el timeout GCS "real" sí se guarda tal cual

    def test_settimeout_short_value_used_directly(self):
        gateway, instance = self._connected_gateway()
        gateway.settimeout(200)
        self.assertEqual(instance._usb_read_timeout, 200)

    def test_flush_only_when_connected(self):
        gateway, instance = self._connected_gateway()
        gateway.close()
        instance.purge_buffers.reset_mock()
        gateway.flush()
        instance.purge_buffers.assert_not_called()

    def test_close_is_idempotent(self):
        gateway, instance = self._connected_gateway()
        gateway.close()
        gateway.close()  # no debe explotar ni volver a llamar ftdi.close()
        instance.close.assert_called_once()

    def test_close_connection_alias_calls_close(self):
        """CloseConnection() es idioma típico de los ejemplos oficiales de
        PI (pensados para GCSDll); acá debe comportarse igual que close()."""
        gateway, instance = self._connected_gateway()
        gateway.CloseConnection()
        self.assertFalse(gateway.connected)
        instance.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
