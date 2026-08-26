"""Narrow the trigger sequence, using the protocol object directly so this
runs identically on upstream code (which has no viAssertTrigger)."""
import time
import pyvisa
RN = "TCPIP0::192.168.81.74::hislip0,4880::INSTR"

def iface(inst):
    return inst.visalib.sessions[inst.session].interface

def run(label, n_trig, do_clear, gap=0.0, query_after=False, reps=5):
    rm = pyvisa.ResourceManager("@py")
    inst = rm.open_resource(RN, open_timeout=10000); inst.timeout = 5000
    inst.query("*IDN?"); it = iface(inst)
    try:
        for i in range(reps):
            for _ in range(n_trig):
                it.trigger()
                if gap: time.sleep(gap)
            if query_after: inst.query("*IDN?")
            if do_clear: it.device_clear()
        print(f"  {label:44s} survived {reps} reps")
    except Exception as e:
        print(f"  {label:44s} FAILED rep {i}: {type(e).__name__}: {str(e)[:45]}")
    finally:
        try: inst.close()
        except Exception: pass

run("1 trigger + clear",            1, True)
run("2 triggers + clear",           2, True)
run("3 triggers + clear",           3, True)
run("3 triggers, no clear",         3, False)
run("3 triggers + query, no clear", 3, False, query_after=True)
run("3 triggers (100ms gaps) + clear", 3, True, gap=0.1)
run("3 triggers + query + clear",   3, True, query_after=True)
