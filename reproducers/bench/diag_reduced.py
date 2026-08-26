"""Soak with only operations that BOTH upstream and the new code support."""
import random, sys, time, collections
import pyvisa
from pyvisa import constants
from pyvisa.constants import StatusCode
RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"
seed = int(sys.argv[1]); limit = int(sys.argv[2]) if len(sys.argv) > 2 else 3000
rng = random.Random(seed)
rm = pyvisa.ResourceManager("@py"); inst = rm.open_resource(RN, open_timeout=10000)
inst.timeout = 5000; lib, sess = inst.visalib, inst.session
idn = inst.query("*IDN?").strip(); big = inst.query("*LRN?")
OPS = ["query","query","query","big_query","partial_read","read_stb","clear","flush","srq"]
EXTRA = sys.argv[3] if len(sys.argv) > 3 else ""
if EXTRA: OPS += [EXTRA, EXTRA]
hist = collections.deque(maxlen=6); t0 = time.time()
for i in range(limit):
    op = rng.choice(OPS)
    try:
        if op == "query":
            if inst.query("*IDN?").strip() != idn: raise RuntimeError("bad idn")
        elif op == "big_query":
            if inst.query("*LRN?") != big: raise RuntimeError("bad lrn")
        elif op == "partial_read":
            lib.write(sess, b"*LRN?\n"); got = bytearray()
            while len(got) < len(big):
                d, st = lib.read(sess, rng.choice((1,13,512,8192)))
                if not d: break
                got.extend(d)
                if st == StatusCode.success: break
        elif op == "read_stb": inst.read_stb()
        elif op == "clear": lib.clear(sess)
        elif op == "flush": lib.flush(sess, constants.BufferOperation.discard_read_buffer)
        elif op == "lock":
            k = rng.choice([constants.Lock.exclusive, constants.Lock.shared])
            try:
                key, st = lib.lock(sess, k, 1000, None)
                if st == StatusCode.success: lib.unlock(sess)
            except pyvisa.errors.VisaIOError as e:
                if e.error_code != StatusCode.error_timeout: raise
        elif op == "ren":
            lib.gpib_control_ren(sess, rng.choice(list(constants.RENLineOperation)))
        elif op == "trigger":
            lib.assert_trigger(sess, constants.TriggerProtocol.default)
        elif op == "srq":
            inst.write("*CLS"); inst.write("*ESE 1"); inst.write("*SRE 32"); inst.write("*OPC")
        hist.append(op)
    except Exception as e:
        print(f"  seed={seed}: FAILED at op #{i} ({time.time()-t0:.1f}s) on {op}: "
              f"{type(e).__name__}: {str(e)[:60]}  after {list(hist)}")
        break
else:
    print(f"  seed={seed}: survived {limit} ops in {time.time()-t0:.1f}s")
try: inst.close()
except Exception: pass
