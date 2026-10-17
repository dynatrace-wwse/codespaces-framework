#!/bin/bash
# Login to GitLab root via form + CSRF through a base URL. Password never printed.
# usage: gl-login.sh <base> [curl-extra...]
S=$SCRATCH
base=$1; shift
pw=$($S/dtx.sh 'kubectl -n gitlab get secret spike-gitlab-root -o jsonpath="{.data.password}" | base64 -d')
[ -n "$pw" ] || { echo "no password"; exit 1; }
jar=$(mktemp -p $S); trap 'rm -f $jar' EXIT
page=$(curl -sk "$@" -c $jar -b $jar "$base/users/sign_in")
tok=$(printf %s "$page" | grep -o 'name="authenticity_token" value="[^"]*"' | head -1 | sed 's/.*value="//;s/"$//')
echo "csrf token present: $([ -n "$tok" ] && echo yes || echo no)"
code=$(curl -sk "$@" -c $jar -b $jar -o /dev/null -w "%{http_code} %{redirect_url}" \
  --data-urlencode "authenticity_token=$tok" --data-urlencode "user[login]=root" \
  --data-urlencode "user[password]=$pw" --data "user[remember_me]=0" "$base/users/sign_in")
echo "POST sign_in -> $code"
echo "cookies in jar: $(grep -v '^#' $jar | awk 'NF>=7{print $6}' | tr '\n' ' ')"
curl -sk "$@" -b $jar -o /tmp/gl-after.html -w "GET / after login -> %{http_code} %{redirect_url}\n" "$base/"
grep -o '<title>[^<]*' /tmp/gl-after.html | head -1
