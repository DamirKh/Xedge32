.. _espnow-api:

ESP-NOW Receive API
===================

See :ref:`hardware-resource-lifetime` for callback ownership, close results,
and cleanup behavior.

ESP-NOW is a connectionless Wi-Fi protocol for short messages between nearby
ESP32 devices. Xedge32 exposes a receive-only Lua API. Use it when another
device sends sensor readings, commands, or small binary records without
setting up a TCP/IP connection.

.. important::

   The receiver uses the channel of the active Wi-Fi interface. The sender
   must transmit on the same channel. In station mode the channel follows the
   connected access point. In access-point mode it is
   ``CONFIG_WIFI_AP_CHANNEL`` (default ``1``).

ESP-NOW receive is available on ESP32 and ESP32-S3 targets with native Wi-Fi.
It is not registered on ESP32-P4 ESP-Hosted builds.

Creating a Receiver
-------------------

Function signature:

.. code-block:: lua

   rx, err = esp32.espnow{callback = function(data, src, info) ... end}

``callback`` is required. Only one receiver can be open at a time. A second
``esp32.espnow()`` call returns ``nil`` and an error string until the first
receiver is closed.

Callback arguments:

``data``
   Binary string containing 1 to 250 bytes.

``src``
   Six-byte binary string containing the sender's MAC address.

``info``
   Table with these fields:

   - ``dest``: six-byte destination MAC address.
   - ``rssi``: received signal strength in dBm.
   - ``channel``: Wi-Fi channel on which the packet was received.
   - ``len``: payload length in bytes.

The callback runs in an ``LThreadMgr`` worker. It may close the receiver, but
it should return quickly. If the callback raises an error, Xedge32 closes the
receiver.

Receiver Methods
----------------

``rx:close()``
   Stops ESP-NOW receive handling. Returns ``true`` on success, or ``nil`` and
   an error string. Closing an already closed receiver succeeds. Pending
   callbacks are discarded.

``rx:info()``
   Returns a table with the current Wi-Fi ``channel``, ESP-NOW ``version``, and
   the number of received packets dropped because the queue was full or memory
   was unavailable.

Limits and Behavior
-------------------

- Maximum payload is 250 bytes (ESP-NOW v1). Longer packets are dropped.
- No send, peer management, or encryption API is exposed in this version.
- Up to eight received packets may be queued for Lua delivery. When the queue
  is full, the newest packet is dropped and counted by ``rx:info().drops``.
- Keep a Lua reference to the receiver while it is in use. Queued callbacks do
  not keep the object alive.

Example
-------

.. code-block:: lua

   local function formatMac(mac)
      return (mac:gsub(".", function(c)
         return string.format("%02X:", c:byte())
      end):gsub(":$", ""))
   end

   local rx = assert(esp32.espnow{
      callback = function(data, src, info)
         trace("ESP-NOW", formatMac(src), #data, info.rssi, info.channel)
      end
   })

   trace("channel", rx:info().channel)

   -- Keep the receiver alive for as long as messages are expected.
   ba.sleep(60000)

   rx:close()

Troubleshooting
---------------

- ``ESP_ERR_ESPNOW_NOT_INIT`` or ``wifi: ESP_ERR_INVALID_STATE`` means Wi-Fi is
  not started. Start Wi-Fi or access-point mode before opening the receiver.
- No packets usually means the sender is on a different channel. Compare
  ``rx:info().channel`` with the sender's configured channel.
- ESP-NOW shares the radio with Wi-Fi. Large Wi-Fi transfers can increase
  latency and packet loss.
