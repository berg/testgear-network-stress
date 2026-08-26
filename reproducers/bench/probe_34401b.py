"""Attribute each instrument error to the operation that caused it."""
import pyvisa
from pyvisa import constants
RN = "TCPIP0::127.0.0.1::hislip23,4880::INSTR"
rm = pyvisa.ResourceManager("@py")
inst = rm.open_resource(RN, open_timeout=5000); inst.timeout = 4000
lib, sess = inst.visalib, inst.session

def drain():
    out = []
    for _ in range(20):
        e = inst.query("SYST:ERR?").strip()
        if e.startswith(("+0,", "0,")): break
        out.append(e)
    return out

drain()
def step(label, fn):
    try:
        r = fn()
        errs = drain()
        print(f"{label:28s} -> {str(r)[:38]:40s} errors={errs}")
    except Exception as e:
        errs = drain()
        print(f"{label:28s} -> !! {type(e).__name__:22s} errors={errs}")

step("assert_trigger", lambda: lib.assert_trigger(sess, constants.TriggerProtocol.default))
step("assert_trigger x3", lambda: [lib.assert_trigger(sess, constants.TriggerProtocol.default) for _ in range(3)])
step("clear", lambda: lib.clear(sess))
step("trigger x3 + clear", lambda: ([lib.assert_trigger(sess, constants.TriggerProtocol.default) for _ in range(3)], lib.clear(sess)))
for m in constants.RENLineOperation:
    step(f"REN {m.name}", lambda m=m: lib.gpib_control_ren(sess, m))
step("lock/unlock", lambda: (lib.lock(sess, constants.Lock.exclusive, 2000, None), lib.unlock(sess)))
step("read_stb", lambda: hex(inst.read_stb()))
step("query *IDN?", lambda: inst.query("*IDN?").strip())
inst.close()
