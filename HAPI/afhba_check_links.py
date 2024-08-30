#!/usr/bin/env python3

import acq400_hapi
from acq400_hapi import afhba404
import argparse
import time

"""
usage: afhba_check_link.py [-h] [uutnames ...]

Afhba Link Checker : checks Aurora link status, if it's down, resets optic link from the UUT with retry.

positional arguments:
  uutnames    uuts to check leave empty to check all connections

options:
  -h, --help  show this help message and exit
"""

def get_parser():
    parser = argparse.ArgumentParser(description='Afhba Link Checker')
    parser.add_argument('uutnames', nargs='*', help="uuts to check. Omit to check all connections")
    return parser

def get_connections():
    connections = {}
    for conn in afhba404.get_connections().values():
        connections.setdefault(conn.uut, {})[conn.cx] = conn.dev
    return connections

def check_links(uuts):
    connections = get_connections()

    for uutname in uuts:
        if uutname not in connections:
            if not reset_link(uutname):
                continue

    for uutname, ports in connections.items():
        for rport, lport in ports.items():
            check_lane(uutname, rport, lport)
            if uutname in uuts or len(uuts) == 0:
                check_lane(uutname, rport, lport)
        
def reset_link(uutname):
    print(f"[{uutname}] Resetting Link")

    try:
        uut = acq400_hapi.factory(uutname)
        if not hasattr(uut['A'], 'TX_DISABLE'):
            print(f"[{uutname}] Warning old firmware")
            return False
    except: 
        print(f"[{uutname}] No route to host")
        return False

    attempt = 1
    while attempt <= 5:
        for rport in ['A', 'B']:
            try:
                uut[rport].TX_DISABLE = 1
                time.sleep(0.5)
                uut[rport].TX_DISABLE = 0
                time.sleep(0.5)
            except: pass
        connections = get_connections()
        if uutname in connections:
            print(f"[{uutname}] Link connected")
            return True
    print(f"[{uutname}] Link down")
    return False

def check_lane(uutname, rport, lport):
    link_state = afhba404.get_link_state(lport)
    if link_state.LANE_UP and link_state.RPCIE_INIT:
        print(f"[{uutname}:{rport} -> afhba.{lport}] Link Good")
        return True
    reset_link(uutname)
    try:
        link_state = afhba404.get_link_state(lport)
        if link_state.RPCIE_INIT:
            print(f'[{uutname}:{rport} -> afhba.{lport}] Link Fixed')
            return True
    except: pass
    print(f"[{uutname}] Link down")
    return False

def run_main(args):
    check_links(args.uutnames)

if __name__ == '__main__':
    run_main(get_parser().parse_args())

