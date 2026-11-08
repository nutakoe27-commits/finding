#!/bin/sh
# Managed by Matchlab: reload Nginx only when this certificate is renewed.
set -eu
if [ "${RENEWED_LINEAGE:-}" = '/etc/letsencrypt/live/prognozich.online' ]; then
    /usr/sbin/nginx -t
    /usr/bin/systemctl reload nginx.service
fi
