for p in $(pgrep -u vscode -x MainThread); do kill $p 2>/dev/null; done
for p in $(pgrep -u vscode -x bash); do [ "$p" != "$$" ] && [ "$p" != "$PPID" ] && grep -q shellIntegration /proc/$p/cmdline 2>/dev/null && kill $p; done
sleep 3; echo left=$(pgrep -u vscode -c MainThread)
cd /workspaces/demo-astroshop-problems; t0=$(date +%s%N)
(setsid nohup ~/.local/bin/code-server --bind-addr 0.0.0.0:8443 --auth none --disable-telemetry --disable-update-check . > /tmp/cs.log 2>&1 &)
for i in $(seq 1 100); do c=$(curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8443/healthz); [ "$c" = 200 ] && break; sleep 0.1; done
echo healthz=$c start_ms=$(( ($(date +%s%N)-t0)/1000000 )); sleep 10
ps -o rss= -u vscode | awk '{s+=$1} END{print "IDLE_MiB", int(s/1024)}'
