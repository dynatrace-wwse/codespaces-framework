#!/usr/bin/env bash
#
# Backfill the `env` tag onto Orbital's long-lived EC2 instances.
#
# WHY THIS EXISTS, AND WHY IT MUST RUN BEFORE THE IAM CONDITION
#
# The four long-lived machines (master, worker-1, worker-2, the stopped
# worker-3) predate environment separation and carry no `env` tag. The
# autoscaler cannot tag them itself: its `ec2:CreateTags` grant is conditioned
# on `ec2:CreateAction=RunInstances`, so it may only tag what it launches. This
# needs a human credential, once.
#
# `shared/environment.py` reads an untagged instance as production, so the
# CODE is safe to deploy before this runs. IAM is not: the moment the
# production role's mutating grants are conditioned on `env=prod`, an untagged
# instance becomes one production can see but cannot stop or terminate. Run
# this, verify, and only then tighten IAM.
#
# Idempotent, and dry-run by default.
#
# Usage:
#   backfill-env-tags.sh                 # show what would change
#   backfill-env-tags.sh --apply         # do it
#   backfill-env-tags.sh --apply --env staging
#
set -euo pipefail

REGION="${AWS_REGION:-eu-west-2}"
ENV_VALUE="prod"
APPLY=0
# Deliberately explicit rather than a wildcard: this writes a tag that governs
# who may terminate a machine, so the set of machines it touches is a list a
# human can read, not a query whose results could change under us.
NAME_PREFIX="autonomous-enablements"

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=1; shift ;;
        --env)   ENV_VALUE="${2:?--env needs a value}"; shift 2 ;;
        --region) REGION="${2:?--region needs a value}"; shift 2 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$ENV_VALUE" in
    prod|staging) ;;
    *) echo "refusing to write env=${ENV_VALUE} — expected prod or staging" >&2; exit 2 ;;
esac

command -v aws >/dev/null || { echo "aws CLI not found" >&2; exit 1; }
aws sts get-caller-identity >/dev/null 2>&1 \
    || { echo "AWS credentials are not usable — refresh them and retry" >&2; exit 1; }

echo "region=${REGION}  env=${ENV_VALUE}  mode=$([ "$APPLY" = 1 ] && echo APPLY || echo DRY-RUN)"
echo

mapfile -t ROWS < <(
    aws ec2 describe-instances --region "$REGION" \
        --filters "Name=tag:Name,Values=${NAME_PREFIX}*" \
        --query 'Reservations[].Instances[?State.Name!=`terminated`].[InstanceId,Tags[?Key==`Name`]|[0].Value,State.Name,Tags[?Key==`env`]|[0].Value]' \
        --output text
)

[ "${#ROWS[@]}" -gt 0 ] || { echo "no instances matched ${NAME_PREFIX}* — nothing to do"; exit 0; }

changed=0 already=0 conflict=0
for row in "${ROWS[@]}"; do
    [ -n "$row" ] || continue
    iid="$(awk '{print $1}' <<<"$row")"
    name="$(awk '{print $2}' <<<"$row")"
    state="$(awk '{print $3}' <<<"$row")"
    cur="$(awk '{print $4}' <<<"$row")"
    [ "$cur" = "None" ] && cur=""

    if [ "$cur" = "$ENV_VALUE" ]; then
        printf '  =  %-20s %-38s already env=%s\n' "$iid" "$name" "$cur"
        already=$((already + 1))
        continue
    fi
    if [ -n "$cur" ]; then
        # Never silently repoint a machine from one environment to another —
        # that is a change of ownership, and it should be a deliberate act with
        # a human looking at it.
        printf '  !  %-20s %-38s env=%s (NOT %s) — refusing to overwrite\n' \
            "$iid" "$name" "$cur" "$ENV_VALUE"
        conflict=$((conflict + 1))
        continue
    fi

    printf '  +  %-20s %-38s [%s] -> env=%s\n' "$iid" "$name" "$state" "$ENV_VALUE"
    changed=$((changed + 1))
    if [ "$APPLY" = 1 ]; then
        aws ec2 create-tags --region "$REGION" --resources "$iid" \
            --tags "Key=env,Value=${ENV_VALUE}"
    fi
done

echo
echo "to tag: ${changed}   already correct: ${already}   conflicting: ${conflict}"
[ "$conflict" -gt 0 ] && { echo "resolve the conflicts above by hand before continuing" >&2; exit 1; }
if [ "$APPLY" = 0 ] && [ "$changed" -gt 0 ]; then
    echo "dry run — re-run with --apply to write these tags"
fi
if [ "$APPLY" = 1 ]; then
    echo
    echo "NEXT: verify every fleet instance reports an env tag, THEN tighten IAM."
    echo "  aws ec2 describe-instances --region ${REGION} \\"
    echo "    --filters Name=tag:Name,Values=${NAME_PREFIX}'*' \\"
    echo "    --query 'Reservations[].Instances[?State.Name!=\`terminated\`].[InstanceId,Tags[?Key==\`env\`]|[0].Value]' --output text"
fi
