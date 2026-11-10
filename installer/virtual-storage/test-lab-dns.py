"""DNS bridge for isolated guests, using only the build host's working resolver."""

import socket
import socketserver
import struct


class Resolver(socketserver.BaseRequestHandler):
    def handle(self):
        query, stream = self.request
        at, labels = 12, []
        try:
            while query[at]:
                size = query[at]
                labels.append(query[at + 1:at + size + 1].decode("ascii"))
                at += size + 1
            at += 1
            kind, cls = struct.unpack("!HH", query[at:at + 4])
            question = query[12:at + 4]
            answers = []
            if cls == 1 and kind in (1, 28):
                family = socket.AF_INET if kind == 1 else socket.AF_INET6
                try:
                    addresses = {r[4][0] for r in socket.getaddrinfo(".".join(labels),
                                 None, family, socket.SOCK_STREAM)}
                except socket.gaierror:
                    addresses = set()
                for address in addresses:
                    raw = socket.inet_pton(family, address)
                    answers.append(b"\xc0\x0c" + struct.pack("!HHIH", kind, 1, 60, len(raw)) + raw)
            header = query[:2] + b"\x81\x80" + struct.pack("!HHHH", 1, len(answers), 0, 0)
            stream.sendto(header + question + b"".join(answers), self.client_address)
        except (IndexError, UnicodeError, struct.error):
            return


class Server(socketserver.ThreadingUDPServer):
    daemon_threads = True


Server(("192.0.2.254", 53), Resolver).serve_forever()
