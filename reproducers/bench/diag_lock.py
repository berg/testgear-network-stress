"""Is exclusive locking slow on its own, or only against a busy sync channel?"""
import time, threading
import pyvisa
from pyvisa import constants
RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"
rm = pyvisa.ResourceManager("@py"); inst = rm.open_resource(RN, open_timeout=5000)
inst.timeout = 5000; lib, sess = inst.visalib, inst.session
inst.query("*IDN?")

# (a) lock/unlock with an idle sync channel
t0 = time.time(); n = 0
while time.time() - t0 < 3.0:
    lib.lock(sess, constants.Lock.exclusive, 2000, None); lib.unlock(sess); n += 1
print(f"idle sync channel:  {n:5d} lock cycles in 3s  ({3000.0/max(n,1):.1f} ms each)")

# (b) same, with the sync channel hammered
stop = threading.Event(); q = [0]
def hammer():
    while not stop.is_set():
        inst.query("*IDN?"); q[0] += 1
t = threading.Thread(target=hammer); t.start()
t0 = time.time(); n = 0; slow = 0
while time.time() - t0 < 3.0:
    s = time.time()
    try:
        lib.lock(sess, constants.Lock.exclusive, 2000, None); lib.unlock(sess); n += 1
    except Exception as e:
        slow += 1
    if time.time() - s > 0.5: slow += 0
stop.set(); t.join(10)
print(f"busy sync channel:  {n:5d} lock cycles in 3s  ({3000.0/max(n,1):.1f} ms each), "
      f"{slow} failures, {q[0]} queries alongside")
inst.close(); rm.close()
