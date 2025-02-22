# -*- coding: utf-8 -*
# Labeless
# by Aliaksandr Trafimchuk
#
# Source code released under
# Creative Commons BY-NC 4.0
# http://creativecommons.org/licenses/by-nc/4.0

__author__ = 'a1ex_t'

# from binascii import hexlify
# import itertools
from os import path
import struct
import sys
import traceback
import ctypes as C
from ctypes import wintypes
from pehelper import PEHelper
import re
# import threading
# import time
# import multiprocessing
from logs import make_logger
import pehelper_decl as D

logger = make_logger()

import ollyapi as oa
try:
    import rpc_pb2 as rpc
except ImportError as e:
    import py_olly
    py_olly.olly_log('protobuf is not installed in python')
    raise e


# information about loaded modules
modules_meta = dict()
modules_exports = dict()  # will hold pairs <ea, 'module_name.api_name'>


def make_names(names, base, remote_base):
    if not names:
        return
    ptrdiff = 0
    if base != remote_base:
        ptrdiff = remote_base - base
    for n in names:
        oa.Insertname(n.ea + ptrdiff, oa.NM_LABEL, str(n.name))
    oa.Redrawdisassembler()


def make_comments(comments, base, remote_base):
    if not comments:
        return
    ptrdiff = remote_base - base
    for cmt in comments:
        oa.Insertname(cmt.ea + ptrdiff, oa.NM_COMMENT, str(cmt.name))
    oa.Redrawdisassembler()


def get_memory_map():
    oa.Listmemory()
    t = oa.pluginvalue_to_t_table(oa.Plugingetvalue(oa.VAL_MEMORY))
    rv = rpc.GetMemoryMapResult()

    for i in xrange(t.data.n):
        mi = rv.memories.add()
        m = oa.void_to_t_memory(oa.Getsortedbyselection(t.data, i))
        module = oa.Findmodule(m.base)
        module_name = ("'%s'" % module.name) if module else ''
        mi.access = m.access
        mi.base = m.base
        mi.name = str(module_name)
        mi.size = m.size
        
    return rv


def unsafe_read_process_memory(addr, size):
    b = bytearray(size)
    n = oa.Readmemory(b, addr, size, oa.MM_RESTORE | oa.MM_SILENT)
    return None if n < size else (n, b)


def safe_read_chunked_memory_region_as_one(base, size):
    mbi = D.MEMORY_BASIC_INFORMATION()
    VirtualQueryEx = C.windll.kernel32.VirtualQueryEx
    VirtualProtectEx = C.windll.kernel32.VirtualProtectEx
    GRANULARITY = 0x1000

    h_process = wintypes.HANDLE(oa.Plugingetvalue(oa.VAL_HPROCESS))
    rv = bytearray(size)

    guarded = list()
    gpoints = dict()
    protect = 0

    queried = VirtualQueryEx(h_process, C.c_void_p(base), C.byref(mbi), C.sizeof(mbi))
    if queried:
        protect = mbi.Protect
    else:
        print >> sys.stderr, 'safe_read_chunked_memory_region_as_one: VirtualQueryEx() failed'
    if queried and mbi.Protect & D.PAGE_GUARD:
        g = {'ea': base, 'size': GRANULARITY, 'p': mbi.Protect}
        gpoints[base] = 0
        ea = base
        while True:
            ea -= GRANULARITY
            if VirtualQueryEx(h_process, C.c_void_p(ea), C.byref(mbi), C.sizeof(mbi)) and\
                    (mbi.Protect & D.PAGE_GUARD) != 0 and g['p'] == mbi.Protect:
                g['ea'] -= GRANULARITY
                g['size'] += GRANULARITY
            else:
                break

        guarded.append(g)

    for i in range(base + GRANULARITY, base + size, GRANULARITY):
        p_addr = C.c_void_p(i)
        if VirtualQueryEx(h_process, p_addr, C.byref(mbi), C.sizeof(mbi)) and\
                        mbi.Protect & D.PAGE_GUARD:
            prevaddr = i - GRANULARITY
            if prevaddr in gpoints and guarded[gpoints[prevaddr]]['p'] == mbi.Protect:
                idx = gpoints[prevaddr]
            else:
                guarded.append({'ea': i, 'size': 0, 'p': mbi.Protect})
                idx = len(guarded) - 1
            guarded[idx]['size'] += GRANULARITY
            gpoints[i] = idx

    ea = base + size - GRANULARITY
    if ea in gpoints:
        while True:
            ea += GRANULARITY
            if VirtualQueryEx(h_process, C.c_void_p(ea), C.byref(mbi), C.sizeof(mbi)) and\
                        mbi.Protect & D.PAGE_GUARD:
                guarded[-1]['size'] += GRANULARITY
            else:
                break

    # turn off page guard before read
    dummy = C.c_long()
    for g in guarded:
        for off in range(0, g['size'], GRANULARITY):
            g['ok'] = VirtualProtectEx(h_process, C.c_void_p(g['ea'] + off), GRANULARITY,
                                       C.c_long(g['p'] & ~D.PAGE_GUARD), C.byref(dummy))

    for i in range(base, base + size, GRANULARITY):
        p_addr = C.c_void_p(i)
        if VirtualQueryEx(h_process, p_addr, C.byref(mbi), C.sizeof(mbi)):
            if mbi.Protect & D.PAGE_GUARD:
                # TODO
                pass
            mem = unsafe_read_process_memory(i, GRANULARITY)
            if mem is None:
                continue
            mem = mem[1]
            if mem:
                off = i - base
                rv[off:off + GRANULARITY] = mem

    for g in guarded:
        for off in range(0, g['size'], GRANULARITY):
            if not g['ok']:
                continue
            if not VirtualProtectEx(h_process, C.c_void_p(g['ea'] + off), GRANULARITY, C.c_long(g['p']), C.byref(dummy)):
                print >> sys.stderr, 'VirtualProtectEx(ptr 0x%08X, size 0x%08X, protect 0x%08X) failed' %\
                                     (g['ea'] + off, GRANULARITY, g['p'])
    if rv and len(rv) > size:
        rv = rv[:size]
    return size, rv, protect

def disasm(ea, mem=None, size=oa.MAXCMDSIZE):
    if mem is None:
        mem = safe_read_chunked_memory_region_as_one(ea, size)
        if not mem:
            print >> sys.stderr, 'Unable to read specified memory (0x%08X of size 0x%08X)' % (ea, size)
            return None, None
        mem = buffer(mem[1])
    cmd = bytearray(mem[:min(oa.MAXCMDSIZE, len(mem))])
    dis = oa.t_disasm()
    n = oa.Disasm(cmd, len(cmd), ea, None, dis, oa.DISASM_CODE, 0)
    if n <= 0 or dis.error:
        return None, None
    return n, dis

def read_memory_regions(regions):
    oa.Listmemory()
    
    rv = rpc.ReadMemoryRegionsResult()
    for r in regions:
        mem = rv.memories.add()
        mem.addr = int(r.addr)
        mem.size = int(r.size)

        m = safe_read_chunked_memory_region_as_one(mem.addr, mem.size)
        if m is None:
            print >> sys.stderr, 'safe_read_chunked_memory_region_as_one() failed for (0x%08X, 0x%08X)' % (mem.addr, mem.size)
            continue
        mem.mem = str(m[1])
        mem.protect = int(m[2])
    return rv


def analyze_external_refs(ea_from, ea_to, increment, analysing_base, analysing_size):
    # print >> sys.stderr, 'analyze_external_refs(%08X, %08X, %08X, %08X, %08X)' % \
    #                      (ea_from, ea_to, increment, analysing_base, analysing_size)
    rv = rpc.AnalyzeExternalRefsResult()
    if ea_from > ea_to:
        print >> sys.stderr, 'Invalid arguments passed'
        return rv

    mem = safe_read_chunked_memory_region_as_one(ea_from, ea_to - ea_from)
    if not mem:
        print >> sys.stderr, 'Unable to read specified memory (0x%08X - 0x%08X)' % (ea_from, ea_to)
        return rv
    mem = buffer(mem[1])
    intptr_size = struct.calcsize("<I")
    main_module_name = oa.Findmodule(analysing_base)
    if main_module_name is None:
        main_module_name = ''
    else:
        main_module_name = path.splitext(path.basename(main_module_name.path))[0].lower()

    th = oa.Findthread(oa.Getcputhreadid())
    r = oa.ulongArray.frompointer(th.reg.r)
    rv.context.eax = r[oa.REG_EAX]
    rv.context.ecx = r[oa.REG_ECX]
    rv.context.edx = r[oa.REG_EDX]
    rv.context.ebx = r[oa.REG_EBX]
    rv.context.esp = r[oa.REG_ESP]
    rv.context.ebp = r[oa.REG_EBP]
    rv.context.esi = r[oa.REG_ESI]
    rv.context.edi = r[oa.REG_EDI]
    rv.context.eip = th.reg.ip

    global modules_exports

    scan_for_ref_api_calls(ea_from, ea_to, increment, rv=rv, mem=mem, base=analysing_base)
    used_addrs = set([])

    for ea in xrange(ea_from, ea_to, increment):
        try:
            if ea in used_addrs:
                continue
            l = ea_to - ea
            off = ea - ea_from

            if l < intptr_size:
                break
            addr = struct.unpack_from("<I", mem, off)[0]
            if addr not in modules_exports:
                continue
            symb = modules_exports[addr]
            module_name, proc_name = symb.split('.')
            if module_name == main_module_name:
                continue

            v = rv.api_constants.add()
            v.ea = ea
            v.module = module_name
            v.proc = proc_name

        except Exception as exc:
            print >> sys.stderr, 'Exception: %r\r\n%s' % (exc, traceback.format_exc().replace('\n', '\r\n'))
    print 'AnalyzeExternalRefs: found %u' % len(rv.api_constants)
    print rv
    return rv


def scan_for_ref_api_calls(ea_from, ea_to, increment, rv, base, mem):
    # import inspect
    if ea_from > ea_to:
        print >> sys.stderr, 'Invalid arguments passed'
        return None
    logger.info(('scan_for_ref_api_calls(ea_from=0x%08X, ea_to=0x%08X, increment=0x%08X, base=0x%08X)\n' +
                'getting modules meta') % (ea_from, ea_to, increment, base))

    global modules_meta
    global modules_exports
    logger.info('scan_for_ref_api_calls() modules_meta got')

    this_module_exports = set()
    for name, info in modules_meta.items():
        for i in xrange(len(info['base'])):
            if info['base'][i] <= ea_from < info['base'][i] + info['size'][i]:
                this_module_exports = set(map(lambda x: x['ea'], info['apis'][i]))
                print 'module found: %s, len of exports: %u' % (name, len(this_module_exports))
                break

    def isPointsToExternalDll(addr):
        if addr in modules_exports and (not this_module_exports or addr not in this_module_exports):
            return modules_exports[addr]
        return False

    for ea in xrange(ea_from, ea_to, increment):
        try:
            l = ea_to - ea
            offs = ea - ea_from
            cmd = bytearray(mem[offs: offs + min(oa.MAXCMDSIZE, l)])
            dis = oa.t_disasm()
            n = oa.Disasm(cmd, len(cmd), ea, None, dis, oa.DISASM_CODE, 0)
            if n <= 0 or dis.error:
                continue
            if dis.immconst:
                v = isPointsToExternalDll(dis.immconst)
                if v:
                    ref = rv.refs.add()
                    ref.ref_type = rpc.AnalyzeExternalRefsResult.RefData.REFT_IMMCONST
                    ref.module, ref.proc = v.split('.')
                    ref.v = dis.immconst
                    ref.ea = ea
                    ref.len = n
                    ref.dis = dis.result
                    print 'dis.immconst points to %s at %08X as %s bytes: %s' % (v, ea, dis.result, dis.dump)
                continue
            if dis.adrconst:
                v = isPointsToExternalDll(dis.adrconst)
                if v:
                    ref = rv.refs.add()
                    ref.ref_type = rpc.AnalyzeExternalRefsResult.RefData.REFT_ADDRCONST
                    ref.module, ref.proc = v.split('.')
                    ref.v = dis.adrconst
                    ref.ea = ea
                    ref.len = n
                    ref.dis = dis.result
                    print 'dis.adrconst points to %s at %08X as %s bytes: %s' % (v, ea, dis.result, dis.dump)
                continue
            if dis.jmpconst:
                v = isPointsToExternalDll(dis.jmpconst)
                if v:
                    ref = rv.refs.add()
                    ref.ref_type = rpc.AnalyzeExternalRefsResult.RefData.REFT_JMPCONST
                    ref.module, ref.proc = v.split('.')
                    ref.v = dis.jmpconst
                    ref.ea = ea
                    ref.len = n
                    ref.dis = dis.result
                    print 'dis.jmpconst points to %s at %08X as %s bytes: %s' % (v, ea, dis.result, dis.dump)
                continue

                #for k, v in inspect.getmembers(dis):
                #    if '_' not in k:
                #        print "%r: %r" % (k, v)
        except Exception as exc:
            print >> sys.stderr, 'Exception: %r\r\n%s' % (exc, traceback.format_exc().replace('\n', '\r\n'))


def update_modules_meta():
    global modules_meta
    global modules_exports

    modules_meta = dict()
    modules_exports = dict()

    me32 = D.MODULEENTRY32()
    me32.dwSize = C.sizeof(D.MODULEENTRY32)
    pid = oa.Plugingetvalue(oa.VAL_PROCESSID)
    h_snap = C.windll.kernel32.CreateToolhelp32Snapshot(D.TH32CS_SNAPMODULE, pid)
    if h_snap == 0xFFFFFFFF:
        print >> sys.stderr, 'get_modules_meta(): Unable to open Toolhelp32 snapshot'
        return modules_meta

    # available_modules = set()

    ret = C.windll.kernel32.Module32First(h_snap, C.pointer(me32))
    if ret == 0:
        C.windll.kernel32.CloseHandle(h_snap)
        print >> sys.stderr, 'get_modules_meta(): Module32First() failed'
        return modules_meta

    while ret:
        modname = path.splitext(path.basename(me32.szExePath))[0].lower()
        if modname not in modules_meta or modules_meta[modname]['base'] != me32.modBaseAddr:
            mem = safe_read_chunked_memory_region_as_one(me32.modBaseAddr, me32.modBaseSize)
            print 'get_modules_meta(): %s at 0x%08X' % (modname, me32.modBaseAddr)
            if mem:
                pe = PEHelper(me32.modBaseAddr, modname, mem[1])
                exps = pe.get_exports()
                if modname in modules_meta:
                    modules_meta[modname]['base'].append(me32.modBaseAddr)
                    modules_meta[modname]['size'].append(me32.modBaseSize)
                    modules_meta[modname]['end'].append(me32.modBaseAddr + me32.modBaseSize)
                    modules_meta[modname]['apis'].append(exps)
                    # re_match_mod_ordinals = re.compile(r'%s\.#\d+' % modname, re.I)
                    modules_exports.update(pe.get_ea_to_longname_map())
                    # modules_exports = dict(filter(lambda (k, v): not re_match_mod_ordinals.match(v), modules_exports.items()))
                else:
                    mi = {
                        'path': [me32.szExePath],
                        'base': [me32.modBaseAddr],
                        'size': [me32.modBaseSize],
                        'apis': [exps],
                        'end':  [me32.modBaseAddr + me32.modBaseSize]
                    }
                    modules_meta[modname] = mi
                    modules_exports.update(pe.get_ea_to_longname_map())
        ret = C.windll.kernel32.Module32Next(h_snap, C.pointer(me32))
    C.windll.kernel32.CloseHandle(h_snap)

    # t = oa.pluginvalue_to_t_table(oa.Plugingetvalue(oa.VAL_MODULES))
    #
    # for i in xrange(t.data.n):
    #     m = oa.void_to_t_module(oa.Getsortedbyselection(t.data, i))
    #     modname = path.splitext(path.basename(m.path))[0].lower()
    #     if modname in modules_meta and modules_meta[modname]['base'] == m.base:
    #         continue
    #     available_modules.add(modname)
    #     externals = list()
    #     for off in xrange(m.codesize):
    #         name = bytearray(oa.TEXTLEN)
    #         if oa.Findname(m.codebase + off, oa.NM_EXPORT, name):
    #             name = str(name.replace('\x00', ''))
    #             externals.append({'ea': m.codebase + off, 'name': name})
    #             modules_exports[m.codebase + off] = '%s.%s' % (modname, name)
    #     mi = {
    #         'path': m.path,
    #         'base': m.base,
    #         'size': m.size,
    #         'apis': externals,
    #         'end': m.base + m.size
    #     }
    #
    #     modules_meta[modname] = mi
    # for name in filter(lambda x: x not in available_modules, modules_meta.keys()):
    #     del modules_meta[name]
    return modules_meta  # sorted(rv, key=lambda x: x['base'])


def check_pe_headers(base, size):
    update_modules_meta()

    rv = rpc.CheckPEHeadersResult()
    rv.pe_valid = False
    mem = safe_read_chunked_memory_region_as_one(base, size)
    if not mem:
        print >> sys.stderr, 'unable to read memory: 0x%08X, size: 0x%08X' % (base, size)
        return rv
    mem = mem[1]
    p = PEHelper(base, '', data=mem)
    rv.pe_valid = p.parse_headers(True)
    if not rv.pe_valid:
        print >> sys.stderr, 'PE headers are invalid'
        return rv

    exports = p.get_exports()
    for e in exports:
        ex = rv.exps.add()
        ex.ea = e['ea']
        ex.ord = e['ord']
        if e['name']:
            ex.name = e['name']

    sections = p.get_sections()
    for sec in sections:
        s = rv.sections.add()
        s.name = sec['name']
        s.va = sec['va']
        s.v_size = sec['v_size']
        s.raw = sec['raw']
        s.raw_size = sec['raw_size']
        s.characteristics = sec['ch']

    return rv