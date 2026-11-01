#!/bin/bash
# disposable SysPerf on 127.0.0.1:5099 with its own data dir; restarted once mid-run
export SYSPERF_DATA_DIR=$SCRATCH/soakrun/data PERFDASH_PORT=5099 PERFDASH_FIX_ENABLED=0 PYTHONPATH=$SCRATCH/wt/src
cd $SCRATCH/wt
start() { ~/.sysperf-sandbox-venv/bin/python -m sysperf > $SCRATCH/soakrun/svc$1.log 2>&1 & echo $! > $SCRATCH/soakrun/pid; }
start 1
for i in $(seq 60); do curl -sf http://127.0.0.1:5099/api/health >/dev/null && break; sleep 1; done
~/.sysperf-sandbox-venv/bin/python tools/soak.py --base http://127.0.0.1:5099 --hours 0.05 --every 15 --out $SCRATCH/soakrun/a.csv; echo "rc_a=$?"
( sleep 50; kill $(cat $SCRATCH/soakrun/pid); sleep 2; start 2 ) &
~/.sysperf-sandbox-venv/bin/python tools/soak.py --base http://127.0.0.1:5099 --hours 0.05 --every 15 --out $SCRATCH/soakrun/b.csv; echo "rc_b=$?"
kill $(cat $SCRATCH/soakrun/pid) 2>/dev/null; wait; echo finished
