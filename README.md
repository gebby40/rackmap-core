# rackmap

Self-hosted network and rack manager: draw your racks, find what is on the network, open consoles,
switch servers on and off, see health from your monitoring system, and keep track of what is cabled
to what. One small web app, one SQLite file, no agents on your equipment.

What the open-source part does:

- Racks and equipment: an equipment database of several thousand models with their real port
  layouts, an editable rack drawing, a table of everything, and a details panel for each piece.
- Discovery: scans the networks you name and asks where each thing it finds belongs.
- Consoles in the browser through a console server (Digi CM), and servers' own management
  controllers (Redfish/iDRAC: power, health, text console) and PiKVM (screen, keyboard, mouse).
- Health, alerts, graphs, port states, readings and suggested cabling from a monitoring system
  (LibreNMS).
- Cables by hand or accepted from suggestions, drawn beside the rack and as a network map.
- Sign-in with a second step (authenticator codes), single sign-on through any OpenID Connect
  provider, HTTPS out of the box, remote access over Tailscale.
- The frame for *connections* to software and switches: a level per connection, confirm-by-name,
  protection, an action log. The connections themselves are plug-ins (see "Connections").

**Add-ons.** Connections to Proxmox VE, Docker, Unraid, pfSense, MikroTik and EnGenius switches
and other makers' switches, and working out the wiring from the switches' own address tables, are
separate add-ons. They are not open source, not in this repository, and not on sale yet. rackmap
is complete and useful without them, and anyone can write their own connection (see
"Connections").

Nothing about any particular network is built in. Devices, addresses and credentials are
entered or discovered through the app and stored in its data directory.

## Finding your way around

- **Dashboard** is home: how much equipment answers, what needs attention, every connection and
  how much of what it runs is up, and the latest actions.
- **Devices** is every piece of equipment in one table; sort by a column, filter by typing.
- **Racks** is the rack drawing. **Network map** draws what is connected to what: recorded cables
  solid, ones rackmap has worked out but you haven't confirmed dashed, Wi-Fi dotted.
- Choosing equipment on any of those slides its details in from the right. A switch's ports are
  drawn as a picture there; choose a port to see it and what can be done with it.

## Run it

With Docker:

```
docker compose up -d
```

Without Docker (Python 3.11 or newer):

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
RACKMAP_DATA=./data .venv/bin/python -m rackmap
```

Open `https://<host>:8443`. The first visitor creates the administrator account, so do that
straight away, and keep the port off untrusted networks.

### HTTPS

Console sessions carry passwords, so rackmap serves HTTPS out of the box. With nothing
configured it makes its own certificate on first start. A browser can't verify a home-made
certificate, so it warns the first time; the connection is encrypted either way, and the
certificate's fingerprint is printed in rackmap's log if you want to compare it. Two ways to
get rid of the warning:

- give rackmap a real certificate: set `RACKMAP_TLS_CERT` and `RACKMAP_TLS_KEY` to PEM files;
- put it behind something that already has one (a reverse proxy such as HAProxy, Caddy or
  nginx, or `tailscale serve`) and point that at `https://<host>:8443`. Console terminals use
  WebSockets, so the proxy must pass those through and keep the `Host` header.

| Setting | Default | Meaning |
|---|---|---|
| `RACKMAP_DATA` | `./data` | where the database, keys and cached images live |
| `RACKMAP_HOST` | `0.0.0.0` | address to listen on |
| `RACKMAP_PORT` | `8443` (`8080` with HTTPS off) | port to listen on |
| `RACKMAP_TLS` | `on` | `off` serves plain HTTP; only for a proxy on the same machine |
| `RACKMAP_TLS_CERT`, `RACKMAP_TLS_KEY` | none | your own certificate and key |

`deploy/rackmap.service` is a systemd unit for a plain install under `/opt/rackmap` with
data in `/var/lib/rackmap`.

## Consoles, controllers and KVMs

Three ways to reach a machine when its network is down, each set up from the equipment's
details panel and each invisible until you have one:

- **Console server** (Digi CM / Passport today): mark the equipment as a console server and
  give it an address and sign-in. Every other piece of equipment then gets a *Console port*
  choice, and a terminal to that serial port in the browser.
- **Server controller** (Dell iDRAC, HPE iLO, Supermicro and others, over Redfish): power
  state, health, power buttons and the serial-over-LAN text console.
- **KVM**: mark the equipment as a KVM and say how many ports it has. Every other piece of
  equipment then gets a *KVM port* choice. With a **PiKVM** and an account on it, rackmap shows
  the screen itself (live picture, keyboard, mouse, typing text, and the power and reset
  buttons if the PiKVM's ATX cable is fitted), including the ports of a PiKVM Switch. Any
  other KVM with a web page is linked to; one without a network connection is simply recorded.

Sign-in passwords for these are stored encrypted with a key kept in the data directory, and
rackmap remembers each device's certificate or SSH key the first time and refuses a different
one afterwards.

## Equipment database

rackmap bundles a catalogue of about 14,000 device types (maker, model, height, depth and port
layout) built from the [NetBox community device-type library](https://github.com/netbox-community/devicetype-library),
which is published under CC0 1.0. Photographs of front and rear panels are not bundled: the
first time one is shown, rackmap downloads it from that library and keeps it in the data
directory, so that needs internet access once per model. Without a photograph, or offline,
rackmap draws the panel from the port list. Rebuild the catalogue with `tools/build_catalog.py`.

## Discovery

On the Discovery page, list the private networks to scan (one per line, for example
`192.168.1.0/24`) and press Scan now. For every address that answers, rackmap records its name,
the common services it offers, the title of its web page and, if it speaks SNMP with the
community you give, what it says it is. From that it guesses the kind of device and, where the
model name appears, picks the model from the equipment database. Add… opens the equipment form
with what was found filled in and asks which rack and unit it sits in; Ignore hides a result.
Nothing is added to the inventory on its own.

Hardware (MAC) addresses, and so maker names, are only visible for devices on the same network
as rackmap. To get them for other networks, switch on SNMP on your router and enter its address
and community under "Ask the router".

rackmap refuses to scan public address ranges, and more than 4,096 addresses in one scan.
Run it on a machine that is allowed to reach the networks you want scanned.

## Consoles

On the Consoles page, connect a console server by giving its address and the account rackmap
should sign in with. rackmap signs in once to check the details, remembers the server's SSH
host key, and lists its serial ports. Click a port for a terminal in the browser; label ports
and tie them to equipment so the rack view gets an Open console button.

Supported so far: the Digi CM / Passport family, over SSH (`connect <port>`). Old models that
only offer old SSH algorithms are handled.

Things to know:

- The console server's password is stored encrypted in the database, with the key in
  `secret.key` beside it. That protects a stray copy of the database, not the whole data directory.
- If the server's SSH host key changes, rackmap refuses to connect until you open Edit and
  enter the password again, which accepts the new key.
- Terminals travel over the same connection as the rest of rackmap, which is plain HTTP unless
  you put it behind HTTPS or a VPN. Anything typed in a console, including passwords, is only as
  private as that connection.
- One session per port at a time is the console server's rule, not rackmap's.

## Servers: power, health and text console

Select a server in the rack view and choose Connect controller to attach its management
controller (Dell iDRAC, HPE iLO, Supermicro or Lenovo BMC, and others that speak Redfish). rackmap
then shows whether it is powered on, its health, temperatures and power draw, and offers Power
on, Shut down, Restart and Force off, each behind a confirmation.

- **Text console:** if the controller offers the server's serial console over SSH, enter the
  command it uses (Dell: `console com2`, HPE: `vsp`) and Open text console gives a terminal.
  The server must have serial console redirection switched on for this to show anything.
- **Remote screen:** many controllers only include a graphical remote screen with a paid
  licence. rackmap does not provide one. If you attach a network KVM, save its web address on
  the controller and rackmap links to it.
- The controller's certificate is remembered when it is added; a different certificate later is
  refused until the password is entered again under Edit controller. The password is stored the
  same way as console-server passwords.

## Live status

Equipment with a private IP address is checked about once a minute (a ping, then a few common
ports). The rack view shows a filled dot for answering and a hollow red one for not answering.
Set `RACKMAP_MONITOR=0` to switch this off.

## Monitoring source

If you already run a monitoring system, rackmap can show what it knows on the equipment recorded
here. Open Monitoring, choose Connect a monitoring system, and give its address and an API token.
LibreNMS is supported today; a read-only account's token is enough, and rackmap never changes
anything there.

- **Rack view:** the dot beside each piece of equipment becomes green (healthy), amber (a
  warning is open) or red (an alert is open, or it is not answering the monitoring system).
- **Rack view readings:** "Show" above the rack puts one reading beside each piece of
  equipment: its warmest temperature, its power (what it draws, or for a switch what it sends
  to powered devices), or its traffic. Equipment drawn by rackmap rather than shown as a
  photograph also gets its ports lit, when the number of ports reported matches the number
  drawn.
- **Details panel:** a Health section with what the device reports as, how long it has been
  running, connected ports and traffic, sensor readings (temperatures, fans, power), open
  alerts, day or week graphs and recent log lines.
- **Ports:** in the same panel, one square per socket: green is connected, amber has errors,
  hollow has nothing connected, grey is switched off, and a dot marks a port powering a
  device. Choosing a port shows its speed, traffic, errors, the power it sends and what is
  plugged into it. That last part comes from neighbours that announce themselves (LLDP) and,
  failing that, from the hardware addresses the switch has seen on the port, tied to
  equipment recorded here by MAC or IP address. A port that has seen many addresses is
  reported as leading to another switch.
- **Monitoring page:** everything that currently needs attention, worst first, and the devices
  the monitoring system watches that are not recorded in rackmap yet.

- **Rack totals:** one line under the rack's name: the power going through its battery
  backups (or, without any, the power its equipment reports drawing), the warmest piece of
  equipment and the battery runtime left.
- **Discovery:** devices the monitoring system watches that are not recorded here are offered
  on the Discovery page, next to what a scan finds.

Equipment is matched to monitored devices by IP address, then by name. Under Health, "Monitored
as" sets the match by hand or switches it off for one piece of equipment. rackmap keeps only a
short-lived copy of the answers; the history stays in the monitoring system. The token is stored
the same way as other saved passwords. An HTTPS certificate that can't be verified the usual way
is remembered when the source is connected, and a different one is refused afterwards.

## Remote access over Tailscale

[Tailscale](https://tailscale.com) joins your own devices into a private network that works
from anywhere. If the machine rackmap runs on is part of yours, Settings in rackmap can publish
rackmap to that network under the machine's Tailscale name (`https://<machine>.<tailnet>.ts.net`),
with a certificate browsers accept. It uses Tailscale's own `serve`, so nothing is opened to the
internet, no router setting changes, and only devices in your tailnet can reach it. You still
sign in to rackmap.

rackmap doesn't install Tailscale or sign it in. Do that once on the machine, and let the
account rackmap runs under manage it:

```
sudo tailscale up
sudo tailscale set --operator=rackmap     # the account rackmap runs as
```

HTTPS certificates must be switched on for the tailnet (Tailscale admin console, DNS, HTTPS
Certificates). Then open Settings in rackmap and choose Turn on remote access. Turning it off
there removes the published address again. The setting lives in Tailscale and survives restarts
of rackmap and of the machine.

With Docker, rackmap can't see the host's Tailscale, so Settings reports it as not installed.
Run the same thing on the host instead, pointed at the published port:
`sudo tailscale serve --bg --https=443 https+insecure://127.0.0.1:8443`.

Failed sign-ins are counted per address. For requests passed on by Tailscale (or any proxy) on
the same machine, rackmap counts the address the proxy reports; a forwarded address from
anywhere else is ignored.

## Your sign-in

**Settings → Your sign-in** changes your password and adds a second step to it: a six-digit code
from an authenticator app (any app that does standard time-based codes). Scan the QR code or type
the key, enter one code to prove the app has it, and keep the eight recovery codes it then shows:
each gets you in once if the phone is lost. Wrong codes count toward the same one-minute lockout
as wrong passwords, and a code can't be used twice. Switching the step off needs the password.
If you are locked out for good, someone with access to the machine can clear it:
`sqlite3 rackmap.db "UPDATE users SET totp_secret = '' WHERE name = 'you'"`.

## Single sign-on

**Settings → Single sign-on** lets a provider you run (Authentik, Keycloak, anything that speaks
OpenID Connect) sign people in, so rackmap gets the same password and second step as the rest of
your services. rackmap's own name and password keep working beside it.

In the provider, create an OAuth2 / OpenID Connect application (confidential client, authorization
code) with the return address the Settings page shows, `https://<where you open rackmap>/api/sso/callback`,
one for each address you use. Then enter the issuer address exactly as the provider states it, the
client ID and secret, and the user names or emails that may sign in. Anyone not on that list is
turned away even if the provider lets them through. The first sign-in makes a rackmap account of
the same name with no password of its own. The machine rackmap runs on must be able to reach the
provider and trust its certificate.

## Connections

A connection is rackmap signing in to software running on a piece of equipment (a hypervisor, a
container host, a firewall, a switch), so what runs there shows in that equipment's details and
can be acted on from it. Add one with **Connect a service** at the bottom of the details panel;
the link is there when at least one kind of connection is installed.

Every connection has a level, which you choose and can change:

| Level | rackmap may |
|---|---|
| View only | look |
| Operate | also start, stop and restart what is already there |
| Full control | also consoles and snapshots, and everything else the sign-in allows |

The level is the most rackmap will ever do through that connection, whatever the sign-in itself
could do, and it is checked on the rackmap server, not just by hiding buttons.

Three more things stand between a click and an action:

- Anything that stops or restarts something asks first, naming it, and the server only carries it
  out if the name confirmed is the name the thing has at that moment.
- **Protect** marks something rackmap must never stop or restart (the firewall's virtual machine,
  or the one rackmap itself runs in). Protection has to be removed before rackmap will touch it.
- **Settings → What rackmap has done** lists every action with who asked for it, including the
  ones rackmap refused and the ones the service turned down.

**Consoles** through a connection need Full control, open in the same terminal as console-server
ports, and every opening is written to the action log.

### Writing a connection

A kind of connection is a Python module (or any object) with a name, the labels its set-up form
needs, the actions it offers and a client that signs in:

```python
NAME, DEFAULT_PORT = "My service", 8443
ACCOUNT_LABEL, ACCOUNT_EXAMPLE, SECRET_LABEL, HELP = "Account", "admin", "Password", "Shown under the form."
ACTIONS = {"restart": {"label": "Restart", "needs": "operate", "stops": True, "warning": "Shown before asking."}}

def offered(guest):            # which actions apply to this thing right now
    return ["restart"] if guest["running"] else []

class Client:
    fingerprint = None         # the certificate or host key seen, to be remembered
    def __init__(self, host, port, username, secret, pinned): ...
    def summary(self):         # raise rackmap.services.ServiceError with a message for the person
        return {"version": "", "nodes": [], "storage": [], "facts": [], "guests_title": "Things",
                "guests": [{"key": "a", "name": "alpha", "label": "thing", "running": True, "state": "running"}]}
    def act(self, guest, action): return ""
```

Make it known either by calling `rackmap.services.register("myservice", module)` or by naming it
in your package's metadata under the `rackmap.services` entry-point group. rackmap does the rest:
the form, the level, confirm-by-name, protection and the log. A connection whose plug-in is later
removed stays in the database, is not shown, and can be used again when the plug-in returns.

## Cables

A cable joins two pieces of equipment and, where known, names the socket at each end. "Cable
list" above the rack shows the recorded ones, takes new ones by hand, and, with a monitoring
source connected, lists the cables it can work out: two devices that announce themselves to each
other (LLDP), or a recorded piece of equipment whose hardware address a switch sees on one port.
Where the same address shows on several switches, the port that sees the fewest addresses is the
one suggested. Each suggestion is added or dismissed by you; nothing is recorded on its own.

Cables between equipment in the same rack are drawn beside it (the "Cables" box switches that
off), and each piece of equipment lists its own in the details panel.

## Data

Everything lives in the directory named by `RACKMAP_DATA` (default `./data`): one SQLite file,
`rackmap.db`, the key for saved passwords, `secret.key`, and downloaded pictures. Back the
directory up and keep it private; delete it to start again.

## Develop

```
.venv/bin/pip install pytest httpx
.venv/bin/python -m pytest
```

## Licence

The open-source part is under the GNU Affero General Public License, version 3 (`LICENSE`): you
may use, change and share it, and if you distribute a changed version, or let other people use
one over a network, you must offer them its source under the same licence.

Bundled with it, under their own terms: xterm.js (MIT, `rackmap/static/vendor/XTERM-LICENSE`) and
an equipment catalogue built from the NetBox community device-type library (see "Equipment
database").
