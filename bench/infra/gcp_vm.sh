#!/bin/bash
# Create the GCP VM that runs the benchmark's task containers (Harbor + Docker).
#
#   GCP_PROJECT=my-project bench/infra/gcp_vm.sh [create|stop|start|delete|ssh-config]
#
# e2-custom 24 vCPU / 96 GB: 20 task containers at once (5 per arm) with room for Terminal-Bench's
# heavier tasks, and ~$0.85/h. 300 GB pd-balanced holds every SWE-bench Verified Mini and
# Terminal-Bench 2.0 image (~160 GB) so no trial re-downloads one. Check the project's
# CPUS_ALL_REGIONS quota first (24 vCPU must fit next to anything else running):
#   gcloud compute project-info describe --project "$GCP_PROJECT" | grep -B1 -A1 CPUS_ALL_REGIONS
set -euo pipefail
: "${GCP_PROJECT:?set GCP_PROJECT}"
ZONE=${GCP_ZONE:-us-central1-b}
VM=${VM_NAME:-jev-bench}
MACHINE=${VM_MACHINE:-e2-custom-24-98304}
DISK_GB=${VM_DISK_GB:-300}
g=(--project "$GCP_PROJECT" --zone "$ZONE")

case ${1:-create} in
create)
    gcloud compute instances create "$VM" "${g[@]}" --machine-type "$MACHINE" \
        --image-family debian-13 --image-project debian-cloud \
        --boot-disk-size "${DISK_GB}GB" --boot-disk-type pd-balanced \
        --labels purpose=jev-bench --shielded-vtpm --shielded-integrity-monitoring
    "$0" ssh-config
    ;;
stop | start) gcloud compute instances "$1" "$VM" "${g[@]}" ;;   # a stopped VM bills only its disk
delete) gcloud compute instances delete "$VM" "${g[@]}" ;;       # also deletes the boot disk
ssh-config)
    # SSH through Identity-Aware Proxy; add this to ~/.ssh/config, then `ssh $VM`
    cat <<EOF
Host $VM
    HostName $VM
    User $(whoami)
    IdentityFile ~/.ssh/google_compute_engine
    ProxyCommand gcloud compute ssh $(whoami)@$VM --zone $ZONE --project $GCP_PROJECT --tunnel-through-iap -- -W %h:%p
EOF
    ;;
*) echo "usage: $0 [create|stop|start|delete|ssh-config]" >&2; exit 2 ;;
esac
