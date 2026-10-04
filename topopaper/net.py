"""Shared HTTP plumbing: one polite User-Agent, the Overpass mirrors, JSON fetch.

Nominatim and Overpass ask clients to identify themselves with a contact URL;
every request topopaper makes goes through here.
"""
import json
import socket
import urllib.request

from . import __version__

PROJECT_URL = "https://github.com/aidenwboudr/topopaper"
UA = {"User-Agent": f"topopaper/{__version__} (+{PROJECT_URL})"}

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

_orig_gai = socket.getaddrinfo


def _gai_v4first(*a, **k):
    # urllib has no happy-eyeballs: on networks (or VPNs) with a broken IPv6
    # route every AAAA-first host fails outright, so try IPv4 first
    res = _orig_gai(*a, **k)
    v4 = [r for r in res if r[0] == socket.AF_INET]
    return v4 or res


def prefer_ipv4():
    socket.getaddrinfo = _gai_v4first


def request(url, data=None, timeout=30):
    return urllib.request.urlopen(
        urllib.request.Request(url, data=data, headers=UA), timeout=timeout)


def fetch_json(url, timeout=15):
    with request(url, timeout=timeout) as r:
        return json.load(r)
