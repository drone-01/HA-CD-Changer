# Sony BDP-CX7000ES network control (as tested)

Summary of how Disc Library talks to the changer. Command bytes come from Sony's
"RS-232C/Network Control Protocol Specification for Sony BD Player BDP-CX7000ES",
version 0.97; the network transport was found by testing a real unit on 2026-10-25.

## Enabling it on the changer

System Settings → Custom Installation Control → **Network**. The unit needs a fixed or
reserved IP address. One mode at a time: Network or RS-232.

## Transport: UPnP (SOAP over HTTP), not a raw socket

- The unit announces itself over SSDP as `urn:schemas-sony-com:service:X_CIS:1`.
- Device description: `http://<ip>:8888/CIS_DeviceDescription.xml` (friendlyName `BDP_CISIP`).
- Control URL: `http://<ip>:8888/upnp/control/CIS`, action **`X_CIS_Command`**:
  - in: `CISDATA`, the whole frame as a hex string (no spaces), e.g. `02028082FC`
  - out: `Res_CISDATA`, the reply frame as hex (or `fd` = ACK, `fe` = NACK)
- Events: `http://<ip>:8888/upnp/event/CIS`, evented variable `X_CISStatus` (unsolicited
  status when Broadcast Mode is on).
- SOAPAction header: `"urn:schemas-sony-com:service:X_CIS:1#X_CIS_Command"`.
- Raw TCP or UDP frames sent to 8888 get no reply (8888 is the HTTP server).

## Frame

`02  LEN  PDC  CMD  DATA…  SUM`

- `LEN` = number of bytes in PDC + CMD + DATA.
- `SUM` = (0x100 − (LEN + PDC + CMD + DATA… ) & 0xFF) & 0xFF.
- Request PDC: `80` / `81` / `82` = the unit's command set BD1 / BD2 / BD3.
- Reply PDC: `88` / `89` / `8A`. ACK = `FD`, NACK = `FE`.
- Numbers are BCD (disc 388 = `03 88`).

Tested replies:

| Request | Reply |
|---|---|
| `020280A0DE` model name | `021088a0` + "BDP-CX7000ES" + `0000cb` |
| `02028082FC` status | `020a8882 0388 0001 0003 11 00 4c` = disc 388, title 1, track 3, playing |

## Commands used by Disc Library

| Function | CMD | Data |
|---|---|---|
| Power | `60` | `00` off, `01` on (startup takes minutes) |
| Play disc / track | `4A` | disc (2, BCD) · title/track (2, BCD) · chapter (2, BCD) · control `00` play / `01` pause |
| Play / Pause / Stop | `12` / `13` / `14` | none |
| Next / Previous | `11` / `10` | none |
| Status request | `82` | none → disc, title, chapter/track, status, mode |
| Disc data | `8A` | disc (2) → type (`01` CD, `03` DVD, `05` BD), first/last track |
| Disc information | `8D` | disc · data type (`00` album title / disc name, `01` track name, `02` genre) · track |
| Slots occupied | `CC` (network) | packet `00`–`04` → bitmap of 80 slots per packet |
| Broadcast mode | `62` | `01` basic status pushes |
| Open / close / eject | `50` | `00` close, `01` open, `02` eject |
| Model name | `A0` | none |

Status byte: `00` off, `10` stop, `11` play, `12` pause, `20` loading, `30` door open, `EE` error.
Over the network the unit answers a status request even in standby (other fields invalid);
only power-on and status are accepted while off.
