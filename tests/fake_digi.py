"""A stand-in for a Digi CM console server, for tests: SSH with a password, `connect N`,
the port sign-in prompts (with the stray NUL bytes the real one sends), then an echoing device."""

import asyncio
import threading

import asyncssh


class FakeDigi:
    def __init__(self, username="root", password="digi-secret", busy_ports=()):
        self.username, self.password, self.busy_ports = username, password, set(busy_ports)
        self.port = None
        self.key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._server = None

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._start())
        self._ready.set()
        self._loop.run_forever()

    async def _start(self, port=0):
        outer = self

        class Auth(asyncssh.SSHServer):
            def begin_auth(self, username):
                return True

            def password_auth_supported(self):
                return True

            def validate_password(self, username, password):
                return username == outer.username and password == outer.password

        async def handle(process):
            command = process.command or ""
            if not command:  # a management controller's own command line
                process.stdout.write(b"\r\n/admin1-> ")
                typed = (await process.stdin.readuntil(b"\r")).strip()
                if typed != b"console com2":
                    process.stdout.write(b"COMMAND NOT RECOGNIZED\r\n/admin1-> ")
                    process.exit(1)
                    return
                process.stdout.write(b"console com2\r\nConnected to Serial Device 2. To end type: ^\\\r\nserver login: ")
                while True:
                    try:
                        data = await process.stdin.read(1024)
                    except asyncssh.TerminalSizeChanged:
                        continue
                    if not data or b"\x04" in data:
                        break
                    process.stdout.write(b"echo:" + data)
                process.exit(0)
                return
            if not command.startswith("connect "):
                process.exit(1)
                return
            number = int(command.split()[1])
            out = process.stdout
            out.write(f"Connecting to  Port {number}...\r\n\r\nEntering character mode\r\n".encode())
            if number in outer.busy_ports:
                out.write(b"Port is busy\r\n")
                process.exit(1)
                return
            out.write(b"\x00\r\n Welcome to Digi CM 48 Console Server\r\n\r\nDigi CM 48\x00 Login : \x00")
            name = (await process.stdin.readuntil(b"\r")).strip()
            out.write(b"\r\nDigi CM 48\x00 Password : \x00")
            secret = (await process.stdin.readuntil(b"\r")).strip()
            if name.decode() != outer.username or secret.decode() != outer.password:
                out.write(b"\r\nLogin incorrect\r\n")
                process.exit(1)
                return
            out.write(b"****\r\n Entering server port, ..... type ^z for port menu.\r\n")
            out.write(f"device-on-port-{number} login: ".encode())
            while True:
                try:
                    data = await process.stdin.read(1024)
                except asyncssh.TerminalSizeChanged:
                    continue
                except (asyncssh.BreakReceived, asyncssh.SignalReceived):
                    break
                if not data or b"\x04" in data:
                    break
                out.write(b"echo:" + data)
            process.exit(0)

        self._server = await asyncssh.listen("127.0.0.1", port, server_factory=Auth, server_host_keys=[self.key],
                                              process_factory=handle, encoding=None)
        self.port = self._server.sockets[0].getsockname()[1]

    def __enter__(self):
        self._thread.start()
        self._ready.wait(10)
        return self

    def __exit__(self, *exc):
        def stop():
            self._server.close()
            self._loop.stop()
        self._loop.call_soon_threadsafe(stop)
        self._thread.join(5)

    def new_identity(self):
        """Swap the host key, as a replaced or reset console server would have."""
        self.key = asyncssh.generate_private_key("ssh-rsa", key_size=2048)
        done = threading.Event()

        async def restart():
            port = self.port
            self._server.close()
            await self._server.wait_closed()
            await self._start(port)
            done.set()

        asyncio.run_coroutine_threadsafe(restart(), self._loop)
        done.wait(10)
