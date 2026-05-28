#!/bin/bash
# fix_rpyc.sh — runs as root during linuxserver.io container init.
#
# RPyC startup has been moved into override_start.sh which runs as the
# abc user (UID 911).  Running wine here (as root) would cause:
#   wine: '/config/.wine' is not owned by you
#
# We do use this root context to fix volume ownership so the abc user
# can write to the Docker named volumes (news_cache, zones_data) that
# Docker creates owned by root by default.
mkdir -p /bot/.cache /bot/logs /bot/zones /bot/.checkpoints /bot/src/backtest_results
chown -R abc:abc /bot/.cache /bot/logs /bot/zones /bot/.checkpoints /bot/src/backtest_results 2>/dev/null || true
chmod +x /Metatrader/start.sh 2>/dev/null || true
exit 0