#!/bin/bash
# Sample the worker's three ceilings once per 10s. PSI is the instrument that
# separates a disk stall from a CPU stall — load average alone cannot.
while true; do
  ssh -o ConnectTimeout=5 autonomous-enablements-worker "
    ts=\$(date +%H:%M:%S)
    mem=\$(free -m | awk '/^Mem:/{print \$3}')
    cpu=\$(awk '/some/{print \$2}' /proc/pressure/cpu | cut -d= -f2)
    io=\$(awk '/some/{print \$2}' /proc/pressure/io | cut -d= -f2)
    mp=\$(awk '/some/{print \$2}' /proc/pressure/memory | cut -d= -f2)
    ld=\$(cut -d' ' -f1 /proc/loadavg)
    read r w u <<< \$(iostat -dm 1 2 2>/dev/null | awk '/^nvme0n1/{r=\$3;w=\$4;u=\$NF} END{print r, w, u}')
    echo \"\$ts mem=\${mem} load=\$ld cpu=\$cpu io=\$io memp=\$mp diskR=\$r diskW=\$w util=\$u\"
  " 2>/dev/null
  sleep 10
done
