"""Which async operation, run concurrently with sync queries, breaks things?"""
import sys, threading, time
import pyvisa
from pyvisa import constants

RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"
DURATION = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
MODE = sys.argv[1]

rm = pyvisa.ResourceManager("@py")
inst = rm.open_resource(RN, open_timeout=5000); inst.timeout = 5000
lib, sess = inst.visalib, inst.session
idn = inst.query("*IDN?").strip()
for _ in range(30):
    if inst.query("SYST:ERR?").strip().startswith(("0,", "+0,")): break

stop = threading.Event(); problems = []; n = {"sync":0, "async":0}

def sync_worker():
    while not stop.is_set():
        try:
            if inst.query("*IDN?").strip() != idn: problems.append("bad idn")
        except Exception as e: problems.append(f"query {type(e).__name__}: {e}")
        n["sync"] += 1

def async_worker():
    while not stop.is_set():
        try:
            if MODE == "stb":   inst.read_stb()
            elif MODE == "lock":
                k, st = lib.lock(sess, constants.Lock.exclusive, 2000, None)
                lib.unlock(sess)
            elif MODE == "ren": lib.gpib_control_ren(sess, constants.RENLineOperation.asrt)
            elif MODE == "all":
                inst.read_stb()
                lib.gpib_control_ren(sess, constants.RENLineOperation.asrt)
                try:
                    lib.lock(sess, constants.Lock.exclusive, 500, None); lib.unlock(sess)
                except Exception: pass
            elif MODE == "none": time.sleep(0.01); continue
        except Exception as e: problems.append(f"{MODE} {type(e).__name__}: {e}")
        n["async"] += 1

ts = [threading.Thread(target=sync_worker), threading.Thread(target=async_worker)]
for t in ts: t.start()
time.sleep(DURATION); stop.set()
for t in ts: t.join(15)

errs = []
for _ in range(30):
    e = inst.query("SYST:ERR?").strip()
    if e.startswith(("0,", "+0,")): break
    errs.append(e)
print(f"MODE={MODE:5s} sync={n['sync']:6d} async={n['async']:5d} "
      f"problems={len(problems)} scpi_errors={len(errs)}")
for p in problems[:4]: print(f"    problem: {p}")
for e in errs[:4]:     print(f"    scpi:    {e}")
inst.close(); rm.close()
