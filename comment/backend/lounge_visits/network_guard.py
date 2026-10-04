"""Resolve once, reject non-public addresses and pin requests to that address."""
import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit


async def public_destination(url):
    parsed = urlsplit(url)
    try:
        records = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, parsed.port or 443,
                                                              type=socket.SOCK_STREAM)
        addresses = list(dict.fromkeys(record[4][0] for record in records))
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
            raise ValueError()
        return addresses[0]
    except Exception:
        raise ValueError('会客地址必须解析到可验证的公网地址') from None


def pin_request(request, expected, address):
    actual = urlsplit(str(request.url))
    if (actual.scheme, actual.hostname, actual.port) != (expected.scheme, expected.hostname, expected.port):
        raise ValueError('会客请求目标发生变化')
    request.headers['Host'] = expected.netloc
    request.extensions['sni_hostname'] = expected.hostname
    request.url = request.url.copy_with(host=address)
