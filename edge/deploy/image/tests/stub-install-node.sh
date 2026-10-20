#!/usr/bin/env bash
# Stand-in for edge/deploy/install-node.sh in the local image tests, until the
# real installer exists. It does what the image build relies on, in the same
# interface: `install-node.sh --chroot`, run as root inside the rootfs, then
# `edge/deploy/platform/install-platform.sh "$@"` if present.
# It is NOT the node software: units run /bin/true.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
echo "[stub-install-node] args: $*"
[[ " $* " == *" --chroot "* ]] || { echo "[stub-install-node] expected --chroot" >&2; exit 1; }

id skynet >/dev/null 2>&1 || useradd --system --home-dir /var/lib/skynet --shell /usr/sbin/nologin --user-group skynet
install -d -m 0750 -o skynet -g skynet /var/lib/skynet
[[ -x /opt/skynet/venv/bin/python ]] || python3 -m venv --system-site-packages /opt/skynet/venv
install -d /etc/skynet
[[ -f /etc/skynet/defaults.yaml ]] || printf '# stub\nenroll_url: "https://example.invalid"\n' >/etc/skynet/defaults.yaml

for u in uapfinder-firstboot.service uapfinder-netsetup.service uapfinder-setup.service \
    skynet-edge.service uapfinder-starcal.service; do
    cat >"/etc/systemd/system/$u" <<EOF
[Unit]
Description=stub $u (local image test)
[Service]
Type=oneshot
ExecStart=/bin/true
[Install]
WantedBy=multi-user.target
EOF
done
printf '[Unit]\nDescription=stub admin path\n[Path]\nPathChanged=/var/lib/uapfinder\n[Install]\nWantedBy=multi-user.target\n' \
    >/etc/systemd/system/uapfinder-admin.path
cat >/etc/systemd/system/uapfinder-starcal.timer <<'EOF'
[Unit]
Description=stub starcal timer (local image test)
[Timer]
OnCalendar=*:0/30
[Install]
WantedBy=timers.target
EOF
systemctl enable uapfinder-firstboot.service uapfinder-netsetup.service uapfinder-setup.service uapfinder-admin.path \
    skynet-edge.service uapfinder-starcal.timer
systemctl enable gpsd.socket 2>/dev/null || true

if [[ -x "$REPO/edge/deploy/platform/install-platform.sh" || -f "$REPO/edge/deploy/platform/install-platform.sh" ]]; then
    bash "$REPO/edge/deploy/platform/install-platform.sh" "$@"
fi
echo "[stub-install-node] done"
