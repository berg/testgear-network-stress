"""Does heavy load make the server refuse subsequent connections, and for how long?"""
import sys, threading, time, traceback
import pyvisa
RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"

def open_n(n, label):
    res = {}
    def w(i):
        try:
            rm = pyvisa.ResourceManager("@py")
            inst = rm.open_resource(RN, open_timeout=5000); inst.timeout = 3000
            inst.query("*IDN?"); res[i] = "ok"
            inst.close(); rm.close()
        except Exception as e:
            res[i] = f"{type(e).__name__}: {str(e)[:60]}"
    ts = [threading.Thread(target=w, args=(i,)) for i in range(n)]
    for t in ts: t.start()
    for t in ts: t.join(30)
    ok = sum(1 for v in res.values() if v == "ok")
    print(f"  {label}: {ok}/{n} ok" + ("" if ok == n else f"  -> {[v for v in res.values() if v!='ok'][:2]}"))
    return ok

print("baseline (no prior load):")
open_n(6, "6 simultaneous")

print("\nnow generating heavy load for 4s...")
rm = pyvisa.ResourceManager("@py"); inst = rm.open_resource(RN, open_timeout=5000)
inst.timeout = 5000
stop = threading.Event(); n = [0]
def hammer():
    while not stop.is_set():
        try: inst.query("*IDN?"); n[0] += 1
        except Exception: pass
def stbs():
    while not stop.is_set():
        try: inst.read_stb()
        except Exception: pass
ts = [threading.Thread(target=hammer), threading.Thread(target=stbs)]
for t in ts: t.start()
time.sleep(4); stop.set()
for t in ts: t.join(10)
print(f"  did {n[0]} queries; closing session")
inst.close(); rm.close()

for delay in (0.0, 1.0, 3.0, 6.0):
    time.sleep(delay if delay else 0)
    open_n(6, f"6 simultaneous, {delay}s after load")
