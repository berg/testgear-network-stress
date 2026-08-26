"""Replay the soak mix with the same seed, stopping at the first failure."""
import random, sys, time, traceback, collections
import pyvisa
from pyvisa import constants
from pyvisa.constants import StatusCode

RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"
rng = random.Random(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
rm = pyvisa.ResourceManager("@py"); inst = rm.open_resource(RN, open_timeout=10000)
inst.timeout = 5000; lib, sess = inst.visalib, inst.session
idn = inst.query("*IDN?").strip(); big = inst.query("*LRN?")

OPS = ["query","query","query","big_query","partial_read","read_stb","trigger",
       "ren","lock","clear","flush","attr","srq"]
hist = collections.deque(maxlen=25)
t0 = time.time()
for i in range(2_000_000):
    op = rng.choice(OPS); detail = op
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
        elif op == "trigger": lib.assert_trigger(sess, constants.TriggerProtocol.default)
        elif op == "ren":
            m = rng.choice(list(constants.RENLineOperation)); detail = f"ren:{m.name}"
            lib.gpib_control_ren(sess, m)
        elif op == "lock":
            k = rng.choice([constants.Lock.exclusive, constants.Lock.shared])
            detail = f"lock:{k.name}"
            try:
                key, st = lib.lock(sess, k, 1000, None)
                if st == StatusCode.success: lib.unlock(sess)
            except pyvisa.errors.VisaIOError as e:
                if e.error_code != StatusCode.error_timeout: raise
        elif op == "clear": lib.clear(sess)
        elif op == "flush": lib.flush(sess, constants.BufferOperation.discard_read_buffer)
        elif op == "attr": inst.get_visa_attribute(constants.ResourceAttribute.tcpip_hislip_max_message_kb)
        elif op == "srq":
            inst.write("*CLS"); inst.write("*ESE 1"); inst.write("*SRE 32"); inst.write("*OPC")
        hist.append(detail)
    except Exception as e:
        print(f"FIRST FAILURE at op #{i} after {time.time()-t0:.1f}s: {op}\n  {type(e).__name__}: {e}")
        print("\n  preceding operations (oldest first):")
        for h in hist: print(f"    {h}")
        traceback.print_exc()
        break
else:
    print("no failure")
inst.close()
