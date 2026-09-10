#!/usr/bin/env bash
# Run the automatic tests (skips the manual uinput cursor test). Each test also
# runs standalone: `python3 tests/test_vrr.py`. Some need GStreamer + an H.264
# encoder (test_webrtc_offer) or aiohttp; those self-skip if unavailable.
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."
fail=0
for t in tests/test_vrr.py tests/test_clipboard.py tests/test_sdnotify.py \
         tests/test_auth_api.py tests/test_webrtc_offer.py; do
  if python3 "$t" >/tmp/rd-test.$$ 2>&1; then
    echo "OK   $t"
  else
    echo "FAIL $t"; tail -5 /tmp/rd-test.$$ | sed 's/^/     /'; fail=1
  fi
done
rm -f /tmp/rd-test.$$
[ $fail = 0 ] && echo "all green" || echo "FAILURES"
exit $fail
