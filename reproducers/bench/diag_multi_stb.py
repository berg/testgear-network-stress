"""N simultaneous sessions each doing query+read_stb, like 04 phase 2."""
import sys, threading, traceback
import pyvisa
RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 6
ITERS = int(sys.argv[2]) if len(sys.argv) > 2 else 20
USE_STB = "nostb" not in sys.argv

res, tbs = {}, {}
def w(i):
    try:
        rm = pyvisa.ResourceManager("@py")
        inst = rm.open_resource(RN, open_timeout=5000); inst.timeout = 3000
        for k in range(ITERS):
            inst.query("*IDN?")
            if USE_STB: inst.read_stb()
        res[i] = "ok"
        inst.close(); rm.close()
    except Exception as e:
        res[i] = f"{type(e).__name__}: {str(e)[:70]}"
        tbs[i] = traceback.format_exc()

ts = [threading.Thread(target=w, args=(i,)) for i in range(N)]
for t in ts: t.start()
for t in ts: t.join(60)
ok = sum(1 for v in res.values() if v == "ok")
print(f"N={N} iters={ITERS} stb={USE_STB}: {ok}/{N} ok")
for i, v in sorted(res.items()):
    if v != "ok": print(f"   [{i}] {v}")
if tbs:
    print("\n" + list(tbs.values())[0][-900:])
