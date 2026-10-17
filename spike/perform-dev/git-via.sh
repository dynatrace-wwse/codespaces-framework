#!/bin/bash
# git clone/push of root/spike92 through replica TLS proxy on :18445. PAT via GIT_ASKPASS, never printed.
# usage: git-via.sh <dir> [push-mb]
S=$SCRATCH
GH=gitlab--spike92.staging.autonomous-enablements.whydevslovedynatrace.com
d=$S/spike/$1; mb=${2:-0}; rm -rf $d
export SPIKE_PAT=$($S/dtx.sh 'kubectl -n gitlab get secret spike-gitlab-pat -o jsonpath="{.data.token}" | base64 -d')
ap=$(mktemp -p $S); printf '#!/bin/sh\ncase "$1" in Username*) echo root;; *) echo "$SPIKE_PAT";; esac\n' > $ap; chmod +x $ap
export GIT_ASKPASS=$ap GIT_TERMINAL_PROMPT=0 GIT_SSL_NO_VERIFY=1
export GIT_CONFIG_COUNT=2 GIT_CONFIG_KEY_0=http.curloptResolve GIT_CONFIG_VALUE_0=$GH:18445:127.0.0.1 GIT_CONFIG_KEY_1=http.sslVerify GIT_CONFIG_VALUE_1=false
url=https://$GH:18445/root/spike92.git
t0=$(date +%s%N); git clone -q $url $d 2>&1 | sed "s/$SPIKE_PAT/<pat>/g" | tail -3
sed_rc=${PIPESTATUS[0]}
echo "clone rc=$([ -d $d/.git ] && echo 0 || echo fail) ms=$(( ($(date +%s%N)-t0)/1000000 ))"
if [ -d $d/.git ] && [ "$mb" -gt 0 ]; then
  cd $d; head -c $((mb*1024*1024)) /dev/urandom > blob-${mb}mb.bin; git add .; git -c user.name=spike -c user.email=spike@example.invalid commit -qm "spike92 ${mb}MB"
  t0=$(date +%s%N); git push -q origin HEAD 2>&1 | sed "s/$SPIKE_PAT/<pat>/g" | tail -4; echo "push ${mb}MB rc=${PIPESTATUS[0]} ms=$(( ($(date +%s%N)-t0)/1000000 ))"
fi
rm -f $ap
