"""Open N sessions SIMULTANEOUSLY from N threads and capture what breaks."""
import logging, threading, traceback
import pyvisa
logging.basicConfig(level=logging.DEBUG, format="%(threadName)s %(levelname)s %(message)s")
logging.getLogger("pyvisa").setLevel(logging.DEBUG)

RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"
N = 6
barrier = threading.Barrier(N)
results = {}

def worker(i):
    try:
        rm = pyvisa.ResourceManager("@py")
        barrier.wait()                     # force真 simultaneity
        inst = rm.open_resource(RN, open_timeout=5000); inst.timeout = 3000
        idn = inst.query("*IDN?").strip()
        results[i] = f"OK {idn[:30]}"
        inst.close(); rm.close()
    except Exception as e:
        results[i] = f"FAIL {type(e).__name__}: {e}\n" + traceback.format_exc()

ts = [threading.Thread(target=worker, args=(i,), name=f"w{i}") for i in range(N)]
for t in ts: t.start()
for t in ts: t.join(60)
print("\n\n======== RESULTS ========")
for i in sorted(results): print(f"[{i}] {results[i].splitlines()[0]}")
for i in sorted(results):
    if results[i].startswith("FAIL"):
        print(f"\n--- traceback for {i} ---\n{results[i]}")
        break
