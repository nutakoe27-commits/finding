#!/usr/bin/env bash
# Upgrade an existing installation after rendering one photo on its actual GPU.
set -eu
umask 077
app_dir="$HOME/mira-photo"
stage=$(cd "$(dirname "$0")" && pwd)
python="$app_dir/.venv/bin/python"
service=mira-photo.service
test "$stage" != "$app_dir"
test -r "$stage/mira.webp"
test -r "$app_dir/mira.webp"
test -s "$app_dir/photo-worker.key"
test -s "$app_dir/worker.py"
systemctl --user is-active --quiet "$service"

# This part can run while the existing photo worker is serving the chat.
"$python" "$stage/worker.py" --download-xl
export CUDA_VISIBLE_DEVICES=GPU-5cc54044-df38-0b75-15d6-a9e419fd2b5e
export MIRA_PHOTO_MODEL=realvisxl
backup=$(mktemp "$app_dir/worker-backup.XXXXXXXX")
cp "$app_dir/worker.py" "$backup"
restore() {
    status=$?
    trap - EXIT
    cp "$backup" "$app_dir/worker.py"
    systemctl --user restart "$service" || echo "Check mira-photo.service" >&2
    exit "$status"
}
trap restore EXIT
trap 'exit 1' INT TERM
systemctl --user stop "$service"
"$python" "$stage/worker.py" --preview "$app_dir/realvis-cafe.jpg" --scene cafe --seed 874011
install -m 600 "$stage/worker.py" "$app_dir/worker.py.new"
mv "$app_dir/worker.py.new" "$app_dir/worker.py"
systemctl --user start "$service"
trap - EXIT INT TERM
echo "Preview: $app_dir/realvis-cafe.jpg"
echo "Previous worker: $backup"
