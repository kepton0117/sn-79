#!/usr/bin/env bash
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy \
  SOCKS_PROXY SOCKS5_PROXY socks_proxy socks5_proxy \
  GIT_HTTP_PROXY GIT_HTTPS_PROXY || true
export NO_PROXY='*'
export no_proxy='*'
exec python3 "$@"
