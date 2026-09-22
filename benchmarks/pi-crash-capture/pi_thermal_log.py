#!/usr/bin/env python3
"""
pi_thermal_log.py -- 1 Hz thermal / power / load logger for a Raspberry Pi 5
that is expected to hard-reset mid-run.

Every row is flushed AND fsync'd to disk before the next sample, so the last
second or two before a silent reboot survives. Append-safe: rerun it after the
Pi comes back and the boot_id column shows the discontinuity.

Usage:
    python3 pi_thermal_log.py                      # writes ./thermal.csv
    python3 pi_thermal_log.py --out ~/thermal.csv --interval 1
Stop with Ctrl-C. Stdlib only; needs vcgencmd (present on Raspberry Pi OS / trixie).
"""
import argparse, datetime, os, re, subprocess, sys, time, glob

def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""

def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return default

def vc_temp():
    m = re.search(r"temp=([\d.]+)", sh(["vcgencmd", "measure_temp"]))
    return m.group(1) if m else ""

def sys_temp():
    v = read("/sys/class/thermal/thermal_zone0/temp")
    return f"{int(v)/1000:.1f}" if v.isdigit() else ""

THROTTLE_BITS = {0: "UNDERVOLT", 1: "FREQCAP", 2: "THROTTLED", 3: "SOFTTEMP"}

def throttled():
    m = re.search(r"0x([0-9a-fA-F]+)", sh(["vcgencmd", "get_throttled"]))
    if not m:
        return "", "", ""
    val = int(m.group(1), 16)
    now = "|".join(n for b, n in THROTTLE_BITS.items() if val & (1 << b)) or "-"
    ever = "|".join(n for b, n in THROTTLE_BITS.items() if val & (1 << (b + 16))) or "-"
    return f"0x{val:x}", now, ever

def pmic():
    # Lines look like: "VDD_CORE_A current(7)=6.53564453A" / "EXT5V_V volt(24)=5.13840000V"
    out = {}
    for line in sh(["vcgencmd", "pmic_read_adc"]).splitlines():
        m = re.match(r"\s*(\S+)\s+\w+\(\d+\)=([\d.]+)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out

def fan_rpm():
    for pat in ("/sys/devices/platform/cooling_fan/hwmon/hwmon*/fan1_input",
                "/sys/class/hwmon/hwmon*/fan1_input"):
        for p in glob.glob(pat):
            v = read(p)
            if v:
                return v
    return ""

def cpu_mhz():
    v = read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq")
    return f"{int(v)/1000:.0f}" if v.isdigit() else ""

def cpu_times():
    parts = read("/proc/stat").split("\n")[0].split()
    vals = list(map(int, parts[1:]))
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return idle, sum(vals)

def meminfo():
    d = {}
    for line in read("/proc/meminfo").splitlines():
        k, _, rest = line.partition(":")
        d[k] = int(rest.split()[0]) if rest.split() else 0
    avail = d.get("MemAvailable", 0) // 1024
    swap_used = (d.get("SwapTotal", 0) - d.get("SwapFree", 0)) // 1024
    return avail, swap_used

def top_proc():
    lines = sh(["ps", "-eo", "comm,%cpu", "--sort=-%cpu"]).splitlines()
    if len(lines) > 1:
        parts = lines[1].split()
        if len(parts) >= 2:
            return parts[0], parts[1]
    return "", ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="thermal.csv")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    boot_id = read("/proc/sys/kernel/random/boot_id")[:8]
    base_cols = ["ts", "uptime_s", "boot_id", "event", "temp_c", "temp_sys_c",
                 "throttled_hex", "throttle_now", "throttle_ever", "cpu_mhz",
                 "cpu_busy_pct", "load1", "mem_avail_mb", "swap_used_mb",
                 "fan_rpm", "top_proc", "top_proc_cpu"]

    # Keep the column set stable across restarts by reusing an existing header.
    existing_header = None
    if os.path.exists(args.out) and os.path.getsize(args.out) > 0:
        with open(args.out) as f:
            existing_header = f.readline().strip().split(",")

    if existing_header:
        cols = existing_header
        pmic_cols = [c for c in cols if c not in base_cols]
    else:
        pmic_cols = sorted(pmic().keys())
        cols = base_cols + pmic_cols

    f = open(args.out, "a", buffering=1)
    if not existing_header:
        f.write(",".join(cols) + "\n")
    print(f"logging to {args.out} every {args.interval}s  (boot_id {boot_id}, "
          f"{len(pmic_cols)} PMIC channels). Ctrl-C to stop.", file=sys.stderr)

    prev_idle, prev_total = cpu_times()
    event = "start"
    try:
        while True:
            t0 = time.time()
            idle, total = cpu_times()
            d_total = total - prev_total
            busy = f"{100.0 * (1 - (idle - prev_idle) / d_total):.1f}" if d_total > 0 else ""
            prev_idle, prev_total = idle, total

            hexv, now, ever = throttled()
            avail, swap = meminfo()
            proc, proc_cpu = top_proc()
            p = pmic()
            row = {
                "ts": datetime.datetime.now().isoformat(timespec="seconds"),
                "uptime_s": read("/proc/uptime").split()[0] if read("/proc/uptime") else "",
                "boot_id": boot_id, "event": event,
                "temp_c": vc_temp(), "temp_sys_c": sys_temp(),
                "throttled_hex": hexv, "throttle_now": now, "throttle_ever": ever,
                "cpu_mhz": cpu_mhz(), "cpu_busy_pct": busy,
                "load1": f"{os.getloadavg()[0]:.2f}",
                "mem_avail_mb": avail, "swap_used_mb": swap,
                "fan_rpm": fan_rpm(), "top_proc": proc, "top_proc_cpu": proc_cpu,
            }
            row.update({k: p.get(k, "") for k in pmic_cols})
            f.write(",".join(str(row.get(c, "")) for c in cols) + "\n")
            f.flush()
            os.fsync(f.fileno())   # the whole point: survive a hard reset
            event = ""
            time.sleep(max(0.0, args.interval - (time.time() - t0)))
    except KeyboardInterrupt:
        pass
    finally:
        f.write(",".join(str(v) for v in [datetime.datetime.now().isoformat(timespec="seconds"),
                 "", boot_id, "stop"] + [""] * (len(cols) - 4)) + "\n")
        f.flush(); os.fsync(f.fileno()); f.close()

if __name__ == "__main__":
    main()
