"""Generate rAthena packets over a TCP socket bound only to local loopback."""

import socket
import struct
from threading import Thread

from rathena_feed import PacketStream


def pack_position(x, y):
    return ((x << 14) | (y << 4)).to_bytes(3, "big")


def pack_movement(x, y, to_x, to_y):
    return ((x << 38) | (y << 28) | (to_x << 18) | (to_y << 8) | 0x88).to_bytes(6, "big")


def map_move(name, x, y):
    return struct.pack("<H16sHH", 0x0091, (name + ".gat").encode("ascii"), x, y)


def entity(opcode, aid, job, x, y):
    walking = opcode == 0x09FD
    size = {0x09FD: 114, 0x09FE: 107, 0x09FF: 108}[opcode]
    data = bytearray(size)
    struct.pack_into("<HHBIIH", data, 0, opcode, size, 5, aid, 0, 200)
    struct.pack_into("<H", data, 23, job)
    if walking:
        data[67:73] = pack_movement(x, y, x + 3, y + 2)
    else:
        data[63:66] = pack_position(x, y)
    return data


def scenario():
    yield 0, struct.pack("<HI", 0x0283, 900001)
    yield 0, struct.pack("<HI", 0x02EB, 0) + pack_position(155, 180) + b"\x05\x05\0\0"
    yield 0, map_move("prontera", 155, 180)
    while True:
        yield 0, entity(0x09FE, 50001, 1002, 151, 182)
        yield 0, entity(0x09FF, 50002, 1014, 159, 181)
        yield 0, struct.pack("<HIIHBHHBBHBH", 0x0ADD, 80001, 578, 0, 1, 157, 177, 8, 8, 3, 0, 0)
        yield 0, struct.pack("<HIHHBHHBBH", 0x084B, 80002, 921, 0, 1, 153, 176, 8, 8, 1)
        yield 1, entity(0x09FD, 50001, 1002, 151, 182)
        yield 1, struct.pack("<HI", 0x0087, 0) + pack_movement(155, 180, 157, 180)
        yield 1, struct.pack("<HI", 0x00A1, 80001)
        yield .5, struct.pack("<HIB", 0x0080, 50002, 0)
        yield 1, map_move("payon", 150, 150)
        yield 0, entity(0x09FE, 50003, 1002, 150, 154)
        yield 1.5, map_move("prontera", 155, 180)


def send_scenario(listener, stop):
    with listener.accept()[0] as connection:
        for delay, message in scenario():
            if stop.wait(delay):
                return
            # Split headers and payloads so the receiver never relies on send boundaries.
            connection.sendall(message[:1])
            connection.sendall(message[1:])


class Loopback:
    def __init__(self, world, lengths, stop, receive_directly):
        self.world = world
        self.stream = PacketStream(lengths, world.consume) if receive_directly else None
        self.stop = stop
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(1)
        self.port = self.listener.getsockname()[1]

    def run(self):
        def serve():
            try:
                send_scenario(self.listener, self.stop)
            except OSError:
                if not self.stop.is_set():
                    raise

        server = Thread(target=serve, daemon=True)
        server.start()
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=.5) as client:
                while not self.stop.is_set():
                    try:
                        data = client.recv(4096)
                    except TimeoutError:
                        continue
                    if not data:
                        break
                    if self.stream:
                        self.stream.feed(data)
        finally:
            self.world.reset()
            server.join(1)

    def close(self):
        self.listener.close()
