#!/usr/bin/env python3

""" hts_multistream High Throughput Stream from up to 16 UUTS V4

    - data on local SFP/AFHBA
    - control on Ethernet

positional arguments:
  uutnames              uuts

options:
  -h, --help            show this help message and exit
  --clk                 int|ext|zclk|xclk,fpclk,SR,[FIN]
  --trg                 int|ext,rising|falling
  --sim                 s1[,s2,s3..] list of sites to run in simulate mode
  --trace               1 : enable command tracing
  --auto_soft_trigger   force soft trigger generation
  --clear_counters      clear all counters SLOW
  --spad                scratchpad, eg 1,16,0
  --decimate            decimate amount
  --nbuffers            max capture in buffers
  --secs SECS           max capture in seconds
  --map MAP             uut:rport:site see example
  --sig_gen             signal gen to trigger when all uuts armed
  --delete              delete stale data
  --recycle             overwrite data
  --check_spad          check spad is sequential
  --dry_run             run setup but dont start streams or uuts
  -wr, --wrtd_txi       Command first box to send this trigger when all units are in ARM state
  -d0, --SIG_SRC_TRG_0  Set trigger d0 source
  -d1, --SIG_SRC_TRG_1  Set trigger d1 source
  --RTM_TRANSLEN        Set rtm_translen for each uut
  --mtrg                value:HDMI|EXT, works with free-running master trigger
  --verbose             increase verbosity
  --hex_str             generate hexdump cmd

Recommendation: --secs is really a timeout, use --nbuffers for exact data length

Usage:

Stream from 2 uuts:

    ./HAPI/hts_multistream.py acq2106_133 acq2106_176

Stream from 2 uuts for 2000 buffers

    ./HAPI/acq2106/hts_multistream.py --nbuffers=2000 acq2106_133 acq2106_176

Stream each site different port:

    ./HAPI/acq2106/hts_multistream.py ---map=133:A:1/133:B:2 acq2106_133

Stream for 1hr while checking spad:

    ./HAPI/acq2106/hts_multistream.py --spad=1,8,1 --secs=3600 --check=1 acq2206_009

Warning: If data rate exceeds bandwidth uut will stay in arm

    --map=  uut:rport:sites
        ex.
            ALL:BOTH:ALL
            067:A:1,2,3/067:B:4,5,6
"""

import acq400_hapi
from acq400_hapi import PR
from acq400_hapi.acq400_print import DISPLAY
from acq400_hapi import afhba404
import afhba_check_links
import argparse
import time
import os
import re
import subprocess
import psutil
from threading import Thread
import traceback

def get_parser():
    parser = argparse.ArgumentParser(description='High Throughput Stream from up to 16 UUTS')
    acq400_hapi.Acq400UI.add_args(parser, transient=False)
    parser.add_argument('--spad', default=None, help="scratchpad, eg 1,16,0")
    parser.add_argument('--decimate', default=None, help='decimate amount')
    parser.add_argument('--nbuffers', type=int, default=5000, help='max capture in buffers')
    parser.add_argument('--secs', default=0, type=int, help="max capture in seconds")
    parser.add_argument('--map', default="ALL:BOTH:ALL", help='uut:port:site ie --map=67:A:1/67:B:2/130:BOTH:ALL ')
    parser.add_argument('--sig_gen', default=None, help='Signal gen to trigger when all uuts armed')
    parser.add_argument('--delete', default=1, type=int, help='delete stale data')
    parser.add_argument('--recycle', default=1, type=int, help='overwrite data')
    parser.add_argument('--check_spad', default=0, type=int, help='check spad is sequential')
    parser.add_argument('--dry_run', default=0, type=int, help='run setup but dont start streams or uuts')
    parser.add_argument('-wr', '--wrtd_txi', default=None, help='Command first box to send this trigger when all units are in ARM state')
    parser.add_argument('-d0', '--SIG_SRC_TRG_0', default=None, help='Set trigger d0 source')
    parser.add_argument('-d1', '--SIG_SRC_TRG_1', default=None, help='Set trigger d1 source')
    parser.add_argument('-bl', '--RTM_TRANSLEN', default=None, help='Set burst length (RTM_TRANSLEN) for each uut')
    parser.add_argument('--mtrg', default=None, help='value:HDMI|EXT, works with free-running master trigger')
    parser.add_argument('--verbose', default=0, type=int, help='increase verbosity')
    parser.add_argument('--hex_str', default=0, type=int, help='generate hexdump cmd')
    parser.add_argument('--cpu_usage', default=0, help='display cpu usage and mean')
    parser.add_argument('--auto', default=0, type=int, help='auto set nbuffer based on space')
    parser.add_argument('--concat', default=0, type=int, help='concatenate buffers concat=2 means 3 buffers combined')
    
    parser.add_argument('uutnames', nargs='+', help="uuts")
    return parser

class UutWrapper:
    """ Encapsulates all information about one UUT """

    def __init__(self, name, args, map, streams):
        self.name = name
        self.spad = args.spad
        self.args = args
        self.state = None
        self.thread = None
        self.ended = False
        self.streams = {}
        self.__attach_api()
        self.__set_id()
        self.__data_builder(map, streams)

        if args.verbose > 0:
            print()
            print(f"UUT {self.name}")
            print(f"Spad {self.spad}")
            for lport, stream in self.streams.items():
                print(f'[{lport}] <-- {stream.rhost}:{stream.rport} {stream.sites_str}')

    def get_state(self):
        self.state =  acq400_hapi.pv(self.api.s0.CONTINUOUS_STATE)

    def get_state_forever(self):
        while True:
            self.get_state()
            time.sleep(1)

    def start(self):
        self.get_state()
        if self.state != 'IDLE':
            self.api.s0.streamtonowhered = "stop"
            time.sleep(2)
        self.api.s0.streamtonowhered = "start"

    def stop(self):
        self.api.s0.streamtonowhered = "stop"
        wc = 0
        while self.state != 'IDLE':
            time.sleep(1)
            wc += 1
            if wc > 10:
                print(f'{self.name} unable to stop, dropping out')
                return
        if self.args.verbose > 0:
            print(f'{self.name} has stopped')
        return
    
    def initialize(self):
        for lport, stream in self.streams.items():
            
            result = afhba_check_links.check_lane_status(self.name, lport, stream.rport, self.api, self.args.verbose)
            if not result:
                PR.Red('Link down: unable to fix')
                os._exit(1)
            
            data_columns = 0
            for site, chans in stream.sites.items():
                channels_per_column  = 1 if self.api[site].data32 == '1' else 2
                data_columns += chans // channels_per_column 

            spad_len = int(self.spad.split(',')[1])

            if self.args.hex_str:
                hex_cmd = f"""Hex Cmd: hexdump -e '{data_columns + spad_len}/4 "%08x," "\\n"' /mnt/afhba.{lport}/{stream.rhost}/000000/{lport}.00"""
                PR.Cyan(hex_cmd)

            if self.args.check_spad > 0:
                if not self.spad_enabled:
                   die(f'Error: Cannot check spad if no spad: {self.spad}')

                step = 1 if self.args.decimate is None else self.args.decimate
                cmd = stream.get_checker_cmd(self.args, spad_len, data_columns, step)
            else:
                cmd = stream.get_cmd(self.args)
            if self.args.verbose > 0:
                print(f"Cmd: {cmd}")
            stream.run(cmd)

    def configure(self):
        if self.spad is not None:
            self.api.s0.spad = self.spad
        else:
            self.spad = self.api.s0.spad

        self.spad_enabled = True if int(self.spad.split(',')[0]) else False
        if self.spad_enabled:
            for sp in ('1', '2', '3', '4' , '5', '6', '7'):
                self.api.s0.sr("spad{}={}".format(sp, sp*8))

        acq400_hapi.Acq400UI.exec_args(self.api, self.args)
        self.api.s0.run0 = f'{self.api.s0.sites} {self.spad}'
        if self.args.decimate is not None:
            self.api.s0.decimate = self.args.decimate
        if self.args.SIG_SRC_TRG_0 is not None:
            self.api.s0.SIG_SRC_TRG_0 = self.args.SIG_SRC_TRG_0
        if self.args.SIG_SRC_TRG_1 is not None:
            self.api.s0.SIG_SRC_TRG_1 = self.args.SIG_SRC_TRG_1
        if self.args.wrtd_txi is not None:
            self.api.s0.SIG_SRC_TRG_1 = 'WRTT1'
        if self.args.RTM_TRANSLEN is not None:
            self.api.s1.RTM_TRANSLEN = self.args.RTM_TRANSLEN

        self.__setup_comms_aggregators()
        if self.args.verbose > 0:
            if self.args.RTM_TRANSLEN is not None:
                PR.Yellow(f'Configuring {self.name}: rtm_translen {self.api.s1.RTM_TRANSLEN} ssb {self.api.s0.ssb} {self.args.buffer_len}MB buffers')
            else:
                PR.Yellow(f'Configuring {self.name}: ssb {self.api.s0.ssb} {self.args.buffer_len}MB buffers')


    def __setup_comms_aggregators(self):
        for lport, stream in self.streams.items():
            comm_site = getattr(self.api, f'c{stream.rport}')
            agg_str = f'sites={stream.sites_str} on'
            comm_site.aggregator = agg_str
            if self.spad_enabled:
                comm_site.spad = 1
            else:
                comm_site.spad = 0
            if self.args.decimate is not None:
                comm_site.decimate = self.args.decimate

    def __attach_api(self):
        try:
            self.api = acq400_hapi.factory(self.name)
        except Exception:
            die(f'Error: Connection failed {self.name}')

    def __set_id(self):
        hostname = self.api.s0.HN
        match = re.search(r'^.+_([0-9]{3})$', hostname)
        if not match:
            die(f'Error: {self.name} Hostname {hostname} is invalid')
        self.name = match.group()
        self.id = match.group(1).lstrip('0')

    def __data_builder(self, map, streams):
        if self.name not in streams:
            die(f'Error: {self.name} has no connections')

        self.ports = self.__get_mapped_sites(map)
        
        for lport in streams[self.name]:
            rport = streams[self.name][lport]['rport']
            if rport in self.ports:
                self.streams[lport] = Stream(lport, rport, self.name, self.ports[rport])

    def __get_mapped_sites(self, map):
        # pgm: we ONLY want to look at sites already in the s0 aggregator.
        #site_list = self.__get_sitelist()
        site_list = self.__get_aggregator_sitelist()
        out = {}
        if 'ALL' in map:
            map[self.id] = map['ALL'].copy()
        if self.id not in map:
            die(f'Error: {self.name} has no valid map')
        for port in map[self.id]:
            out[port] = {}
            map[self.id][port] = map[self.id][port].split(',')

            if map[self.id][port][0] == 'ALL':
                out[port] = site_list
                continue
            if map[self.id][port][0] == 'SPLIT':
                out['A'] = dict(list(site_list.items())[len(site_list)//2:])
                out['B'] = dict(list(site_list.items())[:len(site_list)//2])
                break
            for key in map[self.id][port]:
                if key in site_list:
                    out[port][key] = site_list[key]
            if not out[port]:
                die(f'Error: {self.name} has no valid sites')
        return out

    def __get_aggregator_sitelist(self):
        sites = {}
        for site in self.api.get_aggregator_sites():
            site_conn = getattr(self.api, f's{site}')
            sites[str(site)] = int(site_conn.active_chan)
        return sites

class Stream:
    def __init__(self, lport, rport, rhost, sites):
        self.lport = lport
        self.rport = rport
        self.rhost = rhost
        self.sites = sites
        self.sites_str = ','.join(self.sites.keys())
        self.outroot = f"/mnt/afhba.{self.lport}/{self.rhost}"
        self.logfile = f"{self.outroot}/checker.log"
        Stream.kill_if_active(lport)

    def get_cmd(self, args):
        os.system(f"sudo mkdir -p {self.outroot} -m 0777")
        cmd = f"sudo RTM_DEVNUM={self.lport} NBUFS={args.nbuffers} CONCAT={args.concat} RECYCLE={args.recycle} OUTROOT={self.outroot} ./STREAM/rtm-t-stream-disk"
        return cmd
        
    def get_checker_cmd(self, args, spad_len, data_columns, step):
        total_columns = data_columns + spad_len
        cmd = self.get_cmd(args)
        cmd += f' | ./FUNCTIONAL_TESTS/isramp -N1 -m {total_columns} -c {data_columns} -s {step} -i 1 -L {self.logfile}'
        os.system(f"umask 000 ; touch {self.logfile}")
        return cmd
        
    def run(self, cmd):
        self.process = subprocess.Popen(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        time_start = time.time()
        pid = afhba404.get_stream_pid(self.lport)
        while True:
            if pid != 0:
                PR.Green(f'Started afhba.{self.lport} with PID {pid}')
                self.pid = pid
                break
            if time.time() - time_start > 5:
                die(f'Error: afhba.{self.lport} failed to start')
            pid = afhba404.get_stream_pid(self.lport)
            time.sleep(0.5)

    def read_state(self):
        return afhba404.get_stream_state(self.lport)
    
    def read_results(self):
        if os.path.exists(self.logfile):
            return open(self.logfile, "r").readline().strip()
        return None

    @staticmethod
    def get_config():
        #returns dict with host AFHBA connections
        config = {}
        for conn in afhba404.get_connections().values():
            if conn.uut not in config:
                config[conn.uut] = {}
            config[conn.uut][conn.dev] = {}
            config[conn.uut][conn.dev]['rport'] = conn.cx
        return config
    
    @staticmethod
    def kill_if_active(lport):
        pid = afhba404.get_stream_pid(lport)
        if pid == 0:
            return

        PR.Yellow(f'Warning: Killing afhba.{lport} with pid: {pid}')
        cmd = 'sudo kill -9 {}'.format(pid)
        result = os.system(cmd)
        retry = 0
        while retry < 4:
            time.sleep(1)
            if afhba404.get_stream_pid(lport) == 0:
                return
            retry += 1

        die(f'Fatal Error: Stream failed to die {lport}')

def stop_uuts(uut_collection):
    threads = []
    for uut_item in uut_collection:
        if not uut_item.ended:
            t = Thread(target=uut_item.stop)
            t.start()
            threads.append(t)
            uut_item.ended = True

def object_builder(args):
    stream_config = Stream.get_config()
    map = get_parsed_map(args.map)
    uut_collection = []
    for uut_name in args.uutnames:
        new_uut = UutWrapper(uut_name, args, map, stream_config)
        uut_collection.append(new_uut)
    return uut_collection

def get_parsed_map(maps):
    valid_ports = ['A', 'B', 'C', 'BOTH']
    maps = maps.split('/')
    port_map = {}
    for map in maps:
        uutname, port, sites = map.upper().split(':')
        uutname = uutname.lstrip('0')
        if port not in valid_ports:
            die(f'ERROR: Invalid port: {port}')
        if uutname not in port_map:
            port_map[uutname] = {}
        if port == 'BOTH':
            port_map[uutname]['A'] = sites
            port_map[uutname]['B'] = sites
            port_map[uutname]['C'] = sites
            continue
        port_map[uutname][port] = sites

    return port_map

def read_knob(knob):
    with open(knob, 'r') as f:
        return f.read().strip()

def configure_host(uut_collection, args):
    if not os.path.ismount("/mnt"):
        die(f'Error: /mnt is not a ramdisk')

    if args.delete:
        cmd = 'sudo rm  -rf /mnt/afhba.*'
        PR.Yellow(f'Erasing /mnt/afhba.*')
        os.system(cmd)

    lport = list(uut_collection[0].streams.keys())[0]
    args.buffer_len = int(afhba404.get_buffer_len(lport) / 1024 / 1024)

    if not args.recycle:
        if args.secs:
            die(f'Error: --secs cannot be used if --recycle off')
        PR.Yellow('Warning: recycling disabled')
        total_streams = 0
        free_memory = int(getattr(psutil.virtual_memory(), 'free')/1024/1024)
        for uut_item in uut_collection:
            total_streams += len(uut_item.streams)
        if args.auto:
            args.nbuffers = int((free_memory - 2048) / total_streams)
            PR.Blue(f'nbuffers auto set to {args.nbuffers}')
        memory_needed = total_streams * args.nbuffers
        PR.Blue(f'Memory needed: {memory_needed} MB')
        PR.Blue(f'Memory available: {free_memory} MB')
        if memory_needed > free_memory - 1024:
            die(f'Error: Needed memory exceeds safe usage')
    if args.secs:
        args.t_mins, args.t_secs = divmod(args.secs, 60)
        args.nbuffers = 9999999999

def die(message):
    PR.Red(message)
    os._exit(1)

class ReleasesTriggerWhenReady:
    """ handles all trigger types from UI, enables them at the right time """
    def null_trg_action(self, all_armed):
        return 0

    def sig_gen_trg_action(self, all_armed):
        rc = 0
        if all_armed and not self.triggered:
            try:
                acq400_hapi.Agilent33210A(self.args.sig_gen).trigger()
                self.trg_msg = f'Triggered {{GREEN}}{self.args.sig_gen}'
            except Exception:
                self.trg_msg = f'Could not trigger {{RED}}{self.args.sig_gen}'
                rc = -1
            self.triggered = True
        return rc

    def wrtd_txi_trg_action(self, all_armed):
        if all_armed and not self.triggered:
            self.trg_msg = f'Triggered wrtd_txi'
            self.top_uut.cC.sr(self.args.wrtd_txi)
            self.triggered = True
        return 0

    def mtrg_trg_action(self, all_armed):
        if all_armed and not self.triggered:
            self.trg_msg = f'Trigger mtrg {self.args.mtrg}'
            self.top_uut.s0.SIG_SRC_TRG_0 = self.args.mtrg
            self.triggered = True
        return 0

    def prep_mtrg(self):
        PR.Yellow(f'mtrg {self.args.mtrg} assume free-run , set source NONE')
        self.top_uut.s0.SIG_SRC_TRG_0 = 'NONE'

    def __init__(self, SCRN, args, uut_collection):
        self.SCRN = SCRN
        self.args = args
        self.uut_collection = uut_collection
        self.top_uut = self.uut_collection[0].api
        self.trg_msg = ''
        self.trg_action = None
        self.triggered = False

        if self.args.sig_gen is not None:
            if self.trg_action is None:
                self.trg_msg = f'Waiting to trigger {self.args.sig_gen}'
                self.trg_action = self.sig_gen_trg_action
            else:
                die('duplicate trg_action sig_gen')

        elif self.args.wrtd_txi is not None:
            if self.trg_action is None:
                self.trg_msg = f'Waiting to trigger wrtd_txi'
                self.trg_action = self.wrtd_txi_trg_action
            else:
                die('duplicate trg_action wrtd_txi')

        elif self.args.mtrg is not None:
            if self.trg_action is None:
                self.trg_msg = f'Waiting to trigger mtrg {self.args.mtrg}'
                self.trg_action = self.mtrg_trg_action
                self.prep_mtrg()
            else:
                die('duplicate trg_action mtrg')

        else:
            self.trg_action = self.null_trg_action

    def __call__(self, all_armed):
        rc = self.trg_action(all_armed)
        self.SCRN.add(f'{self.trg_msg} {{RESET}}')
        self.SCRN.add_line('')
        return rc


def hot_run_init(uut_collection):
    def thread_wrapper(uut_item):
        uut_item.configure()
        uut_item.initialize()

    total_streams = 0
    threads = []
    for uut_item in uut_collection:
        new_thread = Thread(target=thread_wrapper, args=(uut_item, ))
        new_thread.start()
        threads.append(new_thread)
        total_streams += len(uut_item.streams)

    for thread in threads:
        thread.join()

    return total_streams

def hot_run_start(uut_collection):
    def thread_wrapper(uut_item):
        uut_item.thread = Thread(target=uut_item.get_state_forever)
        uut_item.thread.daemon = True
        uut_item.thread.start()
        uut_item.start()

    for uut_item in uut_collection:
        Thread(target=thread_wrapper, args=(uut_item, )).start()

def hot_run_status_update_wrapper(SCRN, args, uut_collection):
    def hot_run_status_update():
        armed_uuts = 0
        running_uuts = 0
        ended_streams = 0

        for uut_item in uut_collection:
            SCRN.add(f'{uut_item.name} ')
            if uut_item.state == 'RUN':
                running_uuts += 1
                uut_item.poll_delay = 5
                SCRN.add(f'{{GREEN}}{uut_item.state}{{RESET}}:')
            elif uut_item.state == 'ARM':
                armed_uuts += 1
                SCRN.add(f'{{ORANGE}}{uut_item.state}{{RESET}}:')
            else:
                SCRN.add(f'{{RED}}{uut_item.state}{{RESET}}:')
            SCRN.end()

            for lport, stream in uut_item.streams.items():
                sstate = stream.read_state()
                sites = stream.sites_str
                rport = stream.rport
                SCRN.add_line(f'{{TAB}}{sites}:{rport}{{ORANGE}} --> {{RESET}}afhba.{lport:2}')
                SCRN.add_line(f'{{TAB}}{{TAB}}{{BOLD}}{sstate.rx_rate * args.buffer_len}MB/s Total Buffers: {int(sstate.rx) * args.buffer_len:,} Status: {sstate.STATUS}{{RESET}}')
                if args.check_spad:
                    SCRN.add_line(f'{{TAB}}{{TAB}}Spad Checking {stream.read_results()}')

                if sstate.STATUS == 'STOP_DONE':
                    ended_streams += 1

        return armed_uuts, running_uuts, ended_streams, armed_uuts == len(uut_collection), running_uuts == len(uut_collection)
    return hot_run_status_update

def dry_run(args, uut_collection):
    if args.verbose > 0:
        print("new style Stream.get_stream_conns {}".format(Stream.get_config()))
    top_uut = uut_collection[0].api

    for uut_item in uut_collection:
        uut_item.configure()
    if args.wrtd_txi is not None:
        print(f'wrtd: {args.wrtd_txi}')
        top_uut.cC.sr(args.wrtd_txi)
    exit(PR.Yellow('Dry Run Complete'))


class RateLimiter:
    """ limits cycle rate """
    def __init__(self, _cycle_max):
        self.cycle_max = _cycle_max
        self.cycle_start = time.time()

    def __call__(self):
            cycle_length = time.time() - self.cycle_start
            sleep_time = 0 if cycle_length > self.cycle_max else self.cycle_max - cycle_length
            time.sleep(sleep_time)
            self.cycle_start = time.time()

def hot_run(args, uut_collection):
    SCRN = DISPLAY()
    total_streams = hot_run_init(uut_collection)
    release_trigger_when_ready = ReleasesTriggerWhenReady(SCRN, args, uut_collection)
    hot_run_status_update = hot_run_status_update_wrapper(SCRN, args, uut_collection)
    hot_run_start(uut_collection)
    sec_count = 0
    all_running = False
    all_armed = False
    time_start = time.time()
    rate_limiter = RateLimiter(0.5)

    usage_arr = []
    psutil.cpu_percent()

    try:
        while True:
            SCRN.add_line('')
            SCRN.add('{REVERSE} ')
            if args.secs and all_running:
                if not sec_count:
                    time_start = time.time()
                sec_count = time.time() - time_start
                if args.secs <= 60:
                    SCRN.add("{0:.1f}/{1} secs ", sec_count, args.secs)
                else:
                    c_mins, c_secs = divmod(sec_count, 60)
                    SCRN.add(f'{int(c_mins)}:{int(c_secs):02}/{int(args.t_mins)}:{int(args.t_secs):02} mins ')
                SCRN.add(f'Buffer Length: {args.buffer_len}MB ')
            else:
                SCRN.add("{0:.0f} secs ",time.time() - time_start)
                SCRN.add("Max: {0}MB Buffer Length: {1}MB ", args.nbuffers * args.buffer_len, args.buffer_len)
            
            if args.cpu_usage and all_running:
                cur_usage = psutil.cpu_percent()
                usage_arr.append(cur_usage)
                SCRN.add(f'CPU Usage {cur_usage}% ')

            if release_trigger_when_ready(all_armed) < 0:
                break

            armed_uuts, running_uuts, ended_streams, all_armed, all_running = hot_run_status_update()

            if sec_count and time.time() - time_start > args.secs:
                SCRN.add_line('{BOLD}Time Limit Reached Stopping{RESET}')
                if running_uuts > 0:
                    stop_uuts(uut_collection)
                else:
                    break
            elif ended_streams == total_streams:
                SCRN.add_line('{BOLD}Buffer limit Reached Stopping{RESET}')
                if running_uuts > 0:
                    stop_uuts(uut_collection)
                else:
                    break

            SCRN.render()
            rate_limiter()

        SCRN.render(False)                     # normal exit

    except KeyboardInterrupt:
        SCRN.render_interrupted()
        PR.Red('Interrupt!')
        stop_uuts(uut_collection)
    except Exception as e:
        SCRN.render_interrupted()
        PR.Red('Fatal Error')
        stop_uuts(uut_collection)
        print(e)
        print(traceback.format_exc())
    if len(usage_arr) > 1:
        mean_usage = round(sum(usage_arr) / len(usage_arr))
        print(f"Mean Usage {mean_usage}%")

    print('Run Done')
    time.sleep(1)

def run_main(args):
    uut_collection = object_builder(args)
    configure_host(uut_collection, args)

    if args.dry_run:
        dry_run(args, uut_collection)
    else:
        hot_run(args, uut_collection)

if __name__ == '__main__':
    run_main(get_parser().parse_args())
